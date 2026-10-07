"""Pacific admin display, DST transitions and local-day boundaries.

Accounts and data are temporary; no external messages or billing are used.
"""
import csv
import io
import secrets
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import admin_time
import email_engagement
import growth
import setup_help

WINTER = "2026-01-15T02:30:00+00:00"
SUMMER = "2026-07-15T02:30:00Z"
WINTER_DISPLAY = "2026-01-14 6:30:00 PM PST"
SUMMER_DISPLAY = "2026-07-14 7:30:00 PM PDT"
ZONE_NOTE = "Dates and times use Pacific Time (PST/PDT)."


class PacificTimeTest(unittest.TestCase):
    def test_winter_summer_and_utc_date_rollover(self):
        self.assertEqual(admin_time.pacific_time(WINTER), WINTER_DISPLAY)
        self.assertEqual(admin_time.pacific_time(SUMMER), SUMMER_DISPLAY)
        self.assertEqual(admin_time.pacific_date(WINTER), "2026-01-14")
        self.assertEqual(admin_time.pacific_date(SUMMER), "2026-07-14")

    def test_naive_utc_offsets_and_datetime_values(self):
        for value in ("2026-01-15T02:30:00", "2026-01-15 02:30:00Z",
                      "2026-01-15T04:30:00+02:00",
                      datetime(2026, 1, 15, 2, 30),
                      datetime(2026, 1, 15, 2, 30, tzinfo=timezone.utc)):
            with self.subTest(value=value):
                self.assertEqual(admin_time.pacific_time(value), WINTER_DISPLAY)

    def test_dst_spring_jump_and_repeated_fall_hour(self):
        cases = {
            "2026-03-08T09:59:59Z": "2026-03-08 1:59:59 AM PST",
            "2026-03-08T10:00:00Z": "2026-03-08 3:00:00 AM PDT",
            "2026-11-01T08:30:00Z": "2026-11-01 1:30:00 AM PDT",
            "2026-11-01T09:30:00Z": "2026-11-01 1:30:00 AM PST",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(admin_time.pacific_time(value), expected)

    def test_calendar_dates_and_missing_values(self):
        for value in (date(2026, 1, 15), "2026-01-15"):
            self.assertEqual(admin_time.pacific_date(value), "2026-01-15")
            self.assertEqual(admin_time.pacific_time(value), "2026-01-15")
        for value in (None, "", "not a date", "2026-99-99"):
            self.assertEqual(admin_time.pacific_time(value), "—")
            self.assertEqual(admin_time.pacific_date(value), "—")

    def test_today_uses_pacific_even_when_utc_is_tomorrow(self):
        dt = datetime(2026, 1, 15, 7, 59, tzinfo=timezone.utc)
        with patch.object(admin_time, "datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda zone: dt.astimezone(zone)
            self.assertEqual(admin_time.pacific_today(), date(2026, 1, 14))
            clock.now.assert_called_once_with(admin_time.PACIFIC)


class AdminTimePagesTest(unittest.TestCase):
    def setUp(self):
        store.ensure_founding_column()
        self.pid = store.create_provider("Time sample", secrets.token_hex(6) + "@example.invalid", "password123")
        store.set_subscription(self.pid, "micro", "trialing")
        with store.db() as conn:
            conn.execute("UPDATE providers SET created_at=? WHERE id=?", (WINTER, self.pid))
            conn.execute("UPDATE subscriptions SET trial_ends_at=? WHERE provider_id=?", (WINTER, self.pid))
        self.client = TestClient(app.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set(app.SESSION_COOKIE, store.create_session(self.pid))
        admin = patch.object(app, "is_admin", return_value=True)
        admin.start()
        self.addCleanup(admin.stop)

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn(ZONE_NOTE, response.text)
        return response

    def test_customer_trial_attribution_and_abuse_dates(self):
        response = self.page("/admin")
        self.assertIn('data-sort="' + WINTER + '">2026-01-14</td>', response.text)
        self.assertIn("ends 2026-01-14", response.text)
        for path in ("/admin/attribution", "/admin/abuse"):
            with self.subTest(path=path):
                self.assertIn("2026-01-14</td>", self.page(path).text)
        with store.db() as conn:
            self.assertEqual(conn.execute("SELECT created_at FROM providers WHERE id=?", (self.pid,)).fetchone()["created_at"], WINTER)
        self.assertNotIn(ZONE_NOTE, self.client.get("/demo").text)

    def test_conversion_reset_and_suggestion_times(self):
        report = {"days": 28, "reset_at": WINTER, "stages": [], "sources": [], "pages": []}
        with patch.object(growth, "report", return_value=report):
            response = self.page("/admin/conversions")
        self.assertIn('datetime="' + WINTER + '">' + WINTER_DISPLAY + "</time>", response.text)
        suggestion = {"id": 1, "name": "Sample", "email": "sample@example.invalid", "company": "Sample daycare", "body": "Sample suggestion", "created_at": SUMMER}
        with patch.object(store, "list_suggestions", return_value=[suggestion]):
            self.assertIn(SUMMER_DISPLAY, self.page("/admin/suggestions").text)

    def test_visitor_today_changes_at_pacific_midnight(self):
        stamps = ["2026-01-15T07:59:59Z", "2026-01-15T08:00:00Z", "2026-01-15T08:01:00Z"]
        visits, rollup = [], []
        for i, stamp in enumerate(stamps):
            identity = "samehash" + str(i)
            visits.append({"ts": stamp, "ip_hash": identity, "ua": "", "referrer": "", "path": "/demo"})
            rollup.append({"first_ts": stamp, "last_ts": stamp, "ip_hash": identity, "visits": 1, "pages": 1, "last_path": "/demo"})
        with patch.object(store, "site_visit_stats", return_value=(visits, rollup)), patch.object(app, "pacific_today", return_value=date(2026, 1, 15)):
            response = self.page("/admin/visitors")
        self.assertEqual(response.context["today_visitors"], 2)
        self.assertIn("2026-01-14 11:59:59 PM PST", response.text)
        self.assertIn("2026-01-15 12:00:00 AM PST", response.text)

    def test_email_and_setup_request_times(self):
        contact = {"id": 1, "email": "sample@example.invalid", "name": "Sample", "source": "/demo", "marketing_status": "subscribed", "setup_opt_out_at": None, "suppressed_at": None, "requested_at": WINTER, "confirmed_at": SUMMER, "last_sent": SUMMER}
        report = {"settings": {"postal_address": "", "followups_enabled": False}, "contacts": [contact], "confirmed": 1, "pending": 0, "campaigns": [], "outbox": [{"email": contact["email"], "purpose": "kit", "stage": "", "status": "sent", "attempts": 1, "sent_at": WINTER, "error": ""}]}
        with patch.object(email_engagement, "report", return_value=report):
            response = self.page("/admin/email")
        self.assertEqual(response.text.count(WINTER_DISPLAY), 2)
        self.assertEqual(response.text.count(SUMMER_DISPLAY), 2)
        request = {"id": 1, "name": "Sample", "program": "Sample daycare", "email": contact["email"], "families": "1-10", "stage": "exploring", "topic": "start", "reply_url": "mailto:sample@example.invalid", "note": "", "created_at": WINTER, "notified_at": SUMMER, "notification_status": "accepted", "status": "new", "admin_note": ""}
        with patch.object(setup_help, "report", return_value={"requests": [request], "counts": {}, "offset": 0, "next": None}):
            response = self.page("/admin/setup-help")
        self.assertIn("Requested " + WINTER_DISPLAY, response.text)
        self.assertIn(" at " + SUMMER_DISPLAY, response.text)
        self.page("/admin/partners")

    def test_admin_export_preserves_instants_with_pacific_offsets(self):
        contact = {"email": "sample@example.invalid", "name": "Sample", "source": "/demo", "marketing_status": "subscribed", "suppressed_at": None, "requested_at": "2026-01-15T02:30:00.123456+00:00", "confirmed_at": SUMMER, "consent_version": "sample", "consent_text": "Sample consent"}
        with patch.object(email_engagement, "contacts", return_value=[contact]):
            response = self.client.get("/admin/email/export")
        self.assertEqual(response.status_code, 200)
        row = next(csv.DictReader(io.StringIO(response.text)))
        self.assertEqual(row["requested_at"], "2026-01-14T18:30:00.123456-08:00")
        self.assertEqual(row["confirmed_at"], "2026-07-14T19:30:00-07:00")
        for key in ("requested_at", "confirmed_at"):
            self.assertEqual(datetime.fromisoformat(row[key]), datetime.fromisoformat(contact[key]))


if __name__ == "__main__":
    unittest.main()
