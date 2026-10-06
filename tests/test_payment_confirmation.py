"""Verify parent reports without duplicating ledger entries or settling new fees."""
import secrets
import unittest
from datetime import date
from unittest.mock import patch
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
from reminders import family_status


class PaymentConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.pid = store.create_provider('Test daycare', secrets.token_hex(8) + '@example.invalid', 'password123')
        loc = store.create_location(self.pid, 'Sample daycare')
        room = store.create_classroom(loc, 'Sample classroom')
        self.fid = store.create_family(room, 'Sample child', '+12025550123', 800, 5)
        with store.db() as conn:
            conn.execute("UPDATE families SET created_at = ? WHERE id = ?",
                         ('2026-09-01T00:00:00+00:00', self.fid))
        self.session = store.create_session(self.pid)
        self.client = TestClient(app.app)
        self.client.cookies.set(app.SESSION_COOKIE, self.session)
        self.sub_patch = patch.object(store, 'get_subscription', return_value={'status': 'active', 'plan': 'micro'})
        self.sub_patch.start()
        self.addCleanup(self.sub_patch.stop)
        self.addCleanup(self.client.close)
        self.day_patch = patch.object(app, 'today', return_value=date(2026, 10, 6))
        self.day_patch.start()
        self.addCleanup(self.day_patch.stop)

    def report(self):
        store.mark_family_paid(self.fid, '2026-10', source='reply')

    def post(self, period='2026-10', fid=None, csrf=True):
        data = {'family_id': self.fid if fid is None else fid, 'period': period}
        if csrf:
            data['csrf_token'] = app.csrf_token_for_session(self.session)
        return self.client.post('/families/confirm-paid', data=data, follow_redirects=False)

    def ledger(self):
        with store.db() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM paid_log WHERE family_id = ?', (self.fid,)).fetchall()]

    def test_dashboard_report_becomes_verified_and_button_disappears(self):
        self.report()
        html = self.client.get('/dashboard').text
        self.assertIn('Reported paid', html)
        self.assertIn('Confirm payment received', html)
        self.assertIn('name="period" value="2026-10"', html)
        self.assertEqual(self.post().status_code, 303)
        family = store.get_family(self.fid)
        self.assertEqual(family['paid_source'], 'manual')
        self.assertEqual(family_status(family, date(2026, 10, 6))['label'], 'Paid')
        html = self.client.get('/dashboard?payment_confirmed=1').text
        self.assertIn('Payment confirmed.', html)
        self.assertNotIn('Confirm payment received', html)
        self.assertNotIn('Reported paid', html)

    def test_repeat_confirmation_preserves_ledger_new_charges_and_period(self):
        store.add_extra_charge(self.fid, 'Existing fee', 25)
        self.report()
        before = self.ledger()
        self.assertEqual(before[0]['amount'], 825)
        store.add_extra_charge(self.fid, 'Later fee', 10)
        for _ in range(2):
            self.assertEqual(self.post().status_code, 303)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(store.outstanding_charges_total(self.fid), 10)
        self.assertEqual(store.get_family(self.fid)['paid_period'], '2026-10')
        store.mark_family_paid(self.fid, '2026-10', source='reply')
        self.assertEqual(store.get_family(self.fid)['paid_source'], 'manual')

    def test_confirmation_does_not_roll_forward_after_month_changes(self):
        self.report()
        with patch.object(app, 'today', return_value=date(2026, 11, 6)):
            self.assertIn('Confirm payment received', self.client.get('/dashboard').text)
            self.assertEqual(self.post().status_code, 303)
        self.assertEqual(store.get_family(self.fid)['paid_period'], '2026-10')
        self.assertEqual(len(self.ledger()), 1)

    def test_stale_or_unreported_period_is_rejected(self):
        self.assertEqual(self.post().status_code, 409)
        self.report()
        self.assertEqual(self.post('2026-09').status_code, 409)
        self.assertEqual(self.post('2026-11').status_code, 409)
        self.assertEqual(store.get_family(self.fid)['paid_source'], 'reply')

    def test_cross_account_and_csrf_are_blocked(self):
        self.report()
        self.assertEqual(self.post(csrf=False).status_code, 403)
        other = store.create_provider('Other', secrets.token_hex(8) + '@example.invalid', 'password123')
        other_session = store.create_session(other)
        self.client.cookies.set(app.SESSION_COOKIE, other_session)
        self.session = other_session
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(store.get_family(self.fid)['paid_source'], 'reply')
        self.client.cookies.clear()
        self.assertEqual(self.post().headers.get('location'), '/login')

    def test_opted_out_family_can_still_be_confirmed(self):
        self.report()
        store.set_family_opt_out(self.fid, True)
        self.assertIn('Confirm payment received', self.client.get('/dashboard').text)
        self.assertEqual(self.post().status_code, 303)
        self.assertTrue(store.get_family(self.fid)['opted_out'])


if __name__ == '__main__':
    unittest.main()
