from django.contrib import admin

from .models import SupportRequest


@admin.register(SupportRequest)
class SupportRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "phone_digits", "short_message", "handled")
    list_filter = ("handled",)
    list_editable = ("handled",)
    search_fields = ("phone_digits", "message")
    readonly_fields = ("phone_digits", "message", "created_at")

    @admin.display(description="Message")
    def short_message(self, obj):
        return obj.message[:80]