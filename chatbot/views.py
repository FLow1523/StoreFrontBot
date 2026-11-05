import hashlib
import hmac
import json
import logging
import os
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import requests
from django.conf import settings
from django.db import IntegrityError, close_old_connections
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from groq import Groq

from bookings.models import Booking
from inventory.models import Product
from .models import ProcessedMessage

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

sessions = {}

# Network and processing limits
META_TIMEOUT_SECONDS = 15
GROQ_TIMEOUT_SECONDS = 20
MAX_BACKGROUND_WORKERS = 4
MAX_REPLY_LENGTH = 4000

# Per-sender rate limit (enforced through the database so it holds across restarts)
RATE_LIMIT_MESSAGES = 10
RATE_LIMIT_WINDOW_SECONDS = 60
DEDUP_RETENTION_DAYS = 7

# Booking validation rules
ALLOWED_SERVICES = {
    "consultation": "Consultation",
    "follow-up": "Follow-up",
    "other": "Other",
}
MAX_NAME_LENGTH = 100
MAX_FIELD_LENGTH = 50
MAX_INCOMING_TEXT_LENGTH = 1000
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

BOOKING_REJECTED_REPLY = (
    "I was unable to complete your booking with the details provided. "
    "Please confirm your name, preferred date, time and service."
)
PROCESSING_ERROR_REPLY = (
    "Sorry, I am unable to process your request right now. Please try again."
)

# Messages are processed in the background so the webhook can answer Meta
# immediately. A lock per sender keeps one customer's messages in order.
executor = ThreadPoolExecutor(
    max_workers=MAX_BACKGROUND_WORKERS, thread_name_prefix="chatbot"
)
_sender_locks = {}
_sender_locks_guard = threading.Lock()


def get_sender_lock(sender):
    with _sender_locks_guard:
        return _sender_locks.setdefault(sender, threading.Lock())


def mask_phone(number):
    """Return a masked phone number that is safe to write to logs."""
    digits = str(number or "")
    return f"***{digits[-4:]}" if len(digits) > 4 else "***"


def hash_sender(number):
    """Return a keyed hash of a phone number for rate limiting."""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        str(number).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def clean_text(value, max_length):
    """Return a cleaned single-line string, or None if it is missing or invalid."""
    if not isinstance(value, str):
        return None
    cleaned = CONTROL_CHARACTERS.sub(" ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or len(cleaned) > max_length:
        return None
    return cleaned


def validate_booking(booking_data):
    """
    Validate booking data produced by the language model.
    Returns a dict of cleaned values, or None if anything is missing or invalid.
    """
    if not isinstance(booking_data, dict):
        return None

    name = clean_text(booking_data.get("name"), MAX_NAME_LENGTH)
    date = clean_text(booking_data.get("date"), MAX_FIELD_LENGTH)
    time = clean_text(booking_data.get("time"), MAX_FIELD_LENGTH)
    service_raw = clean_text(booking_data.get("service"), MAX_NAME_LENGTH)

    if not all([name, date, time, service_raw]):
        return None

    service_key = re.sub(r"[\s_-]+", "-", service_raw.lower())
    service = ALLOWED_SERVICES.get(service_key)
    if service is None:
        return None

    return {"name": name, "date": date, "time": time, "service": service}


def verify_signature(request):
    """Verify that the request was signed by Meta using the app secret."""
    app_secret = os.getenv("META_APP_SECRET")
    if not app_secret:
        logger.error("META_APP_SECRET is not set; rejecting webhook request")
        return False

    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"), request.body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


def send_message(to, message):
    """
    Send a WhatsApp text message. Raises requests.RequestException if the
    message could not be delivered, so callers can react to failures.
    """
    phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
    access_token = os.getenv("META_ACCESS_TOKEN")
    if not phone_number_id or not access_token:
        raise RuntimeError("META_PHONE_NUMBER_ID and META_ACCESS_TOKEN must be set")

    # Clean the number - remove whatsapp: prefix if present
    to_clean = to.replace("whatsapp:", "").replace("+", "")

    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_clean,
        "type": "text",
        "text": {"body": message[:MAX_REPLY_LENGTH]}
    }
    response = requests.post(
        url, headers=headers, json=payload, timeout=META_TIMEOUT_SECONDS
    )
    if not response.ok:
        logger.warning(
            "Meta send failed for %s with status %s",
            mask_phone(to_clean),
            response.status_code,
        )
        response.raise_for_status()
    logger.info("Meta send succeeded for %s", mask_phone(to_clean))


def get_stock_context():
    products = Product.objects.all()
    if not products:
        return "No products currently in stock."
    lines = []
    for p in products:
        status = "in stock" if p.quantity > 0 else "out of stock"
        lines.append(f"- {p.name}: N${p.price}, {p.quantity} units, {status}")
    return "\n".join(lines)


def fix_linebreaks(text):
    text = text.replace("[BR]", "\n")
    text = text.replace("\\n", "\n")
    return text


def ask_groq(user_message, session):
    client = Groq(api_key=GROQ_API_KEY, timeout=GROQ_TIMEOUT_SECONDS, max_retries=1)
    stock_info = get_stock_context()
    booking_data = {k: v for k, v in session.items() if k not in ["step", "history"]}

    system_prompt = f"""You are StorefrontBot, a professional WhatsApp assistant for a business.
Help customers with bookings, stock checks, and support.

CURRENT STOCK:
{stock_info}

BUSINESS LOCATION: Windhoek, Namibia
BUSINESS HOURS: Monday to Friday, 8am to 5pm

BOOKING IN PROGRESS: {json.dumps(booking_data) if booking_data else "None"}

You must respond with a JSON object only — no other text, no markdown, no backticks.
Use [BR] for line breaks inside the reply string. Never use actual newlines inside JSON strings.

Format:
{{"reply": "your message with [BR] for line breaks", "action": "none", "booking_data": {{"name": null, "date": null, "time": null, "service": null}}}}

RULES:
- Be professional, warm and concise. No emojis at all.
- Sound like a premium business assistant, not a chatbot.
- Use [BR] for every line break in your reply.
- When someone says hi/hello/menu, reply with: "Welcome to StorefrontBot.[BR][BR]How can I help you today?[BR][BR]1. Make a booking[BR]2. Check stock availability[BR]3. Speak to support[BR][BR]Reply with a number or just tell me what you need."
- When someone wants to book or says "1", reply with: "Our services:[BR][BR]Consultation - N$500[BR]A 1-hour session with our team.[BR][BR]Follow-up - N$250[BR]A 30-minute check-in on previous work.[BR][BR]Other - Price on request[BR]Tell us what you need and we will get back to you.[BR][BR]Which service would you like to book?"
- After they choose a service, ask for name, date and time one at a time
- Only use save_booking when ALL four fields are confirmed
- For stock questions, list each product with name, price and availability
- If booking info is incomplete, ask for only the missing piece
- Never make up services or prices not listed above
- If someone asks where we are located say: Windhoek, Namibia. Monday to Friday, 8am to 5pm.
- If they seem confused ask: Would you like to make a booking, check stock, or speak to support?
- You ONLY answer questions related to this business. If someone asks anything unrelated (general knowledge, math, news, personal questions, other businesses) respond with exactly: "I can only assist with business-related enquiries. Would you like to make a booking, check our stock, or speak to support?"
- Never answer questions about politics, celebrities, history, science, sports, or any topic not related to this business.
- Never engage in small talk beyond a brief professional greeting."""

    history = session.get("history", [])
    messages = [{"role": "system", "content": system_prompt}]
    messages += history[-8:]
    messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        temperature=0.7,
        max_tokens=500,
    )

    raw = response.choices[0].message.content.strip()
    logger.debug("Groq response received (%d characters)", len(raw))

    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    raw = raw.strip()

    try:
        parsed = json.loads(raw)
        parsed["reply"] = fix_linebreaks(parsed["reply"])
        return parsed
    except json.JSONDecodeError:
        reply_match = re.search(r'"reply"\s*:\s*"(.*?)"(?=\s*,\s*"action")', raw, re.DOTALL)
        if reply_match:
            reply_text = fix_linebreaks(reply_match.group(1))
            return {
                "reply": reply_text,
                "action": "none",
                "booking_data": {"name": None, "date": None, "time": None, "service": None}
            }
        return {
            "reply": "Welcome to StorefrontBot.\n\nHow can I help you today?\n\n1. Make a booking\n2. Check stock availability\n3. Speak to support\n\nReply with a number or just tell me what you need.",
            "action": "none",
            "booking_data": {"name": None, "date": None, "time": None, "service": None}
        }


def build_reply(from_number, text):
    """Generate the reply for one incoming message and update the session."""
    session = sessions.get(from_number, {"step": "menu"})

    try:
        result = ask_groq(text, session)
        reply = result.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            reply = "Sorry, something went wrong. Please try again."
        action = result.get("action", "none")
        booking_data = result.get("booking_data", {})

        if action == "save_booking":
            validated = validate_booking(booking_data)
            if validated:
                Booking.objects.create(
                    name=validated["name"],
                    phone_number=from_number,
                    date=validated["date"],
                    time=validated["time"],
                    service=validated["service"],
                )
                session["history"] = session.get("history", [])
                sessions[from_number] = {"step": "menu", "history": session["history"]}
                logger.info("Booking saved for %s", mask_phone(from_number))
            else:
                logger.warning(
                    "Rejected incomplete or invalid booking data from %s",
                    mask_phone(from_number),
                )
                reply = BOOKING_REJECTED_REPLY
                sessions[from_number] = session

        elif action == "start_booking":
            session["step"] = "booking"
            if isinstance(booking_data, dict):
                for key in ("name", "date", "time", "service"):
                    cleaned = clean_text(booking_data.get(key), MAX_NAME_LENGTH)
                    if cleaned:
                        session[key] = cleaned
            sessions[from_number] = session

        else:
            sessions[from_number] = session

        history = session.get("history", [])
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        sessions[from_number]["history"] = history[-8:]
        return reply

    except Exception:
        logger.exception("Error while generating a reply")
        return PROCESSING_ERROR_REPLY


def process_message(from_number, text):
    """Runs in a background thread: build the reply and deliver it."""
    close_old_connections()
    try:
        with get_sender_lock(from_number):
            reply = build_reply(from_number, text)
        try:
            send_message(from_number, reply)
        except Exception:
            logger.exception("Failed to deliver a reply to %s", mask_phone(from_number))
    except Exception:
        logger.exception("Unhandled error while processing a message")
    finally:
        close_old_connections()


def register_message(message_id, sender_hash):
    """
    Decide whether a message should be processed.
    Returns "new", "duplicate" or "rate_limited".
    """
    window_start = timezone.now() - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    recent = ProcessedMessage.objects.filter(
        sender_hash=sender_hash, created_at__gte=window_start
    ).count()
    if recent >= RATE_LIMIT_MESSAGES:
        return "rate_limited"

    try:
        ProcessedMessage.objects.create(message_id=message_id, sender_hash=sender_hash)
    except IntegrityError:
        return "duplicate"
    return "new"


def purge_old_records():
    cutoff = timezone.now() - timedelta(days=DEDUP_RETENTION_DAYS)
    deleted, _ = ProcessedMessage.objects.filter(created_at__lt=cutoff).delete()
    if deleted:
        logger.info("Purged %d old processed-message records", deleted)


@csrf_exempt
def webhook(request):
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = (request.GET.get("hub.verify_token") or "").strip()
        challenge = request.GET.get("hub.challenge") or ""
        expected = (os.getenv("VERIFY_TOKEN") or "").strip()

        if (
            mode == "subscribe"
            and token
            and expected
            and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
        ):
            return HttpResponse(challenge, status=200, content_type="text/plain")

        logger.warning(
            "Webhook verification failed: mode=%s token_received=%s "
            "token_configured=%s received_length=%d configured_length=%d",
            mode,
            bool(token),
            bool(expected),
            len(token),
            len(expected),
        )
        return HttpResponse("Forbidden", status=403)

    if request.method == "POST":
        if not verify_signature(request):
            logger.warning("Rejected webhook request with a missing or invalid signature")
            return HttpResponse("Forbidden", status=403)

        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Rejected webhook request with an invalid JSON body")
            return JsonResponse({"status": "invalid payload"}, status=400)

        if not isinstance(data, dict):
            return JsonResponse({"status": "invalid payload"}, status=400)

        # Always answer Meta with 200 once the request is authentic, and do the
        # slow work (database, language model, reply) in the background.
        try:
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    for msg in value.get("messages", []):
                        if msg.get("type") != "text":
                            continue

                        message_id = msg.get("id")
                        from_number = msg.get("from")
                        body = (msg.get("text") or {}).get("body")
                        if not (message_id and from_number and isinstance(body, str)):
                            continue

                        text = body.strip()[:MAX_INCOMING_TEXT_LENGTH]
                        if not text:
                            continue

                        outcome = register_message(message_id, hash_sender(from_number))
                        if outcome == "duplicate":
                            logger.info("Ignored a duplicate message delivery")
                            continue
                        if outcome == "rate_limited":
                            logger.warning(
                                "Rate limit reached for %s; message ignored",
                                mask_phone(from_number),
                            )
                            continue

                        logger.info(
                            "Accepted message from %s (%d characters)",
                            mask_phone(from_number),
                            len(text),
                        )
                        executor.submit(process_message, from_number, text)

            # Occasional housekeeping; moves to the scheduler in the reminders step.
            if random.random() < 0.01:
                purge_old_records()

        except Exception:
            logger.exception("Webhook processing error")

        return JsonResponse({"status": "ok"}, status=200)

    return HttpResponse("OK", status=200)