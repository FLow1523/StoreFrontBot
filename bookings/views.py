import json
import logging
import os
import re
from datetime import datetime, timedelta
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from inventory.models import Product
from .metrics import ALLOWED_RANGES, compute_metrics, parse_range
from .models import Booking, TimeSlot

logger = logging.getLogger(__name__)

# Adjust this set if your dashboard uses other status values.
VALID_BOOKING_STATUSES = {"pending", "confirmed", "cancelled", "completed"}
MAX_STOCK_QUANTITY = 1_000_000

# Time slot limits
MAX_SLOTS_PER_REQUEST = 48
MIN_SLOT_INTERVAL_MINUTES = 5
MAX_SLOT_INTERVAL_MINUTES = 480
DEFAULT_SLOT_INTERVAL_MINUTES = 60
MAX_SLOTS_ON_DASHBOARD = 200


def staff_api_required(view_func):
    """
    Allow only logged-in staff users. Unlike staff_member_required, this
    returns a JSON 403 instead of redirecting to the login page, which is
    what a fetch() call from the dashboard can handle.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not (user.is_authenticated and user.is_active and user.is_staff):
            return JsonResponse({"success": False, "error": "Forbidden"}, status=403)
        return view_func(request, *args, **kwargs)
    return wrapper


def parse_json_body(request):
    """Return the request body as a dict, or None if it is not a JSON object."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def parse_quantity(value):
    """Return a valid non-negative integer quantity, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return None
    if quantity < 0 or quantity > MAX_STOCK_QUANTITY:
        return None
    return quantity


def parse_interval(value):
    """Return a valid slot interval in minutes, or None."""
    if value in (None, ""):
        return DEFAULT_SLOT_INTERVAL_MINUTES
    if isinstance(value, bool):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    if minutes < MIN_SLOT_INTERVAL_MINUTES or minutes > MAX_SLOT_INTERVAL_MINUTES:
        return None
    return minutes


def to_aware(naive):
    """Interpret a naive datetime in the project time zone (settings.TIME_ZONE)."""
    return timezone.make_aware(naive) if settings.USE_TZ else naive


def apply_booking_status(booking, status):
    """
    Change a booking's status and keep its time slot in step:
    cancelling frees the slot, and un-cancelling claims it again.
    Returns (ok, slot_changed). ok is False if the slot was taken meanwhile.
    """
    slot_changed = False
    with transaction.atomic():
        if booking.slot_id and status != booking.status:
            if status == "cancelled":
                TimeSlot.objects.filter(pk=booking.slot_id).update(is_booked=False)
                slot_changed = True
            elif booking.status == "cancelled":
                claimed = TimeSlot.objects.filter(
                    pk=booking.slot_id, is_booked=False
                ).update(is_booked=True)
                if not claimed:
                    return False, False
                slot_changed = True
        booking.status = status
        booking.save(update_fields=["status"])
    return True, slot_changed


def landing(request):
    # Contact buttons appear only when these are set (environment variables on Render).
    number = re.sub(r"\D", "", os.getenv("LANDING_WHATSAPP_NUMBER", ""))
    email = (os.getenv("CONTACT_EMAIL") or "").strip()
    return render(request, 'landing/index.html', {
        'whatsapp_number': number if 8 <= len(number) <= 15 else None,
        'contact_email': email if re.fullmatch(r"[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+", email) else None,
        'site_url': settings.SITE_URL,
    })


@staff_member_required
@ensure_csrf_cookie
def dashboard(request):
    bookings = Booking.objects.all().order_by('-created_at')
    total_bookings = bookings.count()
    pending = bookings.filter(status='pending').count()
    confirmed = bookings.filter(status='confirmed').count()
    now = timezone.now()
    slots = (
        TimeSlot.objects.filter(start__gte=now)
        .order_by('start')
        .prefetch_related('bookings')[:MAX_SLOTS_ON_DASHBOARD]
    )
    open_slots = TimeSlot.objects.filter(start__gt=now, is_booked=False).count()
    return render(request, 'dashboard.html', {
        'active_page': 'bookings',
        'bookings': bookings,
        'total_bookings': total_bookings,
        'pending': pending,
        'confirmed': confirmed,
        'slots': slots,
        'open_slots': open_slots,
    })


@staff_member_required
@ensure_csrf_cookie
def product_list(request):
    products = Product.objects.all().order_by('name')
    total_products = products.count()
    in_stock = products.filter(quantity__gt=0).count()
    return render(request, 'products.html', {
        'active_page': 'products',
        'products': products,
        'total_products': total_products,
        'in_stock': in_stock,
        'out_of_stock': total_products - in_stock,
    })


@staff_member_required
def metrics_page(request):
    days = parse_range(request.GET.get('days'))
    return render(request, 'metrics.html', {
        'active_page': 'metrics',
        'days': days,
        'ranges': ALLOWED_RANGES,
        'm': compute_metrics(days),
    })


@staff_api_required
@require_POST
def update_booking(request, booking_id):
    data = parse_json_body(request)
    if data is None:
        return JsonResponse({"success": False, "error": "Invalid request body"}, status=400)

    status = data.get("status")
    if status not in VALID_BOOKING_STATUSES:
        return JsonResponse({"success": False, "error": "Invalid status"}, status=400)

    booking = get_object_or_404(Booking, id=booking_id)
    ok, slot_changed = apply_booking_status(booking, status)
    if not ok:
        return JsonResponse(
            {"success": False, "error": "That time slot has since been booked by someone else"},
            status=409,
        )
    logger.info("Booking %s set to %s by user %s", booking.id, status, request.user.id)
    return JsonResponse(
        {"success": True, "status": booking.status, "slot_changed": slot_changed}
    )


@staff_api_required
@require_POST
def update_stock(request, product_id):
    data = parse_json_body(request)
    if data is None:
        return JsonResponse({"success": False, "error": "Invalid request body"}, status=400)

    quantity = parse_quantity(data.get("quantity"))
    if quantity is None:
        return JsonResponse({"success": False, "error": "Invalid quantity"}, status=400)

    product = get_object_or_404(Product, id=product_id)
    product.quantity = quantity
    product.save(update_fields=["quantity", "updated_at"])
    logger.info("Product %s quantity set to %s by user %s", product.id, quantity, request.user.id)
    return JsonResponse({"success": True, "quantity": product.quantity})


@staff_api_required
@require_POST
def add_slots(request):
    """
    Add one slot, or a run of slots for a single day.
    Body: {"date": "YYYY-MM-DD", "start_time": "HH:MM",
           "end_time": "HH:MM" (optional), "interval_minutes": 60 (optional)}
    Times are read in the project time zone.
    """
    data = parse_json_body(request)
    if data is None:
        return JsonResponse({"success": False, "error": "Invalid request body"}, status=400)

    end_raw = data.get("end_time") or ""
    try:
        day = datetime.strptime(data.get("date"), "%Y-%m-%d").date()
        first = datetime.strptime(data.get("start_time"), "%H:%M").time()
        last = datetime.strptime(end_raw, "%H:%M").time() if end_raw else None
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Enter a valid date and time"}, status=400)

    first_dt = datetime.combine(day, first)
    naive_times = [first_dt]
    if last is not None:
        interval = parse_interval(data.get("interval_minutes"))
        if interval is None:
            return JsonResponse(
                {
                    "success": False,
                    "error": f"Interval must be between {MIN_SLOT_INTERVAL_MINUTES} and {MAX_SLOT_INTERVAL_MINUTES} minutes",
                },
                status=400,
            )
        last_dt = datetime.combine(day, last)
        if last_dt < first_dt:
            return JsonResponse(
                {"success": False, "error": "The last time must be after the first time"},
                status=400,
            )
        naive_times = []
        current = first_dt
        while current <= last_dt:
            naive_times.append(current)
            if len(naive_times) > MAX_SLOTS_PER_REQUEST:
                return JsonResponse(
                    {
                        "success": False,
                        "error": f"That would create more than {MAX_SLOTS_PER_REQUEST} slots. Use a larger interval or a shorter range",
                    },
                    status=400,
                )
            current += timedelta(minutes=interval)

    try:
        aware_times = [to_aware(dt) for dt in naive_times]
    except (ValueError, OverflowError):
        return JsonResponse({"success": False, "error": "Enter a valid date and time"}, status=400)

    now = timezone.now()
    future_times = [dt for dt in aware_times if dt > now]
    past_count = len(aware_times) - len(future_times)
    if not future_times:
        return JsonResponse(
            {"success": False, "error": "Those times are in the past"}, status=400
        )

    existing = set(
        TimeSlot.objects.filter(start__in=future_times).values_list("start", flat=True)
    )
    new_times = [dt for dt in future_times if dt not in existing]
    if new_times:
        TimeSlot.objects.bulk_create(
            [TimeSlot(start=dt) for dt in new_times], ignore_conflicts=True
        )
    logger.info("%d time slot(s) added by user %s", len(new_times), request.user.id)
    return JsonResponse(
        {
            "success": True,
            "created": len(new_times),
            "existing": len(future_times) - len(new_times),
            "past": past_count,
        }
    )


@staff_api_required
@require_POST
def delete_slot(request, slot_id):
    """Remove an open slot. Booked slots must be freed by cancelling the booking."""
    deleted, _ = TimeSlot.objects.filter(pk=slot_id, is_booked=False).delete()
    if deleted:
        logger.info("Time slot %s removed by user %s", slot_id, request.user.id)
        return JsonResponse({"success": True})
    if TimeSlot.objects.filter(pk=slot_id).exists():
        return JsonResponse(
            {"success": False, "error": "This slot is booked. Cancel the booking first"},
            status=409,
        )
    return JsonResponse({"success": False, "error": "Slot not found"}, status=404)