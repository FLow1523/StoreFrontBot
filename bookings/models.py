from django.db import models

class Booking(models.Model):
    name = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=20)
    date = models.CharField(max_length=50)
    time = models.CharField(max_length=50)
    service = models.CharField(max_length=100, default="General")
    status = models.CharField(max_length=20, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.date} at {self.time}"