from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from twilio.rest import Client
from bookings.models import Booking
from inventory.models import Product
from groq import Groq
import os
import json
import re
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

sessions = {}

def send_message(to, message):
    import requests
    phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
    access_token = os.getenv("META_ACCESS_TOKEN")
    
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
        "text": {"body": message}
    }
    response = requests.post(url, headers=headers, json=payload)
    print(f"Meta send response: {response.status_code} {response.text}")

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
    client = Groq(api_key=GROQ_API_KEY)
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
- If they seem confused ask: Would you like to make a booking, check stock, or speak to support?"""

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
    print(f"Groq raw: {raw}")

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

@csrf_exempt
def webhook(request):
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")
        if mode == "subscribe" and token == os.getenv("VERIFY_TOKEN", "storefront_verify_123"):
            return HttpResponse(challenge, status=200)
        return HttpResponse("Forbidden", status=403)

    if request.method == "POST":
        data = json.loads(request.body)
        print(f"Meta incoming: {data}")
        
        try:
            entry = data["entry"][0]
            changes = entry["changes"][0]
            value = changes["value"]
            messages = value.get("messages", [])
            
            if not messages:
                return JsonResponse({"status": "ok"}, status=200)
            
            msg = messages[0]
            from_number = msg["from"]
            
            if msg.get("type") != "text":
                return JsonResponse({"status": "ok"}, status=200)
                
            text = msg["text"]["body"].strip()
            
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

                history = session.get("history", [])
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                sessions[from_number]["history"] = history[-8:]

            except Exception as e:
                print(f"Groq error: {e}")
                reply = "Sorry, I am unable to process your request right now. Please try again."

            send_message(from_number, reply)
            
        except Exception as e:
            print(f"Webhook error: {e}")
            
        return JsonResponse({"status": "ok"}, status=200)

    return HttpResponse("OK", status=200)