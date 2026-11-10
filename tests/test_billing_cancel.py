"""In-app trial/subscription cancellation: local trials cancel immediately,
Stripe trials cancel at period end, and the billing page always shows the
cancel path (previously cancellation existed only inside the Stripe portal,
and not at all for subscriptions with no Stripe record behind them)."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.pop('DATABASE_URL', None)
os.environ['DEMO_MODE'] = '1'
import store
_test_dir = tempfile.TemporaryDirectory()
store.DB_PATH = os.path.join(_test_dir.name, 'test.db')
store.init_db()
import billing
import app
from fastapi.testclient import TestClient


def _trial_end_ts():
    return int(datetime(2026, 12, 9, tzinfo=timezone.utc).timestamp())


class LocalCancelTest(unittest.TestCase):
    def test_trial_without_stripe_cancels_immediately(self):
        store.set_subscription(101, 'micro', 'trialing',
                               trial_ends_at='2026-12-09T00:00:00+00:00')
        result = billing.cancel_subscription(101)
        self.assertTrue(result['ok'])
        self.assertEqual(result['mode'], 'immediate')
        self.assertEqual(result['status'], 'canceled')
        self.assertEqual(billing.subscription_summary(101)['status'], 'canceled')

    def test_no_subscription_cannot_cancel(self):
        result = billing.cancel_subscription(999)
        self.assertFalse(result['ok'])


class StripeCancelTest(unittest.TestCase):
    def _stripe_sub(self, cancel_flag):
        return {'id': 'sub_x', 'customer': 'cus_x', 'status': 'trialing',
                'trial_end': _trial_end_ts(), 'cancel_at_period_end': cancel_flag,
                'metadata': {'plan': 'micro', 'provider_id': '102'},
                'items': {'data': []}}

    def setUp(self):
        store.set_subscription(102, 'micro', 'trialing',
                               trial_ends_at='2026-12-09T00:00:00+00:00')
        store.set_stripe_ids(102, customer_id='cus_x', subscription_id='sub_x')
        self.stripe = MagicMock()
        self.stripe.Subscription.retrieve.return_value = self._stripe_sub(False)
        self.stripe.Subscription.modify.return_value = self._stripe_sub(True)
        for p in (patch.object(billing, '_stripe_lib', return_value=self.stripe),
                  patch.object(billing, 'stripe_configured', return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_stripe_trial_cancels_at_period_end(self):
        result = billing.cancel_subscription(102)
        self.assertTrue(result['ok'])
        self.assertEqual(result['mode'], 'period_end')
        self.stripe.Subscription.modify.assert_called_once_with(
            'sub_x', cancel_at_period_end=True)
        summary = billing.subscription_summary(102)
        self.assertEqual(summary['status'], 'trialing')
        self.assertTrue(summary['cancel_at_period_end'])
        self.assertTrue(summary['cancel_at'].startswith('2026-12-09'))
        self.assertTrue(summary['has_stripe_customer'])

    def test_already_canceled_in_stripe_syncs_instead_of_erroring(self):
        # Rob's walkthrough account, Nov 9 2026: the local row said trialing
        # but the Stripe subscription on file (an old test checkout) was
        # already canceled — modify() refused with "A canceled subscription
        # can only update its cancellation_details and metadata."
        store.set_subscription(103, 'micro', 'trialing',
                               trial_ends_at='2026-12-09T00:00:00+00:00')
        store.set_stripe_ids(103, customer_id='cus_x', subscription_id='sub_dead')
        self.stripe.Subscription.retrieve.return_value = {
            'id': 'sub_dead', 'customer': 'cus_x', 'status': 'canceled',
            'trial_end': None, 'cancel_at_period_end': False,
            'metadata': {'plan': 'micro', 'provider_id': '103'},
            'items': {'data': []}}
        with patch.object(billing, '_stripe_lib', return_value=self.stripe), \
                patch.object(billing, 'stripe_configured', return_value=True):
            result = billing.cancel_subscription(103)
        self.assertTrue(result['ok'])
        self.assertEqual(result['mode'], 'immediate')
        self.assertEqual(billing.subscription_summary(103)['status'], 'canceled')

    def test_modify_race_with_portal_cancellation_still_succeeds(self):
        store.set_subscription(104, 'micro', 'trialing',
                               trial_ends_at='2026-12-09T00:00:00+00:00')
        store.set_stripe_ids(104, customer_id='cus_x', subscription_id='sub_race')
        live = {'id': 'sub_race', 'customer': 'cus_x', 'status': 'trialing',
                'trial_end': _trial_end_ts(), 'cancel_at_period_end': False,
                'metadata': {'plan': 'micro', 'provider_id': '104'},
                'items': {'data': []}}
        dead = dict(live, status='canceled', trial_end=None)
        self.stripe.Subscription.retrieve.side_effect = [live, dead]
        self.stripe.Subscription.modify.side_effect = Exception(
            'A canceled subscription can only update its cancellation_details and metadata.')
        with patch.object(billing, '_stripe_lib', return_value=self.stripe), \
                patch.object(billing, 'stripe_configured', return_value=True):
            result = billing.cancel_subscription(104)
        self.assertTrue(result['ok'])
        self.assertEqual(billing.subscription_summary(104)['status'], 'canceled')
        self.stripe.Subscription.retrieve.side_effect = None
        self.stripe.Subscription.modify.side_effect = None

    def test_resume_clears_scheduled_cancellation(self):
        billing.cancel_subscription(102)
        self.stripe.Subscription.modify.return_value = self._stripe_sub(False)
        result = billing.resume_subscription(102)
        self.assertTrue(result['ok'])
        self.assertFalse(billing.subscription_summary(102)['cancel_at_period_end'])


class BillingPageCancelUiTest(unittest.TestCase):
    PROVIDER = {'id': 7, 'email': 'owner@example.invalid', 'suspended': False,
                'name': 'Owner'}

    def _get_billing(self, summary, live=False):
        patches = [patch.object(store, 'get_provider_by_session', return_value=self.PROVIDER),
                   patch.object(billing, 'subscription_summary', return_value=summary)]
        if live:
            patches += [patch.object(billing, 'DEMO_MODE', False),
                        patch.object(billing, 'stripe_configured', return_value=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with TestClient(app.app) as client:
            client.cookies.set(app.SESSION_COOKIE, 'test-session')
            return client.get('/billing')

    def test_trial_shows_cancel_trial_not_empty_portal(self):
        summary = {'plan': 'micro', 'status': 'trialing', 'founding': False,
                   'trial_ends_at': '2026-12-09T00:00:00+00:00',
                   'cancel_at_period_end': False, 'cancel_at': None,
                   'has_stripe_customer': False}
        response = self._get_billing(summary)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Cancel your free trial', response.text)
        self.assertIn('action="/billing/cancel"', response.text)
        self.assertNotIn('action="/billing/portal"', response.text)

    def test_scheduled_cancellation_shows_resume(self):
        summary = {'plan': 'micro', 'status': 'trialing', 'founding': False,
                   'trial_ends_at': '2026-12-09T00:00:00+00:00',
                   'cancel_at_period_end': True, 'cancel_at': '2026-12-09T00:00:00+00:00',
                   'has_stripe_customer': True}
        response = self._get_billing(summary)
        self.assertEqual(response.status_code, 200)
        self.assertIn('set to cancel on 2026-12-09', response.text)
        self.assertIn('action="/billing/resume"', response.text)

    def test_cancel_route_redirects_with_notice(self):
        with patch.object(store, 'get_provider_by_session', return_value=self.PROVIDER), \
                patch.object(billing, 'cancel_subscription',
                             return_value={'ok': True, 'mode': 'immediate'}):
            with TestClient(app.app) as client:
                client.cookies.set(app.SESSION_COOKIE, 'test-session')
                token = app.csrf_token_for_session('test-session')
                response = client.post('/billing/cancel', data={'csrf_token': token},
                                       follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/billing?cancel=done')


if __name__ == '__main__':
    unittest.main()
