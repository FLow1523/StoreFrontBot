from django.contrib import admin
from django.urls import path, include
from core.seo import robots_txt, sitemap_xml
from bookings.views import (
    dashboard,
    update_booking,
    update_stock,
    landing,
    add_slots,
    delete_slot,
    product_list,
    metrics_page,
)

urlpatterns = [
    path('robots.txt', robots_txt, name='robots-txt'),
    path('sitemap.xml', sitemap_xml, name='sitemap-xml'),
    path('admin/', admin.site.urls),
    path('chatbot/', include('chatbot.urls')),
    path('dashboard/', dashboard, name='dashboard'),
    path('dashboard/products/', product_list, name='dashboard-products'),
    path('dashboard/metrics/', metrics_page, name='dashboard-metrics'),
    path('dashboard/booking/<int:booking_id>/update/', update_booking),
    path('dashboard/stock/<int:product_id>/update/', update_stock),
    path('dashboard/slots/add/', add_slots),
    path('dashboard/slots/<int:slot_id>/delete/', delete_slot),
    path('', landing, name='landing'),
]