from django.db import models


class TimeSlot(models.Model):
    """A time the owner has made available. Customers can only book these."""
    start = models.DateTimeField(unique=True)
    is_booked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start"]

    def __str__(self):
        state = "booked" if self.is_booked else "open"
        return f"{self.start:%a %d %b %Y %H:%M} ({state})"


class Booking(models.Model):
    name = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=50)
    # date and time stay as text so existing bookings and the dashboard table
    # keep working. For slot bookings they are filled in from the slot.
    date = models.CharField(max_length=50)
    time = models.CharField(max_length=50)
    service = models.CharField(max_length=100, default="General")
    status = models.CharField(max_length=20, default="pending")
    slot = models.ForeignKey(
        TimeSlot,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bookings",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.date} at {self.time}"