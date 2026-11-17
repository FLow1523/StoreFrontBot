from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0005_booking_indexes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chatsession",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, db_index=True),
        ),
    ]