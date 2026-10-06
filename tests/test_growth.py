"""Acquisition integrity, privacy, isolation and real/test billing boundaries."""
import json
import re
import secrets
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from test_late_fee_page import app, store
import billing
import growth
import sms
from fastapi.testclient import TestClient

class GrowthTest(unittest.TestCase):
    def setUp(self):
        growth.ensure_tables()
        with store.db() as conn:
            for table in ('growth_events','growth_accounts','growth_visitors'):
                conn.execute('DELETE FROM '+table)
        self.client=TestClient(app.app,base_url='https://www.tuitionping.com',headers={'user-agent':'Mozilla/5.0 Test browser'})
    def tearDown(self):
        self.client.close()
    def counts(self):
        return {s['event']:s['count'] for s in growth.report()['stages']}
    def visit(self, path='/demo'):
        r=self.client.get(path)
        self.assertEqual(r.status_code,200)
        return re.search('name="tp-analytics-token" content="([a-f0-9]+)"',r.text).group(1)
    def account(self):
        self.visit()
        vid=growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        pid=store.create_provider('Sample',secrets.token_hex(6)+'@example.invalid','password123')
        growth.bind_account(vid,pid)
        return pid
    def test_demo_and_audiences_are_public_and_linked(self):
        provider={'id':1,'email':'sample@example.invalid','name':'Test','suspended':False,'is_admin':False}
        with patch.object(store,'get_provider_by_session',return_value=provider), patch.object(store,'get_subscription',return_value={'status':'expired'}),patch.object(sms,'send_sms') as send:
            self.client.cookies.set(app.SESSION_COOKIE,'sample')
            for path in ['/demo']+list(app.AUDIENCES):
                r=self.client.get(path,follow_redirects=False)
                self.assertEqual(r.status_code,200)
                self.assertEqual(len(re.findall(r'<h1[ >]',r.text)),1)
                self.assertIn('https://www.tuitionping.com'+path,r.text)
                self.assertIn(path,self.client.get('/sitemap.xml').text)
            send.assert_not_called()
        for path in app.AUDIENCES:
            self.assertIn(path,self.client.get('/').text)
    def test_signed_cookie_first_touch_and_private_query_exclusion(self):
        self.visit('/demo?utm_source=postcards&utm_medium=mail&utm_campaign=fall&email=secret@example.invalid')
        self.visit('/guides?utm_source=other')
        self.client.get('/static/downloads/daycare-tuition-collection-kit.zip')
        report=growth.report();self.assertEqual(len(report['sources']),1)
        self.assertEqual(report['sources'][0]['key'],('postcards','mail','fall'))
        self.assertEqual(report['pages'][0]['key'],('/demo',))
        self.assertEqual(self.counts()['download'],1)
        with store.db() as conn:
            saved=str([dict(r) for r in conn.execute('SELECT * FROM growth_visitors').fetchall()])
        self.assertNotIn('secret',saved);self.assertNotIn('email',saved)
        self.assertEqual(growth.visitor_from_cookie('a'*32+'.forged'),'')
    def test_client_cannot_forge_business_milestones_or_private_paths(self):
        token=self.visit()
        def post(data, token_=token):return self.client.post('/analytics/event',json=data,headers={'x-tp-analytics':token_})
        self.assertEqual(post({'event':'demo_started','path':'/demo'}).status_code,204)
        self.assertEqual(post({'event':'demo_started','path':'/demo'}).status_code,204)
        self.assertEqual(self.counts()['demo_started'],1)
        for data in [{'event':'paid_customer','path':'/demo'},{'event':'demo_step','detail':'secret','path':'/demo'},{'event':'trial_click','path':'/signup?email=secret'},['bad']]:
            self.assertEqual(post(data).status_code,400)
        self.assertEqual(post({'event':'demo_started','path':'/demo'},'forged').status_code,403)
        self.assertEqual(self.counts()['paid_customer'],0)
    def test_opt_out_and_bots_do_not_receive_conversion_cookies(self):
        for headers in [{'dnt':'1'},{'sec-gpc':'1'},{'user-agent':'Googlebot'}]:
            with TestClient(app.app,base_url='https://www.tuitionping.com',headers={'user-agent':'Mozilla/5.0',**headers}) as c:
                r=c.get('/demo');self.assertNotIn(growth.COOKIE,r.cookies)
                self.assertNotIn('tp-analytics-token',r.text)
        self.assertEqual(self.counts()['page_view'],0)
    def test_account_milestones_deduplicate_per_account_not_browser(self):
        a=self.account();b=self.account()
        for pid in (a,b):
            growth.milestone(pid,'paid_customer');growth.milestone(pid,'paid_customer')
        self.assertEqual(self.counts()['page_view'],1)
        self.assertEqual(self.counts()['signup'],2)
        self.assertEqual(self.counts()['paid_customer'],2)
        self.assertEqual(growth.report()['sources'][0]['paid'],2)
    def test_signup_links_browser_and_checkout_but_not_trial(self):
        r=self.client.get('/signup')
        csrf=re.search('name="csrf_token" value="([a-f0-9]+)"',r.text).group(1)
        with patch.object(billing,'DEMO_MODE',False),patch.object(billing,'stripe_configured',return_value=True),patch.object(billing,'create_checkout_session',return_value='https://checkout.stripe.com/test'),patch.object(app,'send_welcome_email'),patch.object(app,'EMAIL_ACTIVE',False):
            r=self.client.post('/signup',data={'name':'Sample','email':secrets.token_hex(6)+'@example.invalid','password':'password123','agree_terms':'on','attest_consent':'on','csrf_token':csrf},follow_redirects=False)
        self.assertEqual(r.status_code,303);self.assertEqual(r.headers['location'],'https://checkout.stripe.com/test')
        self.assertEqual(self.counts()['signup'],1);self.assertEqual(self.counts()['checkout_started'],1)
        self.assertEqual(self.counts()['trial_started'],0);self.assertEqual(self.counts()['paid_customer'],0)
    def test_paid_invoice_requires_live_positive_subscription_payment(self):
        pid=self.account()
        with patch.object(billing,'_stripe_lib',return_value=MagicMock()),patch.object(store,'get_provider_by_stripe_customer',return_value={'id':pid}):
            for live,amount,subscription in [(False,1900,'sub_1'),(True,0,'sub_1'),(True,1900,None)]:
                billing.handle_stripe_event({'type':'invoice.paid','data':{'object':{'livemode':live,'amount_paid':amount,'subscription':subscription,'customer':'cus_1'}}})
            self.assertEqual(self.counts()['paid_customer'],0)
            event={'type':'invoice.paid','data':{'object':{'livemode':True,'amount_paid':1900,'parent':{'subscription_details':{'subscription':'sub_1'}},'customer':'cus_1'}}}
            billing.handle_stripe_event(event);billing.handle_stripe_event(event)
        self.assertEqual(self.counts()['paid_customer'],1)
    def test_checkout_completion_is_server_confirmed_and_test_mode_excluded(self):
        pid=self.account()
        stripe=MagicMock()
        stripe.Subscription.retrieve.return_value={'id':'sub_1','customer':'cus_1','livemode':False,'status':'trialing','metadata':{'provider_id':str(pid),'plan':'micro'}}
        event={'type':'checkout.session.completed','data':{'object':{'livemode':False,'subscription':'sub_1'}}}
        with patch.object(billing,'_stripe_lib',return_value=stripe),patch.object(billing,'_maybe_grant_referral_reward'):
            billing.handle_stripe_event(event)
            self.assertEqual(self.counts()['checkout_completed'],0)
            self.assertEqual(self.counts()['trial_started'],0)
            event['data']['object']['livemode']=True
            stripe.Subscription.retrieve.return_value['livemode']=True
            billing.handle_stripe_event(event);billing.handle_stripe_event(event)
        self.assertEqual(self.counts()['checkout_completed'],1)
        self.assertEqual(self.counts()['trial_started'],1)
        self.assertEqual(self.counts()['paid_customer'],0)
    def test_subscription_sync_confirms_trial_and_invoice_without_new_webhook_configuration(self):
        pid=self.account()
        sub={'id':'sub_1','customer':'cus_1','metadata':{'plan':'micro'},'livemode':True,'status':'trialing'}
        billing._sync_from_subscription(pid,sub)
        self.assertEqual(self.counts()['trial_started'],1);self.assertEqual(self.counts()['paid_customer'],0)
        sub.update(status='active',latest_invoice='in_1')
        stripe=MagicMock();stripe.Invoice.retrieve.return_value={'livemode':True,'status':'paid','amount_paid':0}
        with patch.object(billing,'_stripe_lib',return_value=stripe):
            billing._sync_from_subscription(pid,sub)
            self.assertEqual(self.counts()['paid_customer'],0)
            stripe.Invoice.retrieve.return_value={'livemode':True,'status':'paid','amount_paid':1900}
            billing._sync_from_subscription(pid,sub);billing._sync_from_subscription(pid,sub)
        self.assertEqual(self.counts()['paid_customer'],1)
    def test_demo_sms_never_counts_as_first_real_text(self):
        pid=self.account()
        with patch.object(sms,'DEMO_MODE',True):sms.send_sms('+15555550100','Fictional test',pid,999,tuition_reminder=True)
        self.assertEqual(self.counts()['first_reminder'],0)
        with patch.object(sms,'DEMO_MODE',False),patch.object(sms,'_real_credentials_present',return_value=True),patch('twilio.rest.Client') as client:
            client.return_value.messages.create.return_value.sid='SM_sample'
            sms.send_sms('+15555550100','Fictional test',pid,999)
            self.assertEqual(self.counts()['first_reminder'],0)
            sms.send_sms('+15555550100','Fictional test',pid,999,tuition_reminder=True)
        self.assertEqual(self.counts()['first_reminder'],1)
    def test_admin_report_is_private_and_analytics_failure_does_not_break_pages(self):
        self.assertNotEqual(self.client.get('/admin/conversions',follow_redirects=False).status_code,200)
        with patch.object(growth,'register',side_effect=RuntimeError('offline')):
            self.assertEqual(self.client.get('/demo').status_code,200)
        provider={'id':1,'name':'Admin','is_admin':True}
        with patch.object(app,'require_admin',return_value=(provider,None)):
            r=self.client.get('/admin/conversions');self.assertEqual(r.status_code,200)
            self.assertIn('From first visit to customer',r.text)
            self.assertEqual(r.headers['cache-control'],'private, no-store')
    def test_retention_removes_old_events_and_account_links(self):
        pid=self.account()
        old=(datetime.now(timezone.utc)-timedelta(days=91)).isoformat(timespec='seconds')
        with store.db() as conn:
            conn.execute('UPDATE growth_visitors SET first_seen = ?', (old,))
            conn.execute('UPDATE growth_events SET ts = ?', (old,))
        growth.register('b'*32,'direct','none','','/demo')
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS c FROM growth_accounts').fetchone()['c'],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) AS c FROM growth_events').fetchone()['c'],0)

if __name__=='__main__':unittest.main()
