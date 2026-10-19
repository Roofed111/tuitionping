"""Verify parent reports without duplicating ledger entries or settling new fees."""
import secrets
import os
import tempfile
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
        self.time_patch = patch.object(store, 'now_iso', return_value='2026-10-06T12:00:00+00:00')
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

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
        after = self.ledger()
        self.assertEqual(after[0]['paid_source'], 'manual')
        self.assertEqual([{k: v for k, v in row.items() if k != 'paid_source'} for row in after],
                         [{k: v for k, v in row.items() if k != 'paid_source'} for row in before])
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

    def test_private_public_and_annual_statements_exclude_reports_until_verified(self):
        store.mark_family_paid(self.fid, '2026-09', source='manual')
        store.add_extra_charge(self.fid, 'Reported fee', 25)
        self.report()
        token = store.get_or_create_statement_token(self.fid)
        urls = [f'/families/{self.fid}/statement/2026', f'/s/{token}/2026', '/reports/annual/2026']
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            label = 'Collected' if '/reports/' in url else 'Total paid in 2026'
            self.assertIn(f'{label}: $800.00', response.text)
        self.assertEqual(len(store.get_family_payments_for_year(self.fid, 2026)), 1)
        self.assertEqual(store.year_nudged_stats(self.pid, 2026)['total_amount'], 800)
        self.assertEqual(self.post().status_code, 303)
        self.assertEqual(self.post().status_code, 303)
        for url in urls:
            label = 'Collected' if '/reports/' in url else 'Total paid in 2026'
            self.assertIn(f'{label}: $1,625.00', self.client.get(url).text)
        self.assertEqual(len(self.ledger()), 2)
        self.assertEqual(store.year_nudged_stats(self.pid, 2026)['total_amount'], 1625)

    def test_statement_year_uses_original_record_date_after_confirmation(self):
        self.report()
        with store.db() as conn:
            conn.execute('UPDATE paid_log SET paid_at = ? WHERE family_id = ?',
                         ('2025-12-31T23:59:59+00:00', self.fid))
        self.assertEqual(store.get_family_payments_for_year(self.fid, 2025), [])
        self.assertEqual(self.post().status_code, 303)
        self.assertEqual(store.get_family_payments_for_year(self.fid, 2025)[0]['amount'], 800)
        self.assertEqual(store.get_provider_payments_for_year(self.pid, 2026), [])

    def test_unconfirmed_reports_do_not_receive_statement_blast(self):
        self.report()
        with patch.object(app, 'plan_at_least_growth', return_value=True), \
             patch.object(app, 'SMS_DEMO_MODE', False), patch('sms.send_sms') as send:
            response = self.client.post('/statements/blast', data={
                'year': 2026, 'csrf_token': app.csrf_token_for_session(self.session)}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        send.assert_not_called()
        self.assertIn('&n=0&', response.headers['location'])

    def test_verified_family_receives_statement_blast(self):
        self.report()
        self.assertEqual(self.post().status_code, 303)
        with patch.object(app, 'plan_at_least_growth', return_value=True), \
             patch.object(app, 'SMS_DEMO_MODE', False), patch('sms.send_sms') as send:
            response = self.client.post('/statements/blast', data={
                'year': 2026, 'csrf_token': app.csrf_token_for_session(self.session)}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[3], self.fid)
        self.assertIn('&n=1&', response.headers['location'])

    def test_current_record_review_updates_dashboard_verification(self):
        self.report()
        self.assertTrue(store.confirm_logged_family_payment(self.fid, self.ledger()[0]['id']))
        self.assertEqual(store.get_family(self.fid)['paid_source'], 'manual')
        self.assertEqual(self.post().status_code, 303)
        self.assertEqual(len(store.get_family_payments_for_year(self.fid, 2026)), 1)
        self.assertEqual(store.get_unverified_family_payments(self.fid), [])

    def test_historical_review_does_not_change_current_period_or_later_charges(self):
        store.mark_family_paid(self.fid, '2026-09', source='reply')
        self.report()
        store.add_extra_charge(self.fid, 'Later fee', 10)
        original = self.ledger()[0]
        self.assertIn('Payments needing verification', self.client.get(f'/families/edit/{self.fid}').text)
        data = {'family_id': self.fid, 'payment_id': original['id'],
                'csrf_token': app.csrf_token_for_session(self.session)}
        for _ in range(2):
            self.assertEqual(self.client.post('/families/confirm-logged-payment', data=data,
                                             follow_redirects=False).status_code, 303)
        self.assertEqual(store.get_family(self.fid)['paid_period'], '2026-10')
        self.assertEqual(store.get_family(self.fid)['paid_source'], 'reply')
        self.assertEqual(store.outstanding_charges_total(self.fid), 10)
        self.assertEqual(store.get_family_payments_for_year(self.fid, 2026),
                         [{k: original[k] for k in ('period', 'amount', 'paid_at')}])

    def test_historical_review_requires_ownership_and_csrf(self):
        self.report()
        payment_id = self.ledger()[0]['id']
        data = {'family_id': self.fid, 'payment_id': payment_id}
        self.assertEqual(self.client.post('/families/confirm-logged-payment', data=data).status_code, 403)
        other = store.create_provider('Other', secrets.token_hex(8) + '@example.invalid', 'password123')
        other_loc = store.create_location(other, 'Other daycare')
        other_room = store.create_classroom(other_loc, 'Room')
        other_fid = store.create_family(other_room, 'Other child', '+12025550124', 900, 5)
        store.mark_family_paid(other_fid, '2026-10', source='reply')
        with store.db() as conn:
            other_payment = conn.execute('SELECT id FROM paid_log WHERE family_id = ?', (other_fid,)).fetchone()['id']
        data['csrf_token'] = app.csrf_token_for_session(self.session)
        data['payment_id'] = other_payment
        self.assertEqual(self.client.post('/families/confirm-logged-payment', data=data).status_code, 404)
        data['family_id'] = other_fid
        self.assertEqual(self.client.post('/families/confirm-logged-payment', data=data).status_code, 404)
        self.assertEqual(store.get_provider_payments_for_year(other, 2026), [])
        self.assertEqual(store.get_provider_unverified_payments_for_year(self.pid, 2026)[0]['family_id'], self.fid)


class LegacyLedgerMigrationTest(unittest.TestCase):
    def test_legacy_migration_preserves_records_and_requires_evidence(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(store, 'DB_PATH', os.path.join(directory, 'legacy.db')), \
             patch.object(store, '_schema_ensured', set()):
            store.init_db()
            store.ensure_paid_source_column()
            with store.db() as conn:
                conn.execute('INSERT INTO families (id, classroom_id, name, phone, tuition_amount, due_day, created_at, paid_period, paid_source) VALUES (1, 1, ?, ?, 800, 5, ?, ?, ?)',
                             ('Legacy family', '+12025550123', '2025-01-01', '2026-10', 'manual'))
                conn.execute('CREATE TABLE paid_log (id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER NOT NULL, period TEXT NOT NULL, amount REAL NOT NULL, paid_at TEXT NOT NULL, UNIQUE(family_id, period))')
                for period, amount in [('2026-08', 700), ('2026-09', 750), ('2026-10', 800)]:
                    conn.execute('INSERT INTO paid_log (family_id, period, amount, paid_at) VALUES (1, ?, ?, ?)',
                                 (period, amount, period + '-05T00:00:00+00:00'))
            store.ensure_paid_log()
            payments = store.get_family_payments_for_year(1, 2026)
            self.assertEqual([p['amount'] for p in payments], [800])
            pending = store.get_unverified_family_payments(1)
            self.assertEqual([p['amount'] for p in pending], [700, 750])
            self.assertEqual([p['paid_source'] for p in pending], ['unknown', 'unknown'])
            self.assertTrue(store.confirm_logged_family_payment(1, pending[0]['id']))
            store._schema_ensured.clear()  # simulate a restart: migration must remain idempotent
            store.ensure_paid_log()
            self.assertEqual([p['amount'] for p in store.get_family_payments_for_year(1, 2026)], [700, 800])
            self.assertEqual(store.get_unverified_family_payments(1)[0]['amount'], 750)
            with store.db() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) n FROM paid_log').fetchone()['n'], 3)

    def test_current_legacy_parent_report_is_excluded_until_confirmed(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(store, 'DB_PATH', os.path.join(directory, 'legacy.db')), \
             patch.object(store, '_schema_ensured', set()):
            store.init_db()
            store.ensure_paid_source_column()
            with store.db() as conn:
                conn.execute('INSERT INTO families (id, classroom_id, name, phone, tuition_amount, due_day, created_at, paid_period, paid_source) VALUES (1, 1, ?, ?, 800, 5, ?, ?, ?)',
                             ('Legacy family', '+12025550123', '2025-01-01', '2026-10', 'reply'))
                conn.execute('CREATE TABLE paid_log (id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER NOT NULL, period TEXT NOT NULL, amount REAL NOT NULL, paid_at TEXT NOT NULL, UNIQUE(family_id, period))')
                conn.execute("INSERT INTO paid_log (family_id, period, amount, paid_at) VALUES (1, '2026-10', 800, '2026-10-05')")
            self.assertEqual(store.get_family_payments_for_year(1, 2026), [])
            self.assertTrue(store.confirm_family_payment(1, '2026-10'))
            self.assertEqual(store.get_family_payments_for_year(1, 2026)[0]['amount'], 800)


if __name__ == '__main__':
    unittest.main()
