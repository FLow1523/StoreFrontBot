from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from twilio.rest import Client
from bookings.models import Booking
from inventory.models import Product
from groq import Groq
import os
import json
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

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

def ask_groq(user_message, session):
    client = Groq(api_key=GROQ_API_KEY)
    stock_info = get_stock_context()
    booking_data = {k: v for k, v in session.items() if k not in ["step", "history"]}

    system_prompt = f"""You are StorefrontBot, a friendly WhatsApp assistant for a business.
Help customers with bookings, stock checks, and support.

CURRENT STOCK:
{stock_info}

BOOKING IN PROGRESS: {json.dumps(booking_data) if booking_data else "None"}

You must respond with a JSON object only — no other text, no markdown, no backticks.
Format:
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
- Be warm, friendly, use emojis naturally
- Extract booking details from natural language
- Only use save_booking when ALL four fields are confirmed
- If booking info is incomplete, ask for only the missing piece
- Keep replies short — this is WhatsApp not email
- If they say hi/hello, introduce yourself and show menu options"""

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
    print(f"Groq raw response: {raw}")

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
            result = ask_groq(text, session)
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
            history.append({"role": "assistant", "content": reply})
            sessions[from_number]["history"] = history[-8:]

        except Exception as e:
            print(f"Groq error: {e}")
            reply = "Sorry, I'm having a moment! Please try again 😅"

        send_message(from_number, reply)
        return JsonResponse({"status": "ok"}, status=200)

    return HttpResponse("OK", status=200)