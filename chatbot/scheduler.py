from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from django.utils import timezone
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)

def send_reminders():
    from bookings.models import Booking
    from chatbot.views import send_message

    tomorrow = (timezone.now() + timedelta(days=1)).strftime("%B %d, %Y")
    tomorrow_short = (timezone.now() + timedelta(days=1)).strftime("%d %B")

    bookings = Booking.objects.filter(
        status="confirmed"
    ).exclude(phone_number="")

    sent = 0
    for booking in bookings:
        if tomorrow_short.lstrip("0") in booking.date or tomorrow in booking.date:
            message = (
                f"Hello {booking.name},\n\n"
                f"This is a reminder from StorefrontBot.\n\n"
                f"You have a {booking.service} appointment tomorrow:\n"
                f"Date: {booking.date}\n"
                f"Time: {booking.time}\n\n"
                f"If you need to reschedule, please message us."
            )
            try:
                send_message(booking.phone_number, message)
                sent += 1
                logger.info(f"Reminder sent to {booking.name}")
            except Exception as e:
                logger.error(f"Failed to send reminder to {booking.name}: {e}")

    logger.info(f"Reminders sent: {sent}")

def start():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        send_reminders,
        trigger=CronTrigger(hour=9, minute=0),  # runs every day at 9am UTC
        id="send_reminders",
        name="Send booking reminders",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started")