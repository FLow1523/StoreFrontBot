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

from django.conf import settings
from django.db import IntegrityError, close_old_connections
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from groq import Groq
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from bookings.models import Booking
from inventory.models import Product
from .models import ProcessedMessage

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# llama-3.3-70b-versatile was retired by Groq. Override with the GROQ_MODEL
# environment variable in Render if you want to try a different model.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

sessions = {}

# Network and processing limits
GROQ_TIMEOUT_SECONDS = 20
MAX_BACKGROUND_WORKERS = 4
# Twilio's limit for a single WhatsApp message body is 1600 characters.
MAX_REPLY_LENGTH = 1600

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

# Messages are processed in the background so the webhook can answer Twilio
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


# ---------------------------------------------------------------------------
# Twilio: signature check and sending
# ---------------------------------------------------------------------------

def get_webhook_url(request):
    """
    The exact public URL Twilio called. Twilio signs this URL, so it must match
    what is configured in the Twilio console character for character.
    Set TWILIO_WEBHOOK_URL on Render to be safe.
    """
    configured = os.getenv("TWILIO_WEBHOOK_URL")
    if configured:
        return configured.strip()
    url = request.build_absolute_uri()
    # Behind Render's proxy Django can see http even though Twilio called https.
    if request.headers.get("X-Forwarded-Proto") == "https" and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def verify_signature(request):
    """Verify that the request was signed by Twilio using the auth token."""
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not auth_token:
        logger.error("TWILIO_AUTH_TOKEN is not set; rejecting webhook request")
        return False

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    validator = RequestValidator(auth_token)
    return validator.validate(get_webhook_url(request), request.POST.dict(), signature)


_twilio_client = None


def get_twilio_client():
    global _twilio_client
    if _twilio_client is None:
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        if not account_sid or not auth_token:
            raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
        _twilio_client = TwilioClient(account_sid, auth_token)
    return _twilio_client


def send_message(to, message):
    """
    Send a WhatsApp text message through Twilio. Raises if the message could
    not be delivered, so callers can react to failures.
    """
    from_whatsapp = os.getenv("TWILIO_WHATSAPP_NUMBER")
    if not from_whatsapp:
        raise RuntimeError("TWILIO_WHATSAPP_NUMBER must be set")
    if not from_whatsapp.startswith("whatsapp:"):
        from_whatsapp = f"whatsapp:{from_whatsapp}"

    # Normalise to "+<digits>" and add the whatsapp: prefix Twilio expects.
    to_clean = to.replace("whatsapp:", "").strip()
    if not to_clean.startswith("+"):
        to_clean = f"+{to_clean}"

    try:
        result = get_twilio_client().messages.create(
            from_=from_whatsapp,
            to=f"whatsapp:{to_clean}",
            body=message[:MAX_REPLY_LENGTH],
        )
    except TwilioRestException as exc:
        # Common codes: 63015 = recipient has not joined the sandbox,
        # 63016 = outside the 24-hour window (needs a template),
        # 20003 = bad credentials.
        logger.warning(
            "Twilio send failed for %s: status=%s code=%s message=%s",
            mask_phone(to_clean),
            exc.status,
            exc.code,
            exc.msg,
        )
        raise
    logger.info(
        "Twilio send accepted for %s (status=%s)", mask_phone(to_clean), result.status
    )


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
- When someone greets you, greet them back warmly and naturally, then ask how you can help. Vary your wording each time. Do NOT show the numbered list on a greeting. Greetings include, in any capitalisation or spelling: hi, hii, hiii, hello, hey, heyy, hey there, hi there, hello there, hallo, hullo, yo, sup, howzit, how zit, heita, hola, good morning, morning, gm, good afternoon, afternoon, good evening, evening, good day, good night, how are you, how are you doing, how is it going, how's it going, how do you do, how r u, how u doing, what's up, wassup, whatsup, greetings, goeie more, goeiemore, goeie middag, goeie naand, goeie dag, hoe gaan dit, hoe gaan dit met jou, and nice to meet you. Match the time of day if they mention it, for example "Good morning, welcome to StorefrontBot. How can I help you today?" If they ask how you are, answer in one short sentence and then ask how you can help.
- If the person says "menu", always show the numbered list.
- When someone wants to book or says "1", reply with: "Our services:[BR][BR]Consultation - N$500[BR]A 1-hour session with our team.[BR][BR]Follow-up - N$250[BR]A 30-minute check-in on previous work.[BR][BR]Other - Price on request[BR]Tell us what you need and we will get back to you.[BR][BR]Which service would you like to book?"
- After they choose a service, ask for name, date and time one at a time
- Only use save_booking when ALL four fields are confirmed
- For stock questions, list each product with name, price and availability
- If booking info is incomplete, ask for only the missing piece
- Never make up services or prices not listed above
- If someone asks where we are located say: Windhoek, Namibia. Monday to Friday, 8am to 5pm.
- If their message is not about bookings, stock, support, our location or our hours, reply with one short, friendly sentence, then show this list: "I can help with the following:[BR][BR]1. Make a booking[BR]2. Check stock availability[BR]3. Speak to support[BR][BR]Reply with a number or just tell me what you need."
- You ONLY answer questions related to this business. If someone asks anything unrelated (general knowledge, math, news, personal questions, other businesses), do not answer it. Say you can only help with business enquiries, then show the numbered list as above.
- Never answer questions about politics, celebrities, history, science, sports, or any topic not related to this business.
- Keep small talk to one short sentence (for example, replying to "how are you"), then steer back to how you can help."""

    history = session.get("history", [])
    messages = [{"role": "system", "content": system_prompt}]
    messages += history[-8:]
    messages.append({"role": "user", "content": user_message})

    request_kwargs = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1500,
    }
    if GROQ_MODEL.startswith("openai/gpt-oss"):
        # gpt-oss models reason before answering. Keep that short so replies stay
        # fast and the reasoning does not use up the token limit.
        request_kwargs["extra_body"] = {"reasoning_effort": "low"}

    response = client.chat.completions.create(**request_kwargs)

    raw = (response.choices[0].message.content or "").strip()
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
    except (json.JSONDecodeError, KeyError, TypeError):
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


def empty_twiml():
    """An empty TwiML response: tells Twilio 'received', send nothing back inline."""
    return HttpResponse(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>",
        status=200,
        content_type="text/xml",
    )


@csrf_exempt
def webhook(request):
    if request.method == "POST":
        if not verify_signature(request):
            logger.warning("Rejected webhook request with a missing or invalid signature")
            return HttpResponse("Forbidden", status=403)

        # Twilio sends form-encoded fields, not JSON:
        #   MessageSid, From ("whatsapp:+264..."), Body, NumMedia, ProfileName ...
        message_id = request.POST.get("MessageSid")
        raw_from = request.POST.get("From") or ""
        body = request.POST.get("Body")

        logger.info(
            "Webhook message: has_sid=%s has_from=%s has_body=%s num_media=%s",
            bool(message_id),
            bool(raw_from),
            bool(body),
            request.POST.get("NumMedia"),
        )

        # Always answer Twilio with 200 once the request is authentic, and do the
        # slow work (database, language model, reply) in the background.
        try:
            from_number = raw_from.replace("whatsapp:", "").strip()
            if not (message_id and from_number and isinstance(body, str)):
                logger.warning("Skipped a message with missing id, sender or body")
                return empty_twiml()

            text = body.strip()[:MAX_INCOMING_TEXT_LENGTH]
            if not text:
                return empty_twiml()

            outcome = register_message(message_id, hash_sender(from_number))
            if outcome == "duplicate":
                logger.info("Ignored a duplicate message delivery")
            elif outcome == "rate_limited":
                logger.warning(
                    "Rate limit reached for %s; message ignored",
                    mask_phone(from_number),
                )
            else:
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

        return empty_twiml()

    # Twilio does not use a GET verification handshake (Meta did).
    return HttpResponse("OK", status=200)