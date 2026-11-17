import logging
from datetime import datetime, time, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_REMINDER_ATTEMPTS = 3


def send_reminders():
    """
    Send reminders for bookings whose slot falls on tomorrow (local date).

    Correctness guarantees:
      - Only confirmed bookings with a slot and a phone number are considered.
      - The date range is queried in the database, not matched by string.
      - Each reminder is atomically claimed via reminder_sent_at, so two
        workers cannot both send the same reminder.
      - A failed send is retried on the next run, up to MAX_REMINDER_ATTEMPTS.
    """
    from bookings.branding import site_name
    from bookings.models import Booking
    from chatbot.views import mask_phone, send_message

    local_now = timezone.localtime(timezone.now())
    tomorrow_date = (local_now + timedelta(days=1)).date()
    tomorrow_start = timezone.make_aware(
        datetime.combine(tomorrow_date, time.min), timezone.get_current_timezone()
    )
    tomorrow_end = tomorrow_start + timedelta(days=1)

    candidates = (
        Booking.objects.filter(
            status="confirmed",
            slot__isnull=False,
            slot__start__gte=tomorrow_start,
            slot__start__lt=tomorrow_end,
            reminder_sent_at__isnull=True,
            reminder_attempts__lt=MAX_REMINDER_ATTEMPTS,
        )
        .exclude(phone_number="")
        .select_related("slot")
    )

    brand = site_name()
    sent = 0
    for booking in candidates:
        # Atomic claim: only one worker can set reminder_sent_at from NULL.
        claimed = Booking.objects.filter(
            pk=booking.pk, reminder_sent_at__isnull=True
        ).update(reminder_sent_at=timezone.now())
        if not claimed:
            continue

        message = (
            f"Hello {booking.name},\n\n"
            f"This is a reminder from {brand}.\n\n"
            f"You have a {booking.service} appointment tomorrow:\n"
            f"Date: {booking.date}\n"
            f"Time: {booking.time}\n\n"
            f"If you need to reschedule, please message us."
        )
        try:
            send_message(booking.phone_number, message)
            sent += 1
            logger.info("Reminder sent to %s", mask_phone(booking.phone_number))
        except Exception:
            # Reset so the next run retries, and record the failed attempt.
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
        "Reminder run for %s: %d sent, %d candidates",
        tomorrow_date.isoformat(),
        sent,
        candidates.count(),
    )


def start():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        send_reminders,
        trigger=CronTrigger(hour=9, minute=0, timezone=settings.TIME_ZONE),
        id="send_reminders",
        name="Send booking reminders",
        replace_existing=True,
        misfire_grace_time=3600,   # allow late start if the worker boots at 9:05
        coalesce=True,             # collapse multiple missed runs into one
    )
    scheduler.start()
    logger.info("Scheduler started (cron hour=9, tz=%s)", settings.TIME_ZONE)