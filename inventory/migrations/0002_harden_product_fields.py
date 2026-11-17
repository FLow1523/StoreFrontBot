from decimal import Decimal

import django.core.validators
from django.db import migrations, models


def coerce_negatives(apps, schema_editor):
    """
    Before the PositiveIntegerField CHECK is added, clean up any existing
    negative rows so the migration cannot fail on a populated database.
    """
    Product = apps.get_model("inventory", "Product")
    Product.objects.filter(quantity__lt=0).update(quantity=0)
    Product.objects.filter(price__lt=0).update(price=Decimal("0.00"))


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0001_initial"),
    ]

    operations = [
        # 1. Clean up any existing negative data first.
        migrations.RunPython(coerce_negatives, migrations.RunPython.noop),
        # 2. Change quantity to PositiveIntegerField (adds DB CHECK).
        migrations.AlterField(
            model_name="product",
            name="quantity",
            field=models.PositiveIntegerField(default=0),
        ),
        # 3. Add the price validator (recorded for form/admin validation).
        migrations.AlterField(
            model_name="product",
            name="price",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=10,
                validators=[django.core.validators.MinValueValidator(Decimal("0.00"))],
            ),
        ),
        # 4. Record the new default ordering.
        migrations.AlterModelOptions(
            name="product",
            options={"ordering": ["name"]},
        ),
    ]