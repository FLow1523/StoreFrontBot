from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from twilio.base.exceptions import TwilioRestException

from bookings.models import Booking, TimeSlot
from chatbot import scheduler, views
from chatbot.models import SupportRequest

NUMBER = "+264811111111"


def model_says(reply, show_menu=False, action="none", booking_data=None):
    return {
        "reply": reply,
        "action": action,
        "show_menu": show_menu,
        "booking_data": booking_data or {"name": None, "service": None},
    }


# ---------------------------------------------------------------- reminders
class ReminderTests(TestCase):
    def make_booking(self, age_hours=5):
        slot = TimeSlot.objects.create(
            start=timezone.now() + timedelta(hours=20), is_booked=True
        )
        booking = Booking.objects.create(
            name="Test", phone_number=NUMBER, date="d", time="t",
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


# --------------------------------------------------------- conversation flow
class ChatFlowTests(TestCase):
    def say(self, text, model=None):
        """Send one customer message. The model is mocked; code paths never call it."""
        with mock.patch("chatbot.views.ask_groq", return_value=model) as groq:
            reply = views.build_reply(NUMBER, text)
        return reply, groq

    # business questions are answered directly, with no menu
    def test_business_answer_has_no_menu(self):
        reply, _ = self.say(
            "how many laptops are available?",
            model_says("Yes, we have 4 laptops at N$10,000 each."),
        )
        self.assertEqual(reply, "Yes, we have 4 laptops at N$10,000 each.")

    def test_menu_leaked_by_model_is_removed(self):
        leaked = (
            "We have 4 laptops.\n\nI can also help you with:\n\n"
            "1. Make a booking\n2. Check stock availability\n3. Speak to support\n\n"
            "Reply with a number or let me know how I may assist you."
        )
        reply, _ = self.say("laptops?", model_says(leaked))
        self.assertEqual(reply, "We have 4 laptops.")

    # only unrelated messages pull up the menu
    def test_unrelated_message_gets_menu(self):
        reply, _ = self.say(
            "what is the capital of France?",
            model_says("I can only help with questions about the business.", show_menu=True),
        )
        self.assertTrue(reply.startswith("I can only help"))
        self.assertIn("1. Make a booking", reply)

    def test_menu_word_skips_the_model(self):
        reply, groq = self.say("menu")
        groq.assert_not_called()
        self.assertEqual(reply, views.MENU_REPLY)

    def test_menu_number_one_gives_services(self):
        self.say("menu")
        reply, groq = self.say("1")
        groq.assert_not_called()
        self.assertEqual(reply, views.SERVICES_REPLY)

    # booking
    def test_full_booking_flow(self):
        TimeSlot.objects.create(start=timezone.now() + timedelta(days=1))
        reply, _ = self.say(
            "Anna",
            model_says(
                "ok",
                action="show_slots",
                booking_data={"name": "Anna", "service": "Consultation"},
            ),
        )
        self.assertIn("1.", reply)
        confirmation, groq = self.say("1")
        groq.assert_not_called()
        self.assertIn("Booking confirmed.", confirmation)
        self.assertEqual(Booking.objects.get().name, "Anna")

    def test_booking_request_gets_services_without_the_model(self):
        reply, groq = self.say("I want to book")
        groq.assert_not_called()
        self.assertEqual(reply, views.SERVICES_REPLY)

    def test_leaving_the_slot_list_with_menu_shows_menu(self):
        TimeSlot.objects.create(start=timezone.now() + timedelta(days=1))
        self.say(
            "Anna",
            model_says(
                "ok",
                action="show_slots",
                booking_data={"name": "Anna", "service": "Consultation"},
            ),
        )
        reply, _ = self.say("menu")
        self.assertEqual(reply, views.MENU_REPLY)

    # support
    def test_support_request_shows_options(self):
        reply, groq = self.say("speak to support")
        groq.assert_not_called()
        self.assertIn("Leave a message", reply)

    def test_leave_a_message_flow(self):
        self.say("speak to support")
        reply, _ = self.say("1")
        self.assertIn("type your message", reply)
        reply, _ = self.say("my order is late")
        self.assertIn("Thank you", reply)
        self.assertEqual(SupportRequest.objects.get().message, "my order is late")

    def test_typing_a_message_straight_away_is_saved(self):
        self.say("I need a human")
        self.say("please call me about my invoice")
        self.assertEqual(SupportRequest.objects.count(), 1)

    def test_zero_goes_back_to_menu(self):
        self.say("speak to support")
        reply, _ = self.say("0")
        self.assertEqual(reply, views.MENU_REPLY)
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_menu_option_three_opens_support(self):
        self.say("menu")
        reply, _ = self.say("3")
        self.assertIn("Leave a message", reply)


class MoneyTests(TestCase):
    def test_whole_amounts_have_no_decimals(self):
        self.assertEqual(views.money(10000), "N$10,000")
        self.assertEqual(views.money("259.00"), "N$259")

    def test_cents_are_kept(self):
        self.assertEqual(views.money("259.5"), "N$259.50")