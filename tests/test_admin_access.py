"""Admin access and recovered release checks, using a disposable local database."""
import re
import secrets
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import billing


ADMIN_PAGES = (
    '/admin', '/admin/conversions', '/admin/visitors', '/admin/attribution',
    '/admin/abuse', '/admin/suggestions', '/admin/email', '/admin/partners',
    '/admin/setup-help',
)


class AdminAccessTest(unittest.TestCase):
    def setUp(self):
        self.email = secrets.token_hex(8) + '@example.invalid'
        self.pid = store.create_provider('Admin fixture', self.email, 'password123')
        self.allowlist = patch.object(app, 'ADMIN_EMAILS', {self.email})
        self.allowlist.start()
        self.addCleanup(self.allowlist.stop)
        self.client = TestClient(app.app, base_url='https://www.tuitionping.com')
        self.addCleanup(self.client.close)
        app._LOGIN_ATTEMPTS.clear()

    def sign_in(self, email=None, password='password123'):
        return self.client.post('/login', data={
            'email': email or self.email, 'password': password,
        }, follow_redirects=False)

    def test_admin_login_opens_admin_with_a_secure_session(self):
        r = self.sign_in(email='  ' + self.email.upper() + '  ')
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers['location'], '/admin')
        cookie = r.headers['set-cookie'].lower()
        for attribute in ('httponly', 'secure', 'samesite=lax', 'path=/'):
            self.assertIn(attribute, cookie)
        self.assertEqual(self.client.get('/admin').status_code, 200)
        self.assertEqual(self.client.get('/login', follow_redirects=False).headers['location'], '/admin')

    def test_every_admin_page_is_present_and_has_signed_in_navigation(self):
        self.sign_in()
        for path in ADMIN_PAGES:
            with self.subTest(path=path):
                r = self.client.get(path, follow_redirects=False)
                self.assertEqual(r.status_code, 200)
                self.assertIn('action="/logout"', r.text)
                self.assertIn('aria-current="page">Admin</a>', r.text)
                self.assertNotIn('href="/login">Log in</a>', r.text)
                self.assertIn('name="csrf_token"', r.text)

    def test_anonymous_visitors_cannot_access_any_admin_page(self):
        for path in ADMIN_PAGES:
            with self.subTest(path=path):
                r = self.client.get(path, follow_redirects=False)
                self.assertEqual(r.status_code, 303)
                self.assertEqual(r.headers['location'], '/login')

    def test_regular_login_still_opens_dashboard_and_cannot_access_admin(self):
        email = secrets.token_hex(8) + '@example.invalid'
        pid = store.create_provider('Provider fixture', email, 'password123')
        billing.activate_demo_subscription(pid, 'micro')
        r = self.sign_in(email=email)
        self.assertEqual(r.headers['location'], '/dashboard')
        self.assertEqual(self.client.get('/login', follow_redirects=False).headers['location'], '/dashboard')
        for path in ADMIN_PAGES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        dashboard = self.client.get('/dashboard')
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn('href="/admin"', dashboard.text)

    def test_wrong_password_and_suspension_do_not_create_sessions(self):
        self.assertEqual(self.sign_in(password='incorrect').status_code, 401)
        self.assertNotIn(app.SESSION_COOKIE, self.client.cookies)
        with store.db() as conn:
            conn.execute('UPDATE providers SET suspended = 1 WHERE id = ?', (self.pid,))
        self.assertEqual(self.sign_in().status_code, 403)
        self.assertNotIn(app.SESSION_COOKIE, self.client.cookies)

    def test_conversion_reset_still_requires_csrf_and_logout_revokes_access(self):
        self.sign_in()
        self.assertEqual(self.client.post('/admin/conversions/reset').status_code, 403)
        self.client.post('/logout', follow_redirects=False)
        self.assertEqual(self.client.get('/admin', follow_redirects=False).headers['location'], '/login')


class OwnerNotificationRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app, base_url='https://www.tuitionping.com')
        self.addCleanup(self.client.close)

    def signup(self):
        page = self.client.get('/signup?plan=micro')
        csrf = re.search('name="csrf_token" value="([a-f0-9]+)"', page.text).group(1)
        return self.client.post('/signup', data={
            'name': 'Provider fixture', 'email': secrets.token_hex(8) + '@example.invalid',
            'password': 'password123', 'agree_terms': 'on', 'attest_consent': 'on',
            'csrf_token': csrf, 'plan': 'micro', 'heard_about': 'search',
        }, follow_redirects=False)

    def test_signup_retains_the_owner_notification(self):
        with patch.object(app, 'EMAIL_ACTIVE', False), \
                patch.object(app, 'send_welcome_email'), \
                patch.object(billing, 'DEMO_MODE', True), \
                patch.object(app, 'notify_owner_new_signup') as notify:
            response = self.signup()
        self.assertEqual(response.status_code, 303)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[1:3], ('Micro', 'monthly'))
        self.assertEqual(notify.call_args.kwargs['heard_about'], 'search')

    def test_owner_notification_failure_does_not_block_signup(self):
        with patch.object(app, 'EMAIL_ACTIVE', False), \
                patch.object(app, 'send_welcome_email'), \
                patch.object(billing, 'DEMO_MODE', True), \
                patch.object(app, 'notify_owner_new_signup', side_effect=RuntimeError('fixture')):
            response = self.signup()
        self.assertEqual(response.status_code, 303)
        self.assertIn(app.SESSION_COOKIE, self.client.cookies)

    def test_checkout_notifies_owner_after_billing_and_email_failure_does_not_retry_billing(self):
        provider = {'id': 123, 'email': 'fixture@example.invalid'}
        event = {'type': 'checkout.session.completed', 'data': {'object': {'customer': 'cus_fixture'}}}
        with patch.object(billing, 'DEMO_MODE', False), \
                patch.object(billing, 'STRIPE_WEBHOOK_SECRET', 'fixture'), \
                patch.object(billing, 'verify_webhook', return_value=event), \
                patch.object(billing, 'handle_stripe_event', return_value=JSONResponse({'ok': True})) as handle, \
                patch.object(store, 'get_provider_by_stripe_customer', return_value=provider), \
                patch.object(app, 'notify_owner_new_customer', side_effect=RuntimeError('fixture')) as notify:
            response = self.client.post('/webhooks/stripe', content=b'fixture')
        self.assertEqual(response.status_code, 200)
        handle.assert_called_once_with(event)
        notify.assert_called_once_with(provider)

    def test_invalid_webhook_never_notifies_owner(self):
        with patch.object(billing, 'DEMO_MODE', False), \
                patch.object(billing, 'STRIPE_WEBHOOK_SECRET', 'fixture'), \
                patch.object(billing, 'verify_webhook', side_effect=ValueError('fixture')), \
                patch.object(app, 'notify_owner_new_customer') as notify:
            response = self.client.post('/webhooks/stripe', content=b'fixture')
        self.assertEqual(response.status_code, 400)
        notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
