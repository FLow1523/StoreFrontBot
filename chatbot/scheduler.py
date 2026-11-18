import logging
import os
from datetime import datetime, time, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from twilio.base.exceptions import TwilioRestException

logger = logging.getLogger(__name__)

MAX_REMINDER_ATTEMPTS = 3
# Don't remind someone who only just booked.
MIN_BOOKING_AGE = timedelta(hours=2)
# Twilio error for a free-form message outside the 24-hour customer window.
OUTSIDE_WINDOW_CODE = 63016


def _deliver(booking, brand):
    """
    Try a normal message first (free inside the 24-hour window). If WhatsApp
    refuses because the window has closed, use the approved template instead.
    """
    from chatbot.views import send_message, send_template_message

    text = (
        f"Hello {booking.name},\n\n"
        f"This is a reminder from {brand}.\n\n"
        f"You have a {booking.service} appointment:\n"
        f"Date: {booking.date}\n"
        f"Time: {booking.time}\n\n"
        f"If you need to reschedule, please message us."
    )
    try:
        send_message(booking.phone_number, text)
    except TwilioRestException as exc:
        template_sid = os.getenv("REMINDER_TEMPLATE_SID", "").strip()
        if exc.code == OUTSIDE_WINDOW_CODE and template_sid:
            send_template_message(
                booking.phone_number,
                template_sid,
                {
                    "1": booking.name,
                    "2": brand,
                    "3": booking.service,
                    "4": booking.date,
                    "5": booking.time,
                },
            )
        else:
            raise


def send_reminders():
    """
    Remind confirmed bookings whose appointment is still ahead and falls
    today or tomorrow (local time).

    - Runs hourly, so a failed send is retried within the hour, up to
      MAX_REMINDER_ATTEMPTS, as long as the appointment has not started.
    - Each reminder is atomically claimed via reminder_sent_at, so two
      workers cannot both send the same one.
    """
    from bookings.branding import site_name
    from bookings.models import Booking
    from chatbot.views import mask_phone

    now = timezone.now()
    local_now = timezone.localtime(now)
    window_end = timezone.make_aware(
        datetime.combine(local_now.date() + timedelta(days=2), time.min),
        timezone.get_current_timezone(),
    )

    candidates = list(
        Booking.objects.filter(
            status="confirmed",
            slot__isnull=False,
            slot__start__gt=now,
            slot__start__lt=window_end,
            reminder_sent_at__isnull=True,
            reminder_attempts__lt=MAX_REMINDER_ATTEMPTS,
            created_at__lte=now - MIN_BOOKING_AGE,
        )
        .exclude(phone_number="")
        .select_related("slot")
    )

    brand = site_name()
    sent = failed = 0
    for booking in candidates:
        claimed = Booking.objects.filter(
            pk=booking.pk, reminder_sent_at__isnull=True
        ).update(reminder_sent_at=timezone.now())
        if not claimed:
            continue

        try:
            _deliver(booking, brand)
            sent += 1
            logger.info("Reminder sent to %s", mask_phone(booking.phone_number))
        except Exception:
            failed += 1
            with transaction.atomic():
                Booking.objects.filter(pk=booking.pk).update(
                    reminder_sent_at=None,
                    reminder_attempts=F("reminder_attempts") + 1,
                )
            logger.exception(
                "Failed to send reminder to %s (will retry up to %d times)",
                mask_phone(booking.phone_number),
                MAX_REMINDER_ATTEMPTS,
            )

    logger.info(
        "Reminder run: %d sent, %d failed, %d candidates",
        sent, failed, len(candidates),
    )


def start():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        send_reminders,
        trigger=CronTrigger(hour="9-17", minute=0, timezone=settings.TIME_ZONE),
        id="send_reminders",
        name="Send booking reminders",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Scheduler started (hourly 09-17, tz=%s)", settings.TIME_ZONE)