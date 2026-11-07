from django.urls import path

from . import legal, views

urlpatterns = [
    path('webhook/', views.webhook, name='webhook'),
    path('privacy/', legal.privacy_policy, name='privacy-policy'),
    path('data-deletion/', legal.data_deletion, name='data-deletion'),
]