from django.contrib import admin

from .models import Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "quantity", "price", "updated_at")
    list_editable = ("quantity", "price")
    search_fields = ("name", "description")
    list_filter = ("quantity",)
    ordering = ("name",)

    def get_readonly_fields(self, request, obj=None):
        # updated_at is set by auto_now; never allow manual editing.
        return ("updated_at",)