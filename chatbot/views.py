from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from twilio.rest import Client
from bookings.models import Booking
from inventory.models import Product
import google.generativeai as genai
import os
import json
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

sessions = {}

def send_message(to, message):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        body=message,
        to=to
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

def ask_gemini(user_message, session):
    stock_info = get_stock_context()
    booking_data = {k: v for k, v in session.items() if k not in ["step", "history"]}

    prompt = f"""You are StorefrontBot, a friendly WhatsApp assistant for a business.
Help customers with bookings, stock checks, and support.

CURRENT STOCK:
{stock_info}

BOOKING IN PROGRESS: {json.dumps(booking_data) if booking_data else "None"}

Respond with JSON only, no other text:
{{
  "reply": "your WhatsApp message to the customer",
  "action": "none" | "start_booking" | "save_booking" | "check_stock" | "support",
  "booking_data": {{
    "name": "name or null",
    "date": "date or null",
    "time": "time or null",
    "service": "Consultation | Follow-up | Other or null"
  }}
}}

RULES:
- Be warm and friendly, use emojis naturally
- Extract booking details (name/date/time/service) from natural language
- Only use save_booking when ALL four fields are confirmed
- If booking info is incomplete, ask for only the missing piece
- Keep replies short — this is WhatsApp
- If they say hi/hello, show a welcome menu with your services

Customer message: {user_message}

Conversation so far: {json.dumps(session.get("history", [])[-6:])}"""

    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(prompt)
    raw = response.text.strip()

    # Clean markdown if present
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    
    return json.loads(raw.strip())

@csrf_exempt
def webhook(request):
    if request.method == "POST":
        if request.POST.get("MessageStatus"):
            return HttpResponse("OK", status=200)

        from_number = request.POST.get("From", "")
        text = request.POST.get("Body", "").strip()

        if not from_number or not text:
            return HttpResponse("OK", status=200)

        print(f"FROM: {from_number}, TEXT: {text}")

        session = sessions.get(from_number, {"step": "menu"})

        try:
            result = ask_gemini(text, session)
            reply = result.get("reply", "Sorry, something went wrong. Try again!")
            action = result.get("action", "none")
            booking_data = result.get("booking_data", {})

            if action == "save_booking" and all([
                booking_data.get("name"),
                booking_data.get("date"),
                booking_data.get("time"),
                booking_data.get("service"),
            ]):
                Booking.objects.create(
                    name=booking_data["name"],
                    phone_number=from_number,
                    date=booking_data["date"],
                    time=booking_data["time"],
                    service=booking_data["service"],
                )
                session["history"] = session.get("history", [])
                sessions[from_number] = {"step": "menu", "history": session["history"]}
                print(f"Booking saved for {booking_data['name']}")

            elif action == "start_booking":
                session["step"] = "booking"
                for k, v in booking_data.items():
                    if v:
                        session[k] = v
                sessions[from_number] = session

            else:
                sessions[from_number] = session

            # Update history
            history = session.get("history", [])
            history.append({"role": "user", "content": text})
            history.append({"role": "bot", "content": reply})
            sessions[from_number]["history"] = history[-10:]

        except Exception as e:
            print(f"Gemini error: {e}")
            reply = "Sorry, I'm having trouble right now. Please try again!"

        send_message(from_number, reply)
        return JsonResponse({"status": "ok"}, status=200)

    return HttpResponse("OK", status=200)