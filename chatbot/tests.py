from django.test import TestCase

# Create your tests here.
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from twilio.base.exceptions import TwilioRestException

from bookings.models import Booking, TimeSlot
from chatbot import scheduler


class ReminderTests(TestCase):
    def make_booking(self, age_hours=5):
        slot = TimeSlot.objects.create(
            start=timezone.now() + timedelta(hours=20), is_booked=True
        )
        booking = Booking.objects.create(
            name="Test", phone_number="+264811111111", date="d", time="t",
            status="confirmed", slot=slot,
        )
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(hours=age_hours)
        )
        return booking

    def test_reminder_is_sent_once(self):
        booking = self.make_booking()
        with mock.patch("chatbot.views.send_message") as send:
            scheduler.send_reminders()
            scheduler.send_reminders()
        send.assert_called_once()
        booking.refresh_from_db()
        self.assertIsNotNone(booking.reminder_sent_at)

    def test_failed_send_is_retried_next_run(self):
        booking = self.make_booking()
        with mock.patch("chatbot.views.send_message", side_effect=RuntimeError("boom")):
            scheduler.send_reminders()
        booking.refresh_from_db()
        self.assertEqual(booking.reminder_attempts, 1)
        self.assertIsNone(booking.reminder_sent_at)
        with mock.patch("chatbot.views.send_message") as send:
            scheduler.send_reminders()
        send.assert_called_once()
        booking.refresh_from_db()
        self.assertIsNotNone(booking.reminder_sent_at)

    def test_new_booking_is_not_reminded_yet(self):
        self.make_booking(age_hours=0)
        with mock.patch("chatbot.views.send_message") as send:
            scheduler.send_reminders()
        send.assert_not_called()

    @mock.patch.dict("os.environ", {"REMINDER_TEMPLATE_SID": "HX123"})
    def test_template_used_outside_24h_window(self):
        booking = self.make_booking()
        err = TwilioRestException(400, "uri", msg="outside window", code=63016)
        with mock.patch("chatbot.views.send_message", side_effect=err), \
             mock.patch("chatbot.views.send_template_message") as template:
            scheduler.send_reminders()
        template.assert_called_once()
        booking.refresh_from_db()
        self.assertIsNotNone(booking.reminder_sent_at)