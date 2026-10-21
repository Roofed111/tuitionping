"""Published prices, isolated Stripe catalog rollout and legacy billing safety."""
import copy
import json
import re
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from test_late_fee_page import app
import billing


MONTHLY = {'micro': 9, 'starter': 19, 'growth': 39, 'multisite': 59}
LEGACY_MONTHLY = {'micro': 19, 'starter': 29, 'growth': 59, 'multisite': 99}


class Page:
    def __init__(self, data):
        self.data = data

    def to_dict(self):
        return {'data': self.data}


def catalog_price(plan, cycle, legacy=False):
    amount = (LEGACY_MONTHLY if legacy else MONTHLY)[plan]
    return {'id': 'price_' + ('old_' if legacy else 'new_') + plan + '_' + cycle,
            'product': 'prod_' + plan, 'lookup_key': billing.price_lookup(plan, cycle, legacy=legacy),
            'unit_amount': amount * (1000 if cycle == 'annual' else 100), 'currency': 'usd',
            'active': True, 'type': 'recurring',
            'recurring': {'interval': 'year' if cycle == 'annual' else 'month',
                          'interval_count': 1, 'usage_type': 'licensed'}}


class StripeCatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = {billing.price_lookup(plan, cycle, legacy=True): catalog_price(plan, cycle, True)
                        for plan in MONTHLY for cycle in billing.CYCLES}
        self.original = copy.deepcopy(self.catalog)
        self.stripe = MagicMock()
        self.stripe.Price.list.side_effect = lambda lookup_keys, limit: Page([
            copy.deepcopy(self.catalog[key]) for key in lookup_keys if key in self.catalog][:limit])
        self.stripe.Price.create.side_effect = self.create_price
        self.stripe.checkout.Session.create.return_value.url = 'https://checkout.stripe.com/test'
        for p in (patch.object(billing, '_stripe_lib', return_value=self.stripe),
                  patch.object(billing, 'stripe_configured', return_value=True),
                  patch.object(billing, '_price_cache', {})):
            p.start()
            self.addCleanup(p.stop)

    def create_price(self, **kw):
        plan = kw['metadata']['tuitionping_plan']
        cycle = 'annual' if kw['recurring']['interval'] == 'year' else 'monthly'
        result = catalog_price(plan, cycle)
        for field in ('lookup_key', 'unit_amount', 'currency', 'product', 'metadata'):
            result[field] = kw[field]
        self.catalog[result['lookup_key']] = result
        return copy.deepcopy(result)

    def test_rollout_creates_eight_prices_once_and_leaves_legacy_objects_unchanged(self):
        first = billing.sync_price_catalog()
        self.assertEqual(len(first), 8)
        self.assertEqual(self.stripe.Price.create.call_count, 8)
        self.assertEqual(billing.sync_price_catalog(), first)
        self.assertEqual(self.stripe.Price.create.call_count, 8)
        self.assertEqual({key: self.catalog[key] for key in self.original}, self.original)
        for plan, monthly in MONTHLY.items():
            for cycle in billing.CYCLES:
                price = self.catalog[billing.price_lookup(plan, cycle)]
                self.assertEqual(price['unit_amount'], monthly * (1000 if cycle == 'annual' else 100))
                self.assertEqual(price['product'], 'prod_' + plan)
                self.assertEqual(billing.price_id(plan, cycle), price['id'])
        for call in self.stripe.Price.create.call_args_list:
            self.assertEqual(call.kwargs['idempotency_key'], 'tp-price-' + call.kwargs['lookup_key'])
            self.assertNotIn('transfer_lookup_key', call.kwargs)
        for resource in (self.stripe.Subscription, self.stripe.Customer, self.stripe.Invoice,
                         self.stripe.Product):
            self.assertFalse(resource.mock_calls)
        self.stripe.Price.modify.assert_not_called()

    def test_mismatched_catalog_is_rejected_before_startup_or_checkout(self):
        for field, bad_value in [('unit_amount', 1900), ('currency', 'eur'), ('active', False),
                                 ('type', 'one_time'), ('recurring', {'interval': 'year'})]:
            with self.subTest(field=field):
                price = catalog_price('micro', 'monthly')
                price[field] = bad_value
                self.catalog[price['lookup_key']] = price
                billing._price_cache.clear()
                with self.assertRaises(ValueError):
                    billing.sync_price_catalog()
                self.assertFalse(billing._price_cache)
                with self.assertRaises(ValueError):
                    billing.price_id('micro', 'monthly')
        with self.assertRaises(ValueError):
            with TestClient(app.app):
                pass
        self.stripe.checkout.Session.create.assert_not_called()

    def test_checkout_uses_current_monthly_and_annual_prices_without_extra_founding_discount(self):
        billing.sync_price_catalog()
        with patch.object(billing, '_get_or_create_customer', return_value='cus_example'), \
                patch.object(billing, '_claim_spot') as claim, \
                patch.object(billing, 'FOUNDING_COUPON_ID', 'old_founding_coupon'):
            for plan in MONTHLY:
                for cycle in billing.CYCLES:
                    with self.subTest(plan=plan, cycle=cycle):
                        url = billing.create_checkout_session({'id': 1}, plan, cycle, 'https://www.tuitionping.com')
                        self.assertEqual(url, 'https://checkout.stripe.com/test')
                        kw = self.stripe.checkout.Session.create.call_args.kwargs
                        self.assertEqual(kw['line_items'], [{'price': catalog_price(plan, cycle)['id'], 'quantity': 1}])
                        self.assertEqual(kw['subscription_data']['trial_period_days'], 30)
                        self.assertEqual(kw['subscription_data']['metadata']['founding'], '0')
                        self.assertNotIn('discounts', kw)
                        self.assertTrue(kw['allow_promotion_codes'])
            claim.assert_not_called()

    def test_postcard_campaign_keeps_its_coupon(self):
        billing.sync_price_catalog()
        with patch.object(billing, '_get_or_create_customer', return_value='cus_example'), \
                patch.object(billing, 'POSTCARD_COUPON_ID', 'postcard_coupon'):
            billing.create_checkout_session({'id': 1}, 'micro', 'monthly', 'https://www.tuitionping.com', postcard=True)
        kw = self.stripe.checkout.Session.create.call_args.kwargs
        self.assertEqual(kw['discounts'], [{'coupon': 'postcard_coupon'}])
        self.assertNotIn('allow_promotion_codes', kw)
        self.assertEqual(kw['subscription_data']['metadata']['source'], 'postcard')

    def test_demo_startup_never_uses_stripe(self):
        with patch.object(billing, 'stripe_configured', return_value=False):
            self.assertEqual(billing.sync_price_catalog(), {})
        self.assertFalse(self.stripe.mock_calls)

    def test_webhook_plan_recognition_accepts_both_catalogs_and_rejects_unknown_keys(self):
        for plan in MONTHLY:
            for cycle in billing.CYCLES:
                for legacy in (True, False):
                    price = catalog_price(plan, cycle, legacy)
                    self.assertEqual(billing.plan_from_lookup(price['lookup_key']), plan)
                    self.stripe.Price.retrieve.return_value = price
                    self.assertEqual(billing._plan_from_price({'items': {'data': [{'price': {'id': price['id']}}]}}), plan)
        for key in ('another_product', 'tuitionping_micro_monthly_unrecognized', None):
            self.assertIsNone(billing.plan_from_lookup(key))


class ReferralPriceTest(unittest.TestCase):
    def setUp(self):
        self.stripe = MagicMock()
        self.sub = {'plan': 'micro', 'stripe_customer_id': 'cus_example', 'stripe_subscription_id': 'sub_example'}

    def remote(self, plan, cycle, legacy=False):
        return {'customer': 'cus_example', 'livemode': True,
                'items': {'data': [{'quantity': 1, 'price': catalog_price(plan, cycle, legacy)}]}}

    def test_one_month_credit_uses_actual_price_for_legacy_and_new_monthly_and_annual_plans(self):
        for plan in MONTHLY:
            self.sub['plan'] = plan
            for cycle in billing.CYCLES:
                for legacy in (True, False):
                    self.stripe.Subscription.retrieve.return_value = self.remote(plan, cycle, legacy)
                    expected = (LEGACY_MONTHLY if legacy else MONTHLY)[plan] * 100
                    self.assertEqual(billing.monthly_credit_cents(self.sub, self.stripe), expected)

    def test_unverified_or_mismatched_subscription_price_never_guesses_credit(self):
        for change in ('customer', 'test_mode', 'missing_item', 'quantity', 'plan', 'currency', 'amount', 'interval'):
            with self.subTest(change=change):
                remote = self.remote('micro', 'monthly')
                item = remote['items']['data'][0]
                price = item['price']
                if change == 'customer': remote['customer'] = 'cus_other'
                if change == 'test_mode': remote['livemode'] = False
                if change == 'missing_item': remote['items']['data'] = []
                if change == 'quantity': item['quantity'] = 2
                if change == 'plan': price['lookup_key'] = billing.price_lookup('growth', 'monthly')
                if change == 'currency': price['currency'] = 'eur'
                if change == 'amount': price['unit_amount'] = 0
                if change == 'interval': price['recurring']['interval'] = 'week'
                self.stripe.Subscription.retrieve.return_value = remote
                with self.assertRaises(ValueError):
                    billing.monthly_credit_cents(self.sub, self.stripe)
        self.stripe.Customer.create_balance_transaction.assert_not_called()

    def test_price_id_reference_is_resolved(self):
        remote = self.remote('micro', 'monthly')
        price = remote['items']['data'][0]['price']
        remote['items']['data'][0]['price'] = price['id']
        self.stripe.Subscription.retrieve.return_value = remote
        self.stripe.Price.retrieve.return_value = price
        self.assertEqual(billing.monthly_credit_cents(self.sub, self.stripe), 900)
        self.stripe.Price.retrieve.assert_called_once_with(price['id'])


class PublishedPricingTest(unittest.TestCase):
    def test_landing_schema_signup_and_comparison_publish_the_current_catalog(self):
        with TestClient(app.app) as client:
            response = client.get('/')
            self.assertEqual(response.status_code, 200)
            schema = json.loads(re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', response.text, re.S).group(1))
            offers = next(item['offers'] for item in schema['@graph'] if item['@type'] == 'SoftwareApplication')
            self.assertEqual([int(offer['price']) for offer in offers], [9, 19, 39, 59])
            self.assertNotIn('FOUNDING MEMBERS', response.text)
            self.assertNotIn('50% off for 6 months', response.text)
            self.assertIn('var MONTHLY_COST = 9;', response.text)
            self.assertIn('TuitionPing Micro is $9/mo', response.text)
            self.assertIn('30-day free trial', response.text)
            for plan, monthly in MONTHLY.items():
                with self.subTest(plan=plan):
                    self.assertIn(f'${monthly}<', response.text)
                    signup = client.get('/signup?plan=' + plan)
                    self.assertEqual(signup.status_code, 200)
                    self.assertIn(f'Monthly — ${monthly}/mo', signup.text)
                    self.assertIn(f'Annual — ${monthly * 10}/yr', signup.text)
            compare = client.get('/compare/brightwheel')
            self.assertEqual(compare.status_code, 200)
            self.assertIn('$9–$59/mo', compare.text)
            self.assertNotIn('$19–$99/mo', compare.text)

    def test_existing_founding_subscriber_sees_preserved_terms_and_current_catalog(self):
        provider = {'id': 999999, 'name': 'Example', 'email': 'example@example.invalid'}
        summary = {'plan': 'micro', 'status': 'active', 'founding': True, 'trial_ends_at': None}
        with patch.object(app, 'require_login', return_value=(provider, None)), \
                patch.object(billing, 'subscription_summary', return_value=summary), \
                TestClient(app.app) as client:
            response = client.get('/billing')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Founding member', response.text)
        self.assertIn('Existing subscription prices and discounts continue', response.text)
        for monthly in MONTHLY.values():
            self.assertIn(f'<b>${monthly}/mo</b>', response.text)
            self.assertIn(f'or ${monthly * 10}/yr', response.text)
        self.assertNotIn('founding price for', response.text)
        self.assertNotIn('spots are all claimed', response.text)


if __name__ == '__main__':
    unittest.main()
