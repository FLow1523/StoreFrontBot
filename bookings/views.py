from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.admin.views.decorators import staff_member_required
from .models import Booking
from inventory.models import Product
import json

@staff_member_required
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

@csrf_exempt
def update_booking(request, booking_id):
    if request.method == "POST":
        booking = get_object_or_404(Booking, id=booking_id)
        data = json.loads(request.body)
        booking.status = data.get("status", booking.status)
        booking.save()
        return JsonResponse({"success": True, "status": booking.status})

@csrf_exempt
def update_stock(request, product_id):
    if request.method == "POST":
        product = get_object_or_404(Product, id=product_id)
        data = json.loads(request.body)
        product.quantity = data.get("quantity", product.quantity)
        product.save()
        return JsonResponse({"success": True, "quantity": product.quantity})