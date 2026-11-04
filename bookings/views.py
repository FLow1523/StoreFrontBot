import json
import logging
from functools import wraps

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from inventory.models import Product
from .models import Booking

logger = logging.getLogger(__name__)

# Adjust this set if your dashboard uses other status values.
VALID_BOOKING_STATUSES = {"pending", "confirmed", "cancelled", "completed"}
MAX_STOCK_QUANTITY = 1_000_000


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


def landing(request):
    return render(request, 'landing/index.html')


@staff_member_required
@ensure_csrf_cookie
def dashboard(request):
    bookings = Booking.objects.all().order_by('-created_at')
    products = Product.objects.all()
    total_bookings = bookings.count()
    pending = bookings.filter(status='pending').count()
    confirmed = bookings.filter(status='confirmed').count()
    return render(request, 'dashboard.html', {
        'bookings': bookings,
        'products': products,
        'total_bookings': total_bookings,
        'pending': pending,
        'confirmed': confirmed,
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
    booking.status = status
    booking.save(update_fields=["status"])
    logger.info("Booking %s set to %s by user %s", booking.id, status, request.user.id)
    return JsonResponse({"success": True, "status": booking.status})


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