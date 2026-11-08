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
from django.db import IntegrityError, close_old_connections, transaction
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from groq import Groq
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from bookings.models import Booking, TimeSlot
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

BUSINESS_NAME = "StorefrontBot"
BUSINESS_LOCATION = "Windhoek, Namibia"
BUSINESS_HOURS = "Monday to Friday, 8am to 5pm"
SERVICE_PRICES = {
    "Consultation": "N$500",
    "Follow-up": "N$250",
    "Other": "Price on request",
}

# Owner-published time slots
MAX_SLOTS_SHOWN = 10
# Slots are published by the owner, so booking one confirms it. Set to False
# if you want every booking to wait for approval on the dashboard.
AUTO_CONFIRM_BOOKINGS = True
DATE_FORMAT = "%a %d %b %Y"
TIME_FORMAT = "%H:%M"
SLOT_EXIT_WORDS = {"0", "cancel", "back", "exit", "menu"}

BOOKING_REJECTED_REPLY = (
    "I was unable to continue your booking with the details provided. "
    "Please confirm your name and the service you would like."
)
NO_SLOTS_REPLY = (
    "There are no available times at the moment. Please check again soon."
)
SERVICES_REPLY = (
    "Our services:\n\n"
    "Consultation - N$500\nA 1-hour session with our team.\n\n"
    "Follow-up - N$250\nA 30-minute check-in on previous work.\n\n"
    "Other - Price on request\nTell us what you need and we will get back to you.\n\n"
    "Which service would you like to book?"
)
SUPPORT_REPLY = (
    "Please tell me what you need help with. "
    f"Our business hours are {BUSINESS_HOURS}, in {BUSINESS_LOCATION}."
)
CANCEL_INTENT = re.compile(r"\bcancel\w*\b", re.I)
CANCEL_EXCLUDE = re.compile(r"\b(polic(?:y|ies)|fees?|charges?|refunds?|penalt(?:y|ies))\b", re.I)
ACTIVE_STATUSES = ("pending", "confirmed")
MAX_CANCEL_SHOWN = 10
YES_WORDS = {"yes", "y", "yeah", "yep", "yup", "confirm", "ok", "okay", "sure", "please"}
KEEP_WORDS = {"no", "n", "nope", "keep", "0", "back", "exit", "menu"}
CANCEL_NONE_REPLY = (
    "I could not find any upcoming bookings for this number. "
    "If you booked with a different number, please contact us during business hours."
)
CANCEL_KEEP_REPLY = "Okay, your booking has not been changed. How else can I help?"
CANCEL_FAILED_REPLY = (
    "That booking could not be cancelled. It may already have been cancelled, or its time has passed."
)
MENU_MARKER = "1. Make a booking"  # present in every reply that shows the numbered menu
MAX_STOCK_LINES = 30
FALLBACK_REPLY = (
    "Sorry, I did not quite catch that. "
    "Could you tell me what you need, for example a booking or a stock check?"
)
# A clear request to book, with no service named yet, is answered in code.
BOOKING_INTENT = re.compile(
    r"\b(book|booking|bookings|appointment|appointments|reserve|reservation|schedule)\b", re.I
)
BOOKING_INTENT_EXCLUDE = re.compile(
    r"\b(cancel\w*|change\w*|reschedul\w*|move|edit|delete|status|consultation|follow[\s-]?up)\b", re.I
)

BOOKING_CANCELLED_REPLY = (
    "Your booking request has been cancelled.\n\n"
    "How can I help you?\n\n"
    "1. Make a booking\n2. Check stock availability\n3. Speak to support"
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


def validate_details(booking_data):
    """
    Validate the details the language model collected (name and service).
    Returns a dict of cleaned values, or None if anything is missing or invalid.
    The date and time come from a time slot the customer picks, not from the model.
    """
    if not isinstance(booking_data, dict):
        return None

    name = clean_text(booking_data.get("name"), MAX_NAME_LENGTH)
    service_raw = clean_text(booking_data.get("service"), MAX_NAME_LENGTH)
    if not name or not service_raw:
        return None

    service_key = re.sub(r"[\s_-]+", "-", service_raw.lower())
    service = ALLOWED_SERVICES.get(service_key)
    if service is None:
        return None

    return {"name": name, "service": service}


def local_dt(value):
    """Convert a stored datetime to the project time zone for display."""
    return timezone.localtime(value) if timezone.is_aware(value) else value


def format_slot(value):
    local = local_dt(value)
    return f"{local.strftime(DATE_FORMAT)}, {local.strftime(TIME_FORMAT)}"


def get_open_slots():
    """The next open slots in the future, soonest first."""
    return list(
        TimeSlot.objects.filter(is_booked=False, start__gt=timezone.now()).order_by(
            "start"
        )[:MAX_SLOTS_SHOWN]
    )


def create_booking_for_slot(from_number, name, service, slot_id):
    """
    Claim a slot and create the booking in one transaction.
    The conditional update only succeeds if the slot is still free, so two
    customers can never get the same slot. Returns the Booking, or None if the
    slot was taken (or has passed) in the meantime.
    """
    with transaction.atomic():
        claimed = TimeSlot.objects.filter(
            pk=slot_id, is_booked=False, start__gt=timezone.now()
        ).update(is_booked=True)
        if not claimed:
            return None
        slot = TimeSlot.objects.get(pk=slot_id)
        local = local_dt(slot.start)
        return Booking.objects.create(
            name=name,
            phone_number=from_number,
            service=service,
            slot=slot,
            date=local.strftime(DATE_FORMAT),
            time=local.strftime(TIME_FORMAT),
            status="confirmed" if AUTO_CONFIRM_BOOKINGS else "pending",
        )


def build_booking_confirmation(booking):
    """
    Fixed-text confirmation built from the saved booking, so the details are
    exactly what was saved and never reworded by the language model.
    """
    price = SERVICE_PRICES.get(booking.service)
    if booking.status == "confirmed":
        headline = "Booking confirmed."
    else:
        headline = "Booking received. We will confirm it shortly."
    lines = [
        headline,
        "",
        f"Name: {booking.name}",
        f"Service: {booking.service}" + (f" ({price})" if price else ""),
        f"Date: {booking.date}",
        f"Time: {booking.time}",
        f"Location: {BUSINESS_LOCATION}",
        f"Hours: {BUSINESS_HOURS}",
        "",
        'To cancel this booking, send "cancel my booking" at any time. To change the time, cancel it and book a new one.',
        f"Thank you for choosing {BUSINESS_NAME}.",
    ]
    return "\n".join(lines)


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
    booking_data = {k: session[k] for k in ("name", "service") if k in session}

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
{{"reply": "your message with [BR] for line breaks", "action": "none", "booking_data": {{"name": null, "service": null}}}}

The action is either "none" or "show_slots".

RULES:
- Be professional, warm and concise. No emojis at all.
- Sound like a premium business assistant, not a chatbot.
- Use [BR] for every line break in your reply.
- When someone greets you, greet them back warmly and naturally, then ask how you can help. Vary your wording each time. Do NOT show the numbered list on a greeting. Greetings include, in any capitalisation or spelling: hi, hii, hiii, hello, hey, heyy, hey there, hi there, hello there, hallo, hullo, yo, sup, howzit, how zit, heita, hola, good morning, morning, gm, good afternoon, afternoon, good evening, evening, good day, good night, how are you, how are you doing, how is it going, how's it going, how do you do, how r u, how u doing, what's up, wassup, whatsup, greetings, goeie more, goeiemore, goeie middag, goeie naand, goeie dag, hoe gaan dit, hoe gaan dit met jou, and nice to meet you. Match the time of day if they mention it, for example "Good morning, welcome to StorefrontBot. How can I help you today?" If they ask how you are, answer in one short sentence and then ask how you can help.
- If the person says "menu", always show the numbered list.
- If the person replies with 1, 2 or 3 right after the numbered list, 1 means a booking, 2 means a stock check and 3 means support.
- Do not repeat the welcome message or the numbered list unless the person greets you, asks for the menu, or sends something unrelated.
- If someone wants to change or reschedule a booking, tell them to send "cancel my booking" first and then book a new time. Customers cancel their own bookings that way; never say you have cancelled a booking yourself.
- When someone wants to book or says "1", reply with: "Our services:[BR][BR]Consultation - N$500[BR]A 1-hour session with our team.[BR][BR]Follow-up - N$250[BR]A 30-minute check-in on previous work.[BR][BR]Other - Price on request[BR]Tell us what you need and we will get back to you.[BR][BR]Which service would you like to book?"
- After they choose a service, ask for their name. Do not ask for a date or time, and never suggest or invent times. The system shows the available times itself.
- As soon as you have both the service and the name, set action to "show_slots" and put the name and service in booking_data. The system replaces the reply for that turn, so keep it to one short sentence. Never say a booking is confirmed; only the system confirms bookings.
- For stock questions, list each product with name, price and availability
- If booking info is incomplete, ask for only the missing piece (the service or the name)
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

    for attempt in (1, 2):
        response = client.chat.completions.create(**request_kwargs)
        raw = (response.choices[0].message.content or "").strip()
        logger.debug("Groq response received (%d characters)", len(raw))
        parsed = parse_model_output(raw)
        if parsed is not None:
            return parsed
        logger.warning(
            "Unreadable model output (attempt %d): length=%d empty=%s has_brace=%s",
            attempt,
            len(raw),
            not raw,
            "{" in raw,
        )

    return {
        "reply": FALLBACK_REPLY,
        "action": "none",
        "booking_data": {"name": None, "service": None},
    }


def parse_model_output(raw):
    """
    Turn the model's text into {"reply", "action", "booking_data"}.
    Returns None if nothing usable can be read, so the caller can retry.
    """
    raw = (raw or "").strip()
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    candidates = [raw]
    first, last = raw.find("{"), raw.rfind("}")
    if 0 <= first < last:
        candidates.append(raw[first:last + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate, strict=False)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("reply"), str) and parsed["reply"].strip():
            parsed["reply"] = fix_linebreaks(parsed["reply"])
            return parsed

    match = re.search(r'"reply"\s*:\s*"(.*?)"(?=\s*,\s*"action")', raw, re.DOTALL)
    if match and match.group(1).strip():
        return {
            "reply": fix_linebreaks(match.group(1)),
            "action": "none",
            "booking_data": {"name": None, "service": None},
        }

    # The model answered in plain text instead of JSON: send it as the reply.
    if raw and "{" not in raw:
        return {
            "reply": fix_linebreaks(raw[:MAX_REPLY_LENGTH]),
            "action": "none",
            "booking_data": {"name": None, "service": None},
        }
    return None


def phone_variants(number):
    """Bookings made earlier may be stored with or without the leading plus."""
    digits = str(number or "").replace("whatsapp:", "").strip().lstrip("+")
    return {digits, f"+{digits}"}


def describe_booking(booking):
    return f"{booking.service} - {booking.date}, {booking.time}"


def get_cancellable_bookings(from_number):
    """This customer's own upcoming bookings, soonest first."""
    return list(
        Booking.objects.filter(
            phone_number__in=phone_variants(from_number),
            status__in=ACTIVE_STATUSES,
            slot__isnull=False,
            slot__start__gt=timezone.now(),
        )
        .select_related("slot")
        .order_by("slot__start")[:MAX_CANCEL_SHOWN]
    )


def cancel_booking_for_customer(from_number, booking_id):
    """
    Cancel one booking and free its slot, in one transaction. The conditional
    update only matches a booking that belongs to this phone number, is still
    active and has not started, so a customer can never cancel someone else's
    booking. Returns the Booking, or None if it could not be cancelled.
    """
    with transaction.atomic():
        changed = Booking.objects.filter(
            pk=booking_id,
            phone_number__in=phone_variants(from_number),
            status__in=ACTIVE_STATUSES,
            slot__start__gt=timezone.now(),
        ).update(status="cancelled")
        if not changed:
            return None
        booking = Booking.objects.get(pk=booking_id)
        if booking.slot_id:
            TimeSlot.objects.filter(pk=booking.slot_id).update(is_booked=False)
        return booking


def start_cancellation(from_number, session):
    """Find the customer's bookings and ask which one (or confirm the only one)."""
    bookings = get_cancellable_bookings(from_number)
    if not bookings:
        reset_booking_state(session)
        return CANCEL_NONE_REPLY
    if len(bookings) == 1:
        session["step"] = "cancel_confirm"
        session["cancel_id"] = bookings[0].id
        session.pop("cancel_ids", None)
        return (
            "You are about to cancel:\n\n"
            f"{describe_booking(bookings[0])}\n\n"
            "Reply YES to cancel this booking, or NO to keep it."
        )
    session["step"] = "cancel_choose"
    session["cancel_ids"] = [booking.id for booking in bookings]
    session.pop("cancel_id", None)
    lines = ["Your upcoming bookings:", ""]
    for number, booking in enumerate(bookings, start=1):
        lines.append(f"{number}. {describe_booking(booking)}")
    lines += ["", "Reply with the number of the booking you want to cancel, or 0 to keep them all."]
    return "\n".join(lines)


def handle_cancellation(from_number, session, text):
    """Handle a message while the customer is cancelling a booking. No model involved."""
    choice = text.strip().lower().strip(".!")

    if session.get("step") == "cancel_choose":
        if choice in KEEP_WORDS:
            reset_booking_state(session)
            return CANCEL_KEEP_REPLY
        ids = session.get("cancel_ids") or []
        if re.fullmatch(r"\d{1,2}", choice) and 1 <= int(choice) <= len(ids):
            booking = Booking.objects.filter(
                pk=ids[int(choice) - 1],
                phone_number__in=phone_variants(from_number),
                status__in=ACTIVE_STATUSES,
            ).first()
            if booking is not None:
                session["step"] = "cancel_confirm"
                session["cancel_id"] = booking.id
                return (
                    "You are about to cancel:\n\n"
                    f"{describe_booking(booking)}\n\n"
                    "Reply YES to cancel this booking, or NO to keep it."
                )
            prefix = "That booking can no longer be cancelled."
        else:
            prefix = "Please reply with the number of a booking from the list."
        refreshed = start_cancellation(from_number, session)
        if session.get("step") == "cancel_choose":
            return f"{prefix}\n\n{refreshed}"
        return refreshed

    # cancel_confirm
    if choice in YES_WORDS:
        booking = cancel_booking_for_customer(from_number, session.get("cancel_id"))
        reset_booking_state(session)
        if booking is None:
            return CANCEL_FAILED_REPLY
        logger.info("Booking cancelled by customer %s", mask_phone(from_number))
        return (
            "Your booking has been cancelled.\n\n"
            f"{describe_booking(booking)}\n\n"
            "You can make a new booking at any time. Just tell me when you are ready."
        )
    if choice in KEEP_WORDS:
        reset_booking_state(session)
        return CANCEL_KEEP_REPLY
    return "Please reply YES to cancel this booking, or NO to keep it."


def build_stock_reply():
    """Stock list built straight from the database, so it is always accurate."""
    products = list(Product.objects.all().order_by("name")[: MAX_STOCK_LINES + 1])
    if not products:
        return "We have no products listed at the moment."
    lines = ["Current stock:", ""]
    for product in products[:MAX_STOCK_LINES]:
        if product.quantity > 0:
            lines.append(f"{product.name} - N${product.price} - {product.quantity} in stock")
        else:
            lines.append(f"{product.name} - N${product.price} - out of stock")
    if len(products) > MAX_STOCK_LINES:
        lines.append("...and more. Ask me about a specific product.")
    lines += ["", "Would you like to make a booking or ask about something else?"]
    return "\n".join(lines)


def remember(from_number, session, text, reply):
    """Store the exchange in the session history (last 8 messages)."""
    history = session.get("history", [])
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    session["history"] = history[-8:]
    sessions[from_number] = session


def reset_booking_state(session):
    for key in ("name", "service", "slot_ids", "cancel_ids", "cancel_id"):
        session.pop(key, None)
    session["step"] = "menu"


def present_slots(session, header="Available times:"):
    """
    Build the numbered list of open slots and move the session to the
    slot-choosing step. Returns None (and resets) if nothing is open.
    """
    slots = get_open_slots()
    if not slots:
        reset_booking_state(session)
        return None
    session["step"] = "choose_slot"
    session["slot_ids"] = [slot.id for slot in slots]
    lines = [header, ""]
    for number, slot in enumerate(slots, start=1):
        lines.append(f"{number}. {format_slot(slot.start)}")
    lines += ["", "Reply with the number of the time you would like, or 0 to cancel."]
    return "\n".join(lines)


def handle_slot_choice(from_number, session, text):
    """Handle a message while the customer is choosing a time. No model involved."""
    choice = text.strip().lower()

    if choice in SLOT_EXIT_WORDS:
        reset_booking_state(session)
        return BOOKING_CANCELLED_REPLY

    name = session.get("name")
    service = session.get("service")
    if not name or not service:
        reset_booking_state(session)
        return BOOKING_REJECTED_REPLY

    slot_ids = session.get("slot_ids") or []
    if re.fullmatch(r"\d{1,2}", choice) and 1 <= int(choice) <= len(slot_ids):
        booking = create_booking_for_slot(
            from_number, name, service, slot_ids[int(choice) - 1]
        )
        if booking:
            reset_booking_state(session)
            logger.info("Booking saved for %s", mask_phone(from_number))
            return build_booking_confirmation(booking)
        prefix = "Sorry, that time has just been taken."
    else:
        prefix = "Please reply with the number of one of the times below."

    slot_text = present_slots(session)
    if slot_text is None:
        return NO_SLOTS_REPLY
    return f"{prefix}\n\n{slot_text}"


def build_reply(from_number, text):
    """Generate the reply for one incoming message and update the session."""
    session = sessions.get(from_number, {"step": "menu"})

    try:
        if session.get("step") == "choose_slot":
            reply = handle_slot_choice(from_number, session, text)
            remember(from_number, session, text, reply)
            return reply

        if session.get("step") in ("cancel_choose", "cancel_confirm"):
            reply = handle_cancellation(from_number, session, text)
            remember(from_number, session, text, reply)
            return reply

        history = session.get("history", [])
        last_reply = history[-1]["content"] if history else None

        # "1", "2" or "3" straight after the numbered menu is answered in code.
        menu_choice = text.strip().rstrip(".").strip()
        if last_reply and MENU_MARKER in last_reply and menu_choice in ("1", "2", "3"):
            if menu_choice == "1":
                reply = SERVICES_REPLY
            elif menu_choice == "2":
                reply = build_stock_reply()
            else:
                reply = SUPPORT_REPLY
            remember(from_number, session, text, reply)
            return reply

        if CANCEL_INTENT.search(text) and not CANCEL_EXCLUDE.search(text):
            reply = start_cancellation(from_number, session)
            remember(from_number, session, text, reply)
            return reply

        if (
            BOOKING_INTENT.search(text)
            and not BOOKING_INTENT_EXCLUDE.search(text)
            and last_reply != SERVICES_REPLY
        ):
            reply = SERVICES_REPLY
            remember(from_number, session, text, reply)
            return reply

        result = ask_groq(text, session)
        reply = result.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            reply = "Sorry, something went wrong. Please try again."
        action = result.get("action", "none")
        booking_data = result.get("booking_data", {})

        # "save_booking" is accepted too, in case the model uses its old habit.
        if action in ("show_slots", "save_booking"):
            details = validate_details(booking_data)
            if details:
                session["name"] = details["name"]
                session["service"] = details["service"]
                header = f"Thank you, {details['name']}. These times are available:"
                reply = present_slots(session, header) or NO_SLOTS_REPLY
            else:
                logger.warning(
                    "Incomplete or invalid booking details from %s",
                    mask_phone(from_number),
                )
                reply = BOOKING_REJECTED_REPLY

        remember(from_number, session, text, reply)
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