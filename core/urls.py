from django.contrib import admin
from django.urls import path, include
from bookings.views import dashboard, update_booking, update_stock, landing

urlpatterns = [
    path('admin/', admin.site.urls),
    path('chatbot/', include('chatbot.urls')),
    path('dashboard/', dashboard, name='dashboard'),
    path('dashboard/booking/<int:booking_id>/update/', update_booking),
    path('dashboard/stock/<int:product_id>/update/', update_stock),
    path('', landing, name='landing'),
]