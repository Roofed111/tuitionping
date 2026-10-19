"""Consent, campaign isolation, account progress, and safe retry boundaries."""
import csv
import io
import json
import re
import secrets
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import email_engagement as emails


class EmailEngagementTest(unittest.TestCase):
    def setUp(self):
        emails.ensure_tables()
        with store.db() as conn:
            for t in ['email_outbox', 'email_campaigns', 'email_contacts', 'email_settings', 'email_rate_limits', 'email_subscription_state']:
                conn.execute('DELETE FROM ' + t)
        self.dt = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.time = patch.object(emails, 'now', return_value=self.dt)
        self.time.start(); self.addCleanup(self.time.stop)
        self.active = patch.object(app, 'EMAIL_ACTIVE', True)
        self.active.start(); self.addCleanup(self.active.stop)
        self.original_sender = app.send_email
        self.sender = patch.object(app, 'send_email', return_value=True)
        self.send = self.sender.start(); self.addCleanup(self.sender.stop)
        self.client = TestClient(app.app, base_url='https://www.tuitionping.com')
        self.addCleanup(self.client.close)

    def rows(self):
        with store.db() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM email_outbox ORDER BY id').fetchall()]

    def token(self, row=None):
        return re.search(r'/email-list/confirm\?token=([A-Za-z0-9_-]+)', (row or self.rows()[-1])['html']).group(1)

    def subscribed(self, email='owner@example.invalid', name='Owner'):
        emails.request_email(email, name, '/guides', True)
        self.assertTrue(emails.confirm_contact(self.token()))
        return emails.contact(email)

    def configured(self):
        emails.save_settings('123 Example Street\nExample City, CA 90000', True)

    def provider(self, status='trialing', age=3, days_left=27):
        pid = store.create_provider('Sample owner', secrets.token_hex(8) + '@example.invalid', 'password123')
        store.set_email_verified(pid, True)
        with store.db() as conn:
            conn.execute('UPDATE providers SET created_at = ? WHERE id = ?', (emails.stamp(self.dt - timedelta(days=age)), pid))
        if status:
            store.set_subscription(pid, 'micro', status, emails.stamp(self.dt + timedelta(days=days_left)))
        return dict(store.get_provider(pid))

    def family(self, p):
        loc = store.create_location(p['id'], 'Example daycare')
        room = store.create_classroom(loc, 'Example room')
        return store.create_family(room, 'Sample child', '+12025550123', 800, 15)

    def admin(self):
        p = self.provider('active')
        token = store.create_session(p['id'])
        self.client.cookies.set(app.SESSION_COOKIE, token)
        self.admin_patch = patch.object(app, 'ADMIN_EMAILS', {p['email']})
        self.admin_patch.start(); self.addCleanup(self.admin_patch.stop)
        return {'csrf_token': app.csrf_token_for_session(token)}

    def test_form_is_public_csrf_protected_and_optin_is_optional(self):
        r = self.client.get('/guides')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Email me the free kit', r.text)
        self.assertIn('Download the complete free kit', r.text)
        self.assertNotRegex(r.text, r'name="marketing_optin"[^>]*checked')
        self.assertEqual(self.client.post('/email-list/join', data={'email': 'owner@example.invalid'}).status_code, 403)
        csrf = re.search('name="csrf_token" value="([a-f0-9]+)"', r.text).group(1)
        r = self.client.post('/email-list/join', data={'email': 'Owner@Example.invalid', 'name': '<script>oops</script>', 'csrf_token': csrf, 'source': '/guides'})
        self.assertEqual(r.status_code, 200)
        self.send.assert_called_once()
        self.assertEqual(emails.contact('owner@example.invalid')['marketing_status'], 'none')
        self.assertIn(emails.KIT, self.rows()[0]['html'])
        self.assertNotIn('/email-list/confirm?', self.rows()[0]['html'])

    def test_double_optin_get_is_read_only_and_post_confirms(self):
        emails.request_email('owner@example.invalid', 'Owner', '/guides', True)
        token = self.token()
        c = emails.contact('owner@example.invalid')
        self.assertEqual(c['marketing_status'], 'pending')
        self.assertNotIn(token, c['confirm_hash'])
        self.client.get('/email-list/confirm?token=' + token)
        self.assertEqual(emails.contact(c['email'])['marketing_status'], 'pending')
        r = self.client.post('/email-list/confirm', data={'token': token})
        self.assertIn("You're subscribed", r.text)
        self.assertEqual(emails.contact(c['email'])['marketing_status'], 'subscribed')
        self.assertFalse(emails.confirm_contact(token))
        self.assertFalse(emails.confirm_contact('forged'))

    def test_expired_confirmation_and_suppression_do_not_enroll(self):
        emails.request_email('owner@example.invalid', '', '/guides', True)
        token = self.token()
        with patch.object(emails, 'now', return_value=self.dt + timedelta(days=8)):
            self.assertFalse(emails.confirm_contact(token))
        c = emails.contact('owner@example.invalid')
        emails.suppress(c['id'])
        self.assertFalse(emails.confirm_contact(token))
        self.assertIsNone(emails.request_email(c['email'], '', '/guides', True))

    def test_request_dedupe_honeypot_rate_limit_and_retry_form(self):
        emails.request_email('owner@example.invalid', '', '/guides', True)
        emails.request_email('OWNER@example.invalid', '', '/guides', True)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(all(emails.rate_allowed('192.0.2.1') for _ in range(10)))
        self.assertFalse(emails.rate_allowed('192.0.2.1'))
        r = self.client.get('/email-kit')
        csrf = re.search('name="csrf_token" value="([a-f0-9]+)"', r.text).group(1)
        r = self.client.post('/email-list/join', data={'email': 'invalid', 'csrf_token': csrf})
        self.assertEqual(r.status_code, 400)
        self.assertIn('name="csrf_token" value="' + csrf + '"', r.text)
        r = self.client.post('/email-list/join', data={'email': 'bot@example.invalid', 'website': 'bot', 'csrf_token': csrf})
        self.assertEqual(r.status_code, 200)
        self.send.assert_not_called()

    def test_unsubscribe_one_click_needs_no_login_and_excludes_exports(self):
        c = self.subscribed(name='=HYPERLINK("bad")')
        data = list(csv.reader(io.StringIO(emails.export_csv())))
        self.assertTrue(data[1][1].startswith("'="))
        self.assertNotIn(c['manage_token'], emails.export_csv())
        self.client.get('/email-list/preferences?token=' + c['manage_token'])
        self.assertEqual(emails.contact(c['email'])['marketing_status'], 'subscribed')
        self.client.cookies.set(app.SESSION_COOKIE, 'unrelated-session')
        r = self.client.post('/email-list/preferences?token=' + c['manage_token'], data={'List-Unsubscribe': 'One-Click'})
        self.assertEqual(r.status_code, 200)
        self.assertIn("You're unsubscribed", r.text)
        self.assertNotIn(c['email'], emails.export_csv())
        self.assertTrue(emails.unsubscribe(c['manage_token']))

    def test_resubscribe_requires_new_confirmation_and_preserves_setup_optout(self):
        c = self.subscribed()
        emails.unsubscribe(c['manage_token'])
        with patch.object(emails, 'now', return_value=self.dt + timedelta(days=2)):
            emails.request_email(c['email'], '', '/guides', True)
            self.assertNotIn(c['email'], emails.export_csv())
            self.assertTrue(emails.confirm_contact(self.token()))
        c = emails.contact(c['email'])
        self.assertEqual(c['marketing_status'], 'subscribed')
        self.assertTrue(c['setup_opt_out_at'])

    def test_campaign_only_uses_confirmed_and_rechecks_unsubscribes(self):
        c = self.subscribed()
        emails.request_email('pending@example.invalid', '', '/guides', True)
        emails.contact('account-only@example.invalid')
        cid = emails.create_campaign('A useful tip', 'Check receipts before confirming PAID.', '/demo')
        with self.assertRaises(ValueError):
            emails.queue_campaign(cid)
        self.configured()
        self.assertEqual(emails.queue_campaign(cid), 1)
        self.assertEqual(emails.queue_campaign(cid), 0)
        campaigns = [r for r in self.rows() if r['purpose'] == 'campaign']
        self.assertEqual(len(campaigns), 1)
        self.assertIn('123 Example Street', campaigns[0]['html'])
        emails.unsubscribe(c['manage_token'])
        emails.deliver_due(self.send, only_id=campaigns[0]['id'])
        self.send.assert_not_called()

    def test_campaign_snapshot_cancel_escape_and_send_review_guards(self):
        self.subscribed()
        self.configured()
        csrf = self.admin()
        cid = emails.create_campaign('<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '/demo')
        r = self.client.get(f'/admin/email/campaigns/{cid}')
        self.assertEqual(r.status_code, 200)
        self.assertIn('&lt;img', r.text)
        self.assertNotIn('<img src=x', r.text)
        self.assertEqual(self.client.post(f'/admin/email/campaigns/{cid}/send', data={**csrf, 'expected_audience': 1}).status_code, 400)
        self.assertEqual(self.client.post(f'/admin/email/campaigns/{cid}/send', data={**csrf, 'expected_audience': 2, 'reviewed': 'on'}).status_code, 409)
        r = self.client.post(f'/admin/email/campaigns/{cid}/send', data={**csrf, 'expected_audience': 1, 'reviewed': 'on'}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.subscribed('late@example.invalid')
        self.assertEqual(emails.queue_campaign(cid), 0)
        self.assertEqual(len([r for r in self.rows() if r['campaign_id'] == cid]), 1)
        emails.cancel_campaign(cid)
        self.assertEqual([r for r in self.rows() if r['campaign_id'] == cid][0]['status'], 'cancelled')

    def test_retries_keep_identical_payload_and_never_duplicate_sent_mail(self):
        oid = emails.request_email('owner@example.invalid', '', '/guides', True)
        sender = MagicMock(side_effect=[False, True])
        self.assertEqual(emails.deliver_due(sender, only_id=oid)['failed'], 1)
        with patch.object(emails, 'now', return_value=self.dt + timedelta(hours=1)):
            self.assertEqual(emails.deliver_due(sender, only_id=oid)['accepted'], 1)
        self.assertEqual(sender.call_args_list[0], sender.call_args_list[1])
        emails.deliver_due(sender, only_id=oid)
        self.assertEqual(sender.call_count, 2)

    def test_old_unknown_deliveries_are_not_retried_outside_idempotency_window(self):
        oid = emails.request_email('owner@example.invalid', '', '/guides', True)
        sender = MagicMock(return_value=False)
        emails.deliver_due(sender, only_id=oid)
        with patch.object(emails, 'now', return_value=self.dt + timedelta(hours=24)):
            emails.deliver_due(sender, only_id=oid)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.rows()[0]['status'], 'uncertain')

    def test_onboarding_stages_exclude_active_expired_unverified_and_suspended(self):
        self.assertEqual(emails.onboarding_stage(self.provider(None)), 'checkout')
        p = self.provider()
        self.assertEqual(emails.onboarding_stage(p), 'families')
        fid = self.family(p)
        self.assertEqual(emails.onboarding_stage(p), 'first_reminder')
        store.mark_reminder_sent(fid, '2026-10', 'before')
        self.assertIsNone(emails.onboarding_stage(p))
        ending = self.provider(days_left=2)
        self.assertEqual(emails.onboarding_stage(ending), 'trial_ending')
        self.assertIn('cancel', emails.onboarding_copy('trial_ending', ending, dict(store.get_subscription(ending['id'])))[1])
        for status in ['active', 'past_due', 'canceled', 'expired', 'unpaid', 'paused']:
            self.assertIsNone(emails.onboarding_stage(self.provider(status)))
        self.assertIsNone(emails.onboarding_stage({**ending, 'email_verified': 0}))
        self.assertIsNone(emails.onboarding_stage({**ending, 'suspended': 1}))
        self.assertIsNone(emails.onboarding_stage(self.provider(days_left=-1)))
        self.assertIsNone(emails.onboarding_stage(self.provider(None, age=8)))

    def test_queued_followup_is_cancelled_after_account_progress(self):
        p = self.provider()
        self.configured()
        with patch.object(store, 'all_providers', return_value=[p]):
            self.assertEqual(emails.queue_onboarding(), 1)
            self.assertEqual(emails.queue_onboarding(), 0)
            row = self.rows()[0]
            self.family(p)
            self.assertEqual(emails.deliver_due(self.send, only_id=row['id'])['cancelled'], 1)
        self.send.assert_not_called()

    def test_scheduled_cancellation_stops_trial_guidance_before_status_changes(self):
        import billing
        p = self.provider()
        emails.note_subscription(p['id'], {'status': 'trialing', 'cancel_at_period_end': True})
        self.assertIsNone(emails.onboarding_stage(p))
        emails.note_subscription(p['id'], {'status': 'trialing', 'cancel_at_period_end': False})
        self.assertEqual(emails.onboarding_stage(p), 'families')
        emails.note_subscription(p['id'], {'status': 'trialing', 'cancel_at': 1792000000})
        self.assertIsNone(emails.onboarding_stage(p))
        sub = {'id': 'sub_test', 'status': 'trialing', 'cancel_at_period_end': True, 'trial_end': int((self.dt + timedelta(days=27)).timestamp()), 'metadata': {'plan': 'micro'}}
        billing._sync_from_subscription(p['id'], sub)
        self.assertIsNone(emails.onboarding_stage(p))

    def test_live_trial_refresh_failure_does_not_send_stale_guidance(self):
        import billing
        p = self.provider()
        store.set_stripe_ids(p['id'], subscription_id='sub_test')
        stripe = MagicMock()
        stripe.Subscription.retrieve.side_effect = RuntimeError('temporarily unavailable')
        with patch.object(billing, 'stripe_configured', return_value=True), patch.object(billing, '_stripe_lib', return_value=stripe):
            self.assertIsNone(emails.onboarding_stage(p, verify_billing=True))

    def test_followup_spacing_stage_dedup_and_optout(self):
        p = self.provider()
        with patch.object(store, 'all_providers', return_value=[p]):
            self.assertEqual(emails.queue_onboarding(), 0)
            self.configured()
            self.assertEqual(emails.queue_onboarding(), 1)
            self.assertEqual(emails.deliver_due(self.send)['accepted'], 1)
            self.assertEqual(emails.queue_onboarding(), 0)
            self.family(p)
            with patch.object(emails, 'now', return_value=self.dt + timedelta(hours=24)):
                self.assertEqual(emails.queue_onboarding(), 0)
            with patch.object(emails, 'now', return_value=self.dt + timedelta(hours=49)):
                self.assertEqual(emails.queue_onboarding(), 1)
                emails.unsubscribe(emails.contact(p['email'])['manage_token'])
                emails.deliver_due(self.send)
                self.assertEqual(emails.queue_onboarding(), 0)
        self.assertEqual(self.send.call_count, 1)

    def test_admin_routes_require_admin_and_exports_are_private(self):
        self.assertEqual(self.client.get('/admin/email', follow_redirects=False).headers.get('location'), '/login')
        p = self.provider('active')
        self.client.cookies.set(app.SESSION_COOKIE, store.create_session(p['id']))
        self.assertEqual(self.client.get('/admin/email').status_code, 404)
        csrf = self.admin()
        self.assertEqual(self.client.post('/admin/email/settings', data={'postal_address': '123 Example Street, CA 90000'}).status_code, 403)
        self.assertEqual(self.client.post('/admin/email/settings', data={**csrf, 'postal_address': '123 Example Street, CA 90000'}, follow_redirects=False).status_code, 303)
        r = self.client.get('/admin/email')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['cache-control'], 'private, no-store')
        r = self.client.get('/admin/email/export')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['x-robots-tag'], 'noindex')

    def test_sender_transports_idempotency_and_list_unsubscribe_headers(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{"id":"sample"}'
        original = self.original_sender
        with patch('urllib.request.urlopen', return_value=response) as open_, patch.object(app, 'RESEND_API_KEY', 'fake-test-key'):
            self.assertTrue(original('owner@example.invalid', 'Test', '<p>Test</p>', idempotency_key='test-unique', headers={'List-Unsubscribe': '<https://example.invalid/unsub>'}))
        req = open_.call_args.args[0]
        self.assertEqual(req.get_header('Idempotency-key'), 'test-unique')
        self.assertIn('List-Unsubscribe', json.loads(req.data)['headers'])

    def test_cron_requires_token_and_email_failure_does_not_block_sms_result(self):
        with patch.object(app, 'INTERNAL_CRON_TOKEN', ''):
            self.assertEqual(self.client.get('/internal/run-reminders').status_code, 401)
        with patch.object(app, 'INTERNAL_CRON_TOKEN', 'test-cron'), patch.object(app, 'run_reminders', return_value=[]), patch.object(emails, 'run', side_effect=RuntimeError('test')):
            r = self.client.get('/internal/run-reminders?token=test-cron')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()['sent'], 0)
            self.assertIn('error', r.json()['email'])


if __name__ == '__main__':
    unittest.main()
