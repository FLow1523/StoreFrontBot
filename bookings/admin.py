from django.contrib import admin

from .models import Booking, ChatSession, TimeSlot

admin.site.register(Booking)
admin.site.register(TimeSlot)


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ("phone_digits", "updated_at")
    search_fields = ("phone_digits",)
    readonly_fields = ("data",)