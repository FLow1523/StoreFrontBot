import logging
import re
from collections import Counter
from datetime import datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

from .models import Booking, TimeSlot

logger = logging.getLogger(__name__)

ALLOWED_RANGES = (7, 30, 90)
DEFAULT_RANGE = 30
MESSAGE_WINDOW_DAYS = 7      # the bot only keeps message records for this long
MAX_ROWS = 5000
TOP_TIMES = 5
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def parse_range(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RANGE
    return days if days in ALLOWED_RANGES else DEFAULT_RANGE


def digits(number):
    return re.sub(r"\D", "", str(number or ""))


def pct(part, whole):
    """A whole-number percentage, or None when there is nothing to divide by."""
    return round(100 * part / whole) if whole else None


def local(value):
    return timezone.localtime(value) if timezone.is_aware(value) else value


def midnight(day):
    naive = datetime.combine(day, time.min)
    return timezone.make_aware(naive) if settings.USE_TZ else naive


def bars(counter, keys, key_name="name"):
    """Rows with a bar width relative to the biggest value."""
    peak = max((counter.get(k, 0) for k in keys), default=0)
    return [
        {key_name: k, "count": counter.get(k, 0), "bar": round(100 * counter.get(k, 0) / peak) if peak else 0}
        for k in keys
    ]


def bot_activity(now):
    """
    Conversation numbers for the last 7 days, or None if they cannot be read.
    Booking conversion = customers who messaged AND booked, out of customers who messaged.
    """
    try:
        from chatbot.models import ProcessedMessage
        from chatbot.views import hash_sender

        since = now - timedelta(days=MESSAGE_WINDOW_DAYS)
        rows = ProcessedMessage.objects.filter(created_at__gte=since)
        senders = set(rows.values_list("sender_hash", flat=True))
        booked = {
            digits(p)
            for p in Booking.objects.filter(created_at__gte=since).values_list("phone_number", flat=True)
        } - {""}
        booked_hashes = {hash_sender(f"+{d}") for d in booked}
        converted = len(senders & booked_hashes)
        return {
            "days": MESSAGE_WINDOW_DAYS,
            "messages": rows.count(),
            "customers": len(senders),
            "booked": converted,
            "conversion": pct(converted, len(senders)),
        }
    except Exception:
        logger.exception("Could not read bot activity for the metrics page")
        return None


def compute_metrics(days=DEFAULT_RANGE, now=None):
    now = now or timezone.now()
    today = local(now).date()
    start_day = today - timedelta(days=days - 1)
    since = midnight(start_day)

    bookings = list(
        Booking.objects.filter(created_at__gte=since)
        .select_related("slot")
        .order_by("created_at")[:MAX_ROWS]
    )
    made = len(bookings)
    cancelled = sum(1 for b in bookings if b.status == "cancelled")

    phones = {digits(b.phone_number) for b in bookings} - {""}
    earlier = {
        digits(p) for p in Booking.objects.filter(created_at__lt=since).values_list("phone_number", flat=True)
    } - {""}

    # bookings made per day
    per_day = Counter(local(b.created_at).date() for b in bookings)
    day_list = [start_day + timedelta(days=i) for i in range(days)]
    peak = max(per_day.values(), default=0)
    daily = [
        {
            "label": d.strftime("%d %b"),
            "count": per_day.get(d, 0),
            "pct": round(100 * per_day.get(d, 0) / peak) if peak else 0,
        }
        for d in day_list
    ]

    # what was booked
    service_counts = Counter(b.service for b in bookings)
    services = [
        {"name": name, "count": count, "share": pct(count, made), "bar": pct(count, made)}
        for name, count in sorted(service_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    # when the appointments are (active bookings only)
    weekday_counts, time_counts = Counter(), Counter()
    for b in bookings:
        if b.slot_id and b.status != "cancelled":
            start = local(b.slot.start)
            weekday_counts[WEEKDAYS[start.weekday()]] += 1
            time_counts[f"{start:%H}:00"] += 1
    weekdays = bars(weekday_counts, WEEKDAYS)
    top_times = sorted(time_counts, key=lambda t: (-time_counts[t], t))[:TOP_TIMES]
    times = bars(time_counts, top_times)

    # what is coming up
    upcoming = Booking.objects.filter(
        status__in=("pending", "confirmed"), slot__start__gt=now
    ).count()
    slots_total = TimeSlot.objects.filter(start__gt=now).count()
    slots_booked = TimeSlot.objects.filter(start__gt=now, is_booked=True).count()

    return {
        "days": days,
        "first_day": day_list[0].strftime("%d %b"),
        "last_day": day_list[-1].strftime("%d %b"),
        "made": made,
        "cancelled": cancelled,
        "cancel_rate": pct(cancelled, made),
        "customers": len(phones),
        "returning": len(phones & earlier),
        "upcoming": upcoming,
        "slots_total": slots_total,
        "slots_booked": slots_booked,
        "fill_rate": pct(slots_booked, slots_total),
        "daily": daily,
        "peak_day": peak,
        "services": services,
        "weekdays": weekdays,
        "times": times,
        "bot": bot_activity(now),
    }