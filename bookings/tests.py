import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from inventory.models import Product

from .models import Booking, TimeSlot


class SecurityTests(TestCase):
    """Verify the dashboard JSON endpoints enforce auth, CSRF, and validation."""

    def setUp(self):
        self.staff = User.objects.create_user(
            "staff", password="x", is_staff=True
        )
        self.non_staff = User.objects.create_user("visitor", password="x")
        self.product = Product.objects.create(name="Widget", price=10, quantity=5)
        self.slot = TimeSlot.objects.create(
            start=timezone.now() + timedelta(days=1)
        )
        self.booking = Booking.objects.create(
            name="Test",
            phone_number="+264811111111",
            date="2026-01-01",
            time="10:00",
            status="pending",
            slot=self.slot,
        )

    # ── Auth ────────────────────────────────────────────

    def test_stock_update_requires_staff(self):
        self.client.force_login(self.non_staff)
        r = self.client.post(
            f"/dashboard/stock/{self.product.id}/update/",
            data=json.dumps({"quantity": 0}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.json()["success"])

    def test_stock_update_rejects_anonymous(self):
        r = self.client.post(
            f"/dashboard/stock/{self.product.id}/update/",
            data=json.dumps({"quantity": 0}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)

    def test_booking_update_requires_staff(self):
        self.client.force_login(self.non_staff)
        r = self.client.post(
            f"/dashboard/booking/{self.booking.id}/update/",
            data=json.dumps({"status": "cancelled"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)

    # ── Method ──────────────────────────────────────────

    def test_get_is_rejected(self):
        self.client.force_login(self.staff)
        r = self.client.get(f"/dashboard/stock/{self.product.id}/update/")
        self.assertEqual(r.status_code, 405)

    # ── CSRF ────────────────────────────────────────────

    def test_csrf_required(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.staff)
        r = c.post(
            f"/dashboard/stock/{self.product.id}/update/",
            data=json.dumps({"quantity": 1}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)

    # ── Validation ──────────────────────────────────────

    def test_status_whitelist(self):
        self.client.force_login(self.staff)
        for bad in ["hacked", "", None, "CONFIRMED", "confirmed "]:
            r = self.client.post(
                f"/dashboard/booking/{self.booking.id}/update/",
                data=json.dumps({"status": bad}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 400, f"accepted status={bad!r}")

    def test_status_change_frees_slot(self):
        self.slot.is_booked = True
        self.slot.save()
        self.client.force_login(self.staff)
        r = self.client.post(
            f"/dashboard/booking/{self.booking.id}/update/",
            data=json.dumps({"status": "cancelled"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.slot.refresh_from_db()
        self.assertFalse(self.slot.is_booked)

    def test_quantity_rejects_bool(self):
        self.client.force_login(self.staff)
        r = self.client.post(
            f"/dashboard/stock/{self.product.id}/update/",
            data=json.dumps({"quantity": True}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_quantity_rejects_negative(self):
        self.client.force_login(self.staff)
        r = self.client.post(
            f"/dashboard/stock/{self.product.id}/update/",
            data=json.dumps({"quantity": -5}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_quantity_rejects_huge(self):
        self.client.force_login(self.staff)
        r = self.client.post(
            f"/dashboard/stock/{self.product.id}/update/",
            data=json.dumps({"quantity": 10_000_001}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_interval_rejects_out_of_range(self):
        self.client.force_login(self.staff)
        for bad in [0, 4, 481, 9999]:
            r = self.client.post(
                "/dashboard/slots/add/",
                data=json.dumps({
                    "date": "2030-01-01",
                    "start_time": "09:00",
                    "end_time": "17:00",
                    "interval_minutes": bad,
                }),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 400, f"accepted interval={bad}")

    def test_slot_add_rejects_bad_date(self):
        self.client.force_login(self.staff)
        r = self.client.post(
            "/dashboard/slots/add/",
            data=json.dumps({"date": "not-a-date", "start_time": "09:00"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_delete_booked_slot_refused(self):
        self.slot.is_booked = True
        self.slot.save()
        self.client.force_login(self.staff)
        r = self.client.post(f"/dashboard/slots/{self.slot.id}/delete/")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(TimeSlot.objects.filter(pk=self.slot.id).exists())