from django.db import models


class ProcessedMessage(models.Model):
    """
    Records every WhatsApp message ID the webhook has accepted so that Meta
    retries are not processed twice, and provides the data for per-sender
    rate limiting. Message content is deliberately not stored, and the sender
    is stored only as a keyed hash.
    """
    message_id = models.CharField(max_length=255, unique=True)
    sender_hash = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return self.message_id