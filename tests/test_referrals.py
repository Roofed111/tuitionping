"""Referral timing, attribution, live payment validation and durable delivery."""
import copy
import re
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import billing
import referrals


def ts(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp())


class Page:
    def __init__(self, data):
        self.data = data

    def auto_paging_iter(self):
        return iter(self.data)


class ReferralTest(unittest.TestCase):
    def setUp(self):
        referrals.ensure_tables()
        with store.db() as conn:
            for table in ('referral_credits', 'referral_progress', 'referral_rewards'):
                conn.execute('DELETE FROM ' + table)
            conn.execute('UPDATE providers SET referred_by = NULL')
        self.referrer = self.provider('growth', 'cus_referrer', 'sub_referrer')
        self.referee = self.provider('micro', 'cus_referee', 'sub_referee')
        store.set_referred_by(self.referee, self.referrer)
        self.start, self.end = ts('2026-10-07'), ts('2026-11-07')
        self.clock = self.start
        self.invoices = {}
        self.subscriptions = {'sub_referee': {'id': 'sub_referee', 'customer': 'cus_referee',
            'status': 'active', 'livemode': True, 'metadata': {'provider_id': str(self.referee)},
            'items': {'data': [{'quantity': 1, 'price': {'lookup_key': 'tuitionping_micro_monthly',
                'unit_amount': 1900, 'currency': 'usd', 'recurring': {'interval': 'month'}}}]}},
            'sub_referrer': {'id': 'sub_referrer', 'customer': 'cus_referrer',
                'status': 'active', 'livemode': True,
                'items': {'data': [{'quantity': 1, 'price': {'lookup_key': 'tuitionping_growth_monthly',
                    'unit_amount': 5900, 'currency': 'usd', 'recurring': {'interval': 'month'}}}]}}}
        self.transactions = []
        self.creates = []
        self.fail_customer = None
        self.lose_response = False
        self.stripe = MagicMock()
        self.stripe.Subscription.retrieve.side_effect = lambda sid: self.subscriptions[sid]
        self.stripe.Invoice.retrieve.side_effect = lambda iid: copy.deepcopy(self.invoices[iid])
        self.stripe.Invoice.list.side_effect = lambda **kw: Page([copy.deepcopy(i) for i in self.invoices.values() if referrals.subscription_id(i) == kw['subscription']])
        self.stripe.Charge.retrieve.return_value = {'id': 'ch_1', 'refunded': False, 'amount_refunded': 0, 'disputed': False}
        self.stripe.Customer.list_balance_transactions.side_effect = lambda customer, **kw: Page([t for t in self.transactions if t['customer'] == customer])
        self.stripe.Customer.create_balance_transaction.side_effect = self.create_credit
        for p in (patch.object(billing, '_stripe_lib', return_value=self.stripe),
                  patch.object(billing, 'stripe_configured', return_value=True),
                  patch.object(billing, 'sync_price_catalog'),
                  patch.object(referrals, 'now_ts', side_effect=lambda: self.clock)):
            p.start(); self.addCleanup(p.stop)

    def provider(self, plan, customer, subscription):
        pid = store.create_provider('Example daycare', secrets.token_hex(8) + '@example.invalid', 'password123')
        store.set_subscription(pid, plan, 'active')
        store.set_stripe_ids(pid, customer, subscription)
        return pid

    def create_credit(self, customer, **kw):
        self.creates.append((customer, kw))
        if customer == self.fail_customer:
            raise RuntimeError('Temporary Stripe failure')
        result = {'id': 'cbtxn_' + str(len(self.transactions) + 1), 'customer': customer,
                  'amount': kw['amount'], 'currency': kw['currency'], 'metadata': kw['metadata']}
        self.transactions.append(result)
        if self.lose_response:
            self.lose_response = False
            raise RuntimeError('Response lost after Stripe accepted the credit')
        return result

    def invoice(self, **changes):
        inv = {'id': 'in_first', 'livemode': True, 'status': 'paid', 'amount_paid': 1900,
               'billing_reason': 'subscription_cycle', 'subscription': 'sub_referee',
               'customer': 'cus_referee', 'charge': 'ch_1', 'lines': {'data': [{
                   'type': 'subscription', 'amount': 1900, 'proration': False,
                   'price': {'lookup_key': 'tuitionping_micro_monthly',
                             'recurring': {'interval': 'month', 'interval_count': 1}},
                   'period': {'start': self.start, 'end': self.end}}], 'has_more': False}}
        inv.update(changes)
        return inv

    def save(self, invoice=None):
        inv = invoice or self.invoice()
        self.invoices[inv['id']] = copy.deepcopy(inv)
        referrals.record_paid_invoice(self.referee, inv)
        return inv

    def progress(self):
        with store.db() as conn:
            row = conn.execute('SELECT * FROM referral_progress WHERE referee_id = ?', (self.referee,)).fetchone()
        return dict(row) if row else None

    def test_checkout_starts_trial_without_issuing_credit(self):
        with patch.object(billing, '_sync_from_subscription') as sync:
            billing.handle_stripe_event({'type': 'checkout.session.completed', 'data': {'object': {
                'subscription': 'sub_referee', 'metadata': {'provider_id': str(self.referee)}}}})
        sync.assert_called_once()
        self.assertFalse(self.creates)
        self.assertIsNone(self.progress())

    def test_first_paid_invoice_stays_pending_until_full_month_ends(self):
        self.save()
        self.clock = self.end - 1
        referrals.run()
        self.assertFalse(self.transactions)
        self.assertEqual(store.referral_stats(self.referrer)['pending'], 1)
        self.clock = self.end
        result = referrals.run()
        self.assertEqual(result['granted'], 1)
        self.assertEqual(sorted(t['amount'] for t in self.transactions), [-5900, -1900])
        self.assertEqual(store.referral_stats(self.referrer)['earned_months'], 1)
        self.assertEqual(store.referral_stats(self.referrer)['pending'], 0)

    def test_zero_trial_failed_test_manual_and_update_invoices_never_qualify(self):
        for changes in ({'amount_paid': 0}, {'livemode': False}, {'status': 'open'},
                        {'subscription': None}, {'paid_out_of_band': True},
                        {'billing_reason': 'subscription_update'}, {'customer': 'cus_wrong'},
                        {'subscription': 'sub_wrong'}):
            with self.subTest(changes=changes):
                self.save(self.invoice(**changes))
                self.assertIsNone(self.progress())

    def test_prorations_partial_months_and_unrelated_products_do_not_qualify(self):
        for kind in ('proration', 'short', 'unrelated'):
            inv = self.invoice(); line = inv['lines']['data'][0]
            if kind == 'proration': line['proration'] = True
            if kind == 'short': line['period']['end'] = self.start + 10 * 86400
            if kind == 'unrelated': line['price']['lookup_key'] = 'another_product'
            self.save(inv)
            self.assertIsNone(self.progress())

    def test_annual_qualifies_after_calendar_month_not_year(self):
        inv = self.invoice(); line = inv['lines']['data'][0]
        line['price'] = {'lookup_key': 'tuitionping_micro_annual', 'recurring': {'interval': 'year'}}
        line['period']['end'] = ts('2027-10-07')
        self.save(inv)
        self.assertEqual(self.progress()['eligible_at'], self.end)
        self.clock = self.end
        referrals.run()
        self.assertEqual(len(self.transactions), 2)

    def test_new_catalog_referral_credits_match_actual_prices_after_full_month(self):
        inv = self.invoice(amount_paid=900)
        line = inv['lines']['data'][0]
        line['amount'] = 900
        line['price']['lookup_key'] = billing.price_lookup('micro', 'monthly')
        for sid, plan, amount in [('sub_referee', 'micro', 900), ('sub_referrer', 'growth', 3900)]:
            price = self.subscriptions[sid]['items']['data'][0]['price']
            price.update(lookup_key=billing.price_lookup(plan, 'monthly'), unit_amount=amount)
        self.save(inv)
        self.clock = self.end - 1
        referrals.run()
        self.assertFalse(self.transactions)
        self.clock = self.end
        self.assertEqual(referrals.run()['granted'], 1)
        self.assertEqual(sorted(t['amount'] for t in self.transactions), [-3900, -900])

    def test_new_annual_catalog_referral_uses_nominal_month(self):
        inv = self.invoice(amount_paid=9000)
        line = inv['lines']['data'][0]
        line.update(amount=9000, price={'lookup_key': billing.price_lookup('micro', 'annual'),
                                     'recurring': {'interval': 'year'}})
        line['period']['end'] = ts('2027-10-07')
        price = self.subscriptions['sub_referee']['items']['data'][0]['price']
        price.update(lookup_key=billing.price_lookup('micro', 'annual'), unit_amount=9000,
                     recurring={'interval': 'year'})
        self.save(inv)
        self.assertEqual(self.progress()['eligible_at'], self.end)
        self.clock = self.end
        self.assertEqual(referrals.run()['granted'], 1)
        self.assertEqual(sorted(t['amount'] for t in self.transactions), [-5900, -900])

    def test_month_end_and_leap_year_clamp(self):
        self.assertEqual(referrals._next_month(ts('2026-01-31')), ts('2026-02-28'))
        self.assertEqual(referrals._next_month(ts('2028-01-31')), ts('2028-02-29'))
        self.assertEqual(referrals._next_month(ts('2026-12-31')), ts('2027-01-31'))

    def test_current_stripe_invoice_and_line_shapes(self):
        inv = self.invoice(subscription=None, parent={'subscription_details': {'subscription': 'sub_referee'}})
        line = inv['lines']['data'][0]
        line.pop('type'); line.pop('price'); line.pop('proration')
        line['parent'] = {'type': 'subscription_item_details', 'subscription_item_details': {'proration': False}}
        line['pricing'] = {'price_details': {'price': 'price_micro'}}
        self.stripe.Price.retrieve.return_value = {'lookup_key': 'tuitionping_micro_monthly', 'recurring': {'interval': 'month'}}
        self.save(inv); self.clock = self.end; referrals.run()
        self.assertEqual(len(self.transactions), 2)

    def test_duplicate_webhooks_and_runs_do_not_duplicate_credit(self):
        inv = self.save()
        for event in ('invoice.paid', 'invoice.payment_succeeded'):
            billing.handle_stripe_event({'type': event, 'data': {'object': inv}})
        self.clock = self.end
        referrals.run(); referrals.run()
        self.assertEqual(len(self.transactions), 2)
        self.assertEqual(store.referral_stats(self.referrer)['earned_months'], 1)

    def test_missed_webhook_is_recovered_by_cron(self):
        inv = self.invoice(); self.invoices[inv['id']] = inv
        self.clock = self.end
        referrals.run()
        self.assertEqual(len(self.transactions), 2)

    def test_out_of_order_invoices_keep_earliest_paid_month(self):
        later = self.invoice(id='in_later'); later['lines']['data'][0]['period'] = {'start': self.end, 'end': ts('2026-12-07')}
        self.save(later); self.save()
        self.assertEqual(self.progress()['invoice_id'], 'in_first')

    def test_referrer_failure_remains_pending_and_referee_is_not_credited_again(self):
        self.save(); self.clock = self.end; self.fail_customer = 'cus_referrer'
        referrals.run()
        self.assertEqual(len(self.transactions), 1)
        self.assertFalse(store.referral_reward_granted(self.referee))
        self.assertEqual(store.referral_stats(self.referrer)['earned_months'], 0)
        self.fail_customer = None; self.clock += 3600
        referrals.run()
        self.assertEqual(len(self.transactions), 2)
        self.assertTrue(store.referral_reward_granted(self.referee))

    def test_lost_response_is_recovered_even_after_idempotency_retention(self):
        self.save(); self.clock = self.end; self.lose_response = True
        referrals.run()
        self.assertEqual(len(self.transactions), 2)
        self.assertFalse(store.referral_reward_granted(self.referee))
        self.clock += 2 * 86400
        referrals.run()
        self.assertEqual(len(self.transactions), 2)
        self.assertEqual(len(self.creates), 2)
        self.assertTrue(store.referral_reward_granted(self.referee))

    def test_legacy_rewards_are_preserved_and_never_reissued(self):
        store.record_referral_reward(self.referrer, self.referee)
        self.save(); self.clock = self.end; referrals.run()
        self.assertFalse(self.creates)
        self.assertEqual(store.referral_stats(self.referrer)['earned_months'], 1)

    def test_early_cancellation_disqualifies_but_end_of_month_cancellation_counts(self):
        self.save(); self.clock = self.end
        self.subscriptions['sub_referee'].update(status='canceled', ended_at=self.end - 1)
        referrals.run()
        self.assertEqual(self.progress()['state'], 'ineligible')
        self.assertFalse(self.creates)

    def test_cancellation_requested_early_but_service_completes_month(self):
        self.save(); self.clock = self.end
        self.subscriptions['sub_referee'].update(status='canceled', canceled_at=self.start + 1, ended_at=self.end)
        store.set_subscription(self.referee, 'micro', 'canceled')
        referrals.run()
        self.assertEqual(len(self.transactions), 2)

    def test_refunded_disputed_or_credited_back_payment_disqualifies(self):
        for change in ({'amount_refunded': 100}, {'disputed': True}, {'refunded': True}):
            self.stripe.Charge.retrieve.return_value = {'id': 'ch_1', **change}
            self.assertTrue(referrals._payment_reversed(self.invoice(), self.stripe))
        self.save(); self.clock = self.end
        self.invoices['in_first']['post_payment_credit_notes_amount'] = 100
        referrals.run()
        self.assertFalse(self.creates)
        self.assertEqual(self.progress()['state'], 'ineligible')

    def test_modern_invoice_payment_refund_is_detected(self):
        inv = self.invoice(); inv.pop('charge')
        self.stripe.InvoicePayment.list.return_value = Page([{'payment': {'type': 'payment_intent', 'payment_intent': 'pi_1'}}])
        self.stripe.PaymentIntent.retrieve.return_value = {'latest_charge': {'id': 'ch_1', 'amount_refunded': 1900}}
        self.assertTrue(referrals._payment_reversed(inv, self.stripe))

    def test_stripe_validation_failure_keeps_credit_pending(self):
        self.save(); self.clock = self.end
        self.stripe.Invoice.retrieve.side_effect = RuntimeError('Stripe temporarily unavailable')
        self.assertEqual(referrals.run()['errors'], 1)
        self.assertEqual(self.progress()['state'], 'pending')
        self.assertFalse(self.transactions)

    def test_self_referral_and_demo_mode_never_issue_credit(self):
        store.set_referred_by(self.referee, self.referee)
        self.save(); self.clock = self.end; referrals.run()
        self.assertFalse(self.creates)
        store.set_referred_by(self.referee, self.referrer); self.save()
        with patch.object(billing, 'stripe_configured', return_value=False):
            self.assertTrue(referrals.run()['disabled'])
        self.assertFalse(self.creates)

    def test_concurrent_delivery_serializes_each_benefit(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            done = list(pool.map(lambda _: referrals._deliver_credit(self.referee, self.referrer, self.stripe), range(2)))
        self.assertEqual(done, [True, True])
        self.assertEqual(len(self.transactions), 1)

    def test_failed_credit_keeps_original_amount_after_plan_change(self):
        self.fail_customer = 'cus_referrer'
        with self.assertRaises(RuntimeError):
            referrals._deliver_credit(self.referee, self.referrer, self.stripe)
        store.set_subscription(self.referrer, 'multisite', 'active')
        self.fail_customer = None
        referrals._deliver_credit(self.referee, self.referrer, self.stripe)
        self.assertEqual(self.transactions[0]['amount'], -5900)
        self.assertEqual(self.creates[0][1]['idempotency_key'], self.creates[1][1]['idempotency_key'])

    def test_unverifiable_payment_stays_pending(self):
        inv = self.invoice(); inv.pop('charge')
        self.stripe.InvoicePayment.list.return_value = Page([])
        self.save(inv); self.clock = self.end
        self.assertEqual(referrals.run()['errors'], 1)
        self.assertEqual(self.progress()['state'], 'pending')
        self.assertFalse(self.creates)

    def test_hourly_cron_processes_referrals_and_requires_secret(self):
        with TestClient(app.app) as client, patch.object(app, 'INTERNAL_CRON_TOKEN', 'test-only-cron'), \
                patch.object(app, 'run_reminders', return_value=[]), \
                patch.object(app.email_engagement, 'run', return_value={}), \
                patch.object(app.setup_help, 'notify', return_value={}), \
                patch.object(referrals, 'run', return_value={'granted': 1, 'errors': 0}) as run:
            self.assertEqual(client.get('/internal/run-reminders?token=wrong').status_code, 401)
            run.assert_not_called()
            response = client.get('/internal/run-reminders?token=test-only-cron')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['referrals']['granted'], 1)
            run.assert_called_once()

    def test_dashboard_distinguishes_pending_from_issued_credit(self):
        store.set_email_verified(self.referrer, True)
        token = store.create_session(self.referrer)
        with TestClient(app.app) as client:
            client.cookies.set(app.SESSION_COOKIE, token)
            response = client.get('/dashboard')
            self.assertEqual(response.status_code, 200)
            self.assertIn('first paid month', response.text)
            self.assertIn('1 referral pending', response.text)
            self.assertNotIn('1 month of account credit issued', response.text)
            self.save(); self.clock = self.end; referrals.run()
            response = client.get('/dashboard')
            self.assertIn('1 month of account credit issued', response.text)
            self.assertNotIn('1 referral pending', response.text)

    def test_referral_link_prefills_signup_and_records_attribution(self):
        code = store.get_provider(self.referrer)['referral_code']
        email = secrets.token_hex(8) + '@example.invalid'
        with TestClient(app.app, base_url='https://www.tuitionping.com') as client:
            page = client.get('/signup?ref=' + code)
            self.assertIn('value="' + code + '"', page.text)
            self.assertIn('complete your first paid month', page.text)
            token = re.search('name="csrf_token" value="([a-f0-9]+)"', page.text).group(1)
            with patch.object(billing, 'DEMO_MODE', True), patch.object(app, 'EMAIL_ACTIVE', False), patch.object(app, 'send_welcome_email'):
                response = client.post('/signup', data={'name': 'Example', 'email': email, 'password': 'password123',
                    'referral_code': code, 'csrf_token': token, 'agree_terms': 'on', 'attest_consent': 'on'}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
        pid = store.get_provider_by_email(email)['id']
        self.assertEqual(store.get_referred_by(pid), self.referrer)
        self.assertFalse(store.referral_reward_granted(pid))


if __name__ == '__main__':
    unittest.main()
