from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import TestCase

from .models import Product


class ProductModelTests(TestCase):
    """Guardrails for stock levels and prices."""

    def test_quantity_accepts_zero(self):
        p = Product.objects.create(name="A", quantity=0, price=Decimal("10.00"))
        self.assertEqual(p.quantity, 0)

    def test_quantity_accepts_positive(self):
        p = Product.objects.create(name="A", quantity=100, price=Decimal("10.00"))
        self.assertEqual(p.quantity, 100)

    def test_quantity_rejects_negative_via_full_clean(self):
        p = Product(name="A", quantity=-1, price=Decimal("10.00"))
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_quantity_rejects_negative_at_db_level(self):
        """PositiveIntegerField adds a DB CHECK; ORM insert of -1 must fail."""
        # Some backends raise IntegrityError; Django raises ValidationError
        # for the same input on others. Accept either as the constraint firing.
        with self.assertRaises((IntegrityError, ValidationError)):
            Product.objects.create(name="A", quantity=-1, price=Decimal("10.00"))

    def test_price_rejects_negative_via_full_clean(self):
        p = Product(name="A", quantity=1, price=Decimal("-0.01"))
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_price_accepts_zero(self):
        p = Product(name="A", quantity=1, price=Decimal("0.00"))
        p.full_clean()

    def test_str(self):
        p = Product.objects.create(
            name="Widget", quantity=3, price=Decimal("5.00")
        )
        self.assertEqual(str(p), "Widget - 3 in stock")

    def test_default_ordering(self):
        Product.objects.create(name="Zebra", quantity=1, price=Decimal("1"))
        Product.objects.create(name="Apple", quantity=1, price=Decimal("1"))
        names = list(Product.objects.values_list("name", flat=True))
        self.assertEqual(names, ["Apple", "Zebra"])