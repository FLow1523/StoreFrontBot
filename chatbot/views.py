from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from twilio.rest import Client
from bookings.models import Booking
from inventory.models import Product
import os
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

sessions = {}

def send_message(to, message):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        body=message,
        to=to
    )

@csrf_exempt
def webhook(request):
    if request.method == "POST":
        if request.POST.get("MessageStatus"):
            return HttpResponse("OK", status=200)

        from_number = request.POST.get("From", "")
        text = request.POST.get("Body", "").strip()

        if not from_number or not text:
            return HttpResponse("OK", status=200)

        if text.lower() in ["hi", "hello", "menu"]:
            sessions[from_number] = {"step": "menu"}
            reply = (
                "👋 Welcome to StorefrontBot!\n\n"
                "How can I help you today?\n"
                "1️⃣ Make a booking\n"
                "2️⃣ Check stock\n"
                "3️⃣ Contact support\n\n"
                "Reply with a number."
            )
            send_message(from_number, reply)
            return JsonResponse({"status": "ok"}, status=200)

        session = sessions.get(from_number, {"step": "menu"})
        step = session.get("step", "menu")
        reply = "Sorry, I didn't understand. Reply *hi* to see the menu."

        if step == "menu" and text == "1":
            sessions[from_number] = {"step": "ask_name"}
            reply = "📅 Let's book you in!\n\nWhat is your full name?"

        elif step == "menu" and text == "2":
            products = Product.objects.all()
            if products:
                reply = "📦 *Current Stock:*\n\n"
                for p in products:
                    status = "✅ In stock" if p.quantity > 0 else "❌ Out of stock"
                    reply += f"• {p.name} - N${p.price} ({p.quantity} available) {status}\n"
                reply += "\nReply *hi* for the main menu."
            else:
                reply = "📦 No products available right now. Reply *hi* for menu."

        elif step == "menu" and text == "3":
            reply = "📞 Our team will contact you shortly! Reply *hi* for menu."

        elif step == "ask_name":
            sessions[from_number] = {"step": "ask_date", "name": text}
            reply = f"Thanks {text}! 😊\n\nWhat date works for you?\n(e.g. June 10, 2026)"

        elif step == "ask_date":
            sessions[from_number]["step"] = "ask_time"
            sessions[from_number]["date"] = text
            reply = "What time works for you?\n(e.g. 10:00 AM)"

        elif step == "ask_time":
            sessions[from_number]["step"] = "ask_service"
            sessions[from_number]["time"] = text
            reply = (
                "What service do you need?\n\n"
                "1. Consultation\n"
                "2. Follow-up\n"
                "3. Other"
            )

        elif step == "ask_service":
            services = {"1": "Consultation", "2": "Follow-up", "3": "Other"}
            service = services.get(text, text)
            data = sessions[from_number]
            Booking.objects.create(
                name=data["name"],
                phone_number=from_number,
                date=data["date"],
                time=data["time"],
                service=service
            )
            sessions[from_number] = {"step": "menu"}
            reply = (
                f"✅ Booking confirmed!\n\n"
                f"📋 Name: {data['name']}\n"
                f"📅 Date: {data['date']}\n"
                f"🕐 Time: {data['time']}\n"
                f"💼 Service: {service}\n\n"
                f"Reply *hi* for the main menu."
            )

        send_message(from_number, reply)
        return JsonResponse({"status": "ok"}, status=200)

    return HttpResponse("OK", status=200)