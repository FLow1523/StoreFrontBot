from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class Product(models.Model):
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    # PositiveIntegerField adds a DB-level CHECK (quantity >= 0) on every
    # backend Django supports, so negative stock can never be stored even
    # by a direct admin edit or a future code path that bypasses the API.
    quantity = models.PositiveIntegerField(default=0)
    # DecimalField has no "positive" variant, so the validator must be
    # explicit. This is enforced by full_clean() (admin, forms, shell).
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} - {self.quantity} in stock"