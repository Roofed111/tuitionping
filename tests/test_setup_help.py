"""Setup leads, access controls and founder-only notification recovery."""
import re
import secrets
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import setup_help as help
import growth
import email_engagement

class SetupHelpTest(unittest.TestCase):
    def setUp(self):
        help.ensure_tables();growth.ensure_tables();email_engagement.ensure_tables()
        with store.db() as conn:
            conn.execute('DELETE FROM setup_help_requests');conn.execute('DELETE FROM setup_help_limits')
        self.client=TestClient(app.app,base_url='https://www.tuitionping.com',headers={'user-agent':'Mozilla/5.0 Test browser'})
        self.addCleanup(self.client.close)
        self.sender=patch.object(app,'send_email',return_value=True).start()
        self.active=patch.object(app,'EMAIL_ACTIVE',True).start()
        self.addCleanup(patch.stopall)
    def form(self):
        r=self.client.get('/setup-help')
        self.assertEqual(r.status_code,200)
        return {'csrf_token':re.search('name="csrf_token" value="([^"]+)"',r.text).group(1),
                'request_key':re.search('name="request_key" value="([^"]+)"',r.text).group(1),
                'name':'Sample owner','email':'owner@example.invalid','program':'Example daycare','families':'1-10','stage':'exploring','topic':'csv','note':'Help with my spreadsheet.','contact_permission':'on'}
    def count(self):
        with store.db() as conn:return conn.execute('SELECT COUNT(*) AS n FROM setup_help_requests').fetchone()['n']
    def test_public_page_csrf_cache_and_entry_points(self):
        data=self.form()
        r=self.client.get('/setup-help');self.assertIn('no-store',r.headers['cache-control']);self.assertEqual(r.headers['x-robots-tag'],'noindex')
        for p in ['/','/support','/signup']:
            self.assertIn('href="/setup-help"',self.client.get(p).text)
        self.assertEqual(self.client.post('/setup-help',data={**data,'csrf_token':'bad'}).status_code,403)
        self.client.cookies.clear();self.assertEqual(self.client.post('/setup-help',data=data).status_code,403)
        self.assertEqual(self.count(),0);self.sender.assert_not_called()
    def test_request_persists_and_only_founder_is_notified_once(self):
        data=self.form();before=email_engagement.report()['confirmed']
        r=self.client.post('/setup-help',data=data);self.assertEqual(r.status_code,200);self.assertIn('request is saved',r.text)
        self.assertEqual(self.count(),1);self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0],'rob@tuitionping.com')
        self.assertIn('owner@example.invalid',self.sender.call_args.args[2]);self.assertIn('/admin/setup-help',self.sender.call_args.args[2])
        self.assertEqual(self.sender.call_args.kwargs['idempotency_key'],'setup-help-'+data['request_key'])
        self.assertNotIn('owner@example.invalid',r.text)
        self.assertEqual(email_engagement.report()['confirmed'],before)
        self.client.post('/setup-help',data=data);self.assertEqual(self.count(),1);self.sender.assert_called_once()
        row=help.report()['requests'][0];self.assertEqual(row['notification_status'],'accepted');self.assertEqual(row['consent_text'],help.CONSENT)
        with store.db() as conn:self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM growth_events WHERE event='setup_help_requested' AND visitor_id=?",(growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE)),)).fetchone()['n'],1)
    def test_validation_consent_honeypot_size_and_rate_limits(self):
        data=self.form()
        for changes in [{'email':'invalid'},{'name':''},{'note':'a'*1001},{'program':'a'*151},{'families':'999'},{'stage':'other'},{'topic':'private'},{'contact_permission':''},{'request_key':'bad'}]:
            self.client.headers['x-forwarded-for']=secrets.token_hex(8)
            self.assertEqual(self.client.post('/setup-help',data={**data,**changes}).status_code,400,changes)
        self.assertEqual(self.client.post('/setup-help',data={**data,'website':'spam'}).status_code,200)
        self.assertEqual(self.count(),0);self.sender.assert_not_called()
        self.assertEqual(self.client.post('/setup-help',data={**data,'note':'a'*17000}).status_code,413)
        self.client.headers['x-forwarded-for']='sample-rate-limit'
        for _ in range(5):self.assertEqual(self.client.post('/setup-help',data={**data,'request_key':secrets.token_hex(16)}).status_code,200)
        self.assertEqual(self.client.post('/setup-help',data=data).status_code,429)
        self.assertEqual(self.count(),5)
    def test_email_unavailable_or_failed_keeps_saved_request_and_stable_retry_key(self):
        data=self.form()
        with patch.object(app,'EMAIL_ACTIVE',False):
            self.assertEqual(self.client.post('/setup-help',data=data).status_code,200)
        self.sender.assert_not_called();row=help.report()['requests'][0];self.assertEqual(row['notification_status'],'pending')
        sender=Mock(return_value=False);self.assertEqual(help.notify(sender,True)['accepted'],0)
        with store.db() as conn:conn.execute('UPDATE setup_help_requests SET due_at=?',(help.stamp(help.now()-timedelta(minutes=1)),))
        sender.return_value=True;self.assertEqual(help.notify(sender,True)['accepted'],1)
        self.assertEqual(sender.call_args_list[0].kwargs['idempotency_key'],sender.call_args_list[1].kwargs['idempotency_key'])
        self.assertEqual(help.notify(sender,True)['accepted'],0);self.assertEqual(sender.call_count,2)
    def test_crashed_claim_retry_and_expired_window_do_not_blindly_resend(self):
        data=self.form()
        with patch.object(app,'EMAIL_ACTIVE',False):self.client.post('/setup-help',data=data)
        dt=datetime(2026,10,7,12,tzinfo=timezone.utc)
        with store.db() as conn:conn.execute("UPDATE setup_help_requests SET notification_status='sending',first_attempt_at=?,due_at=?",(help.stamp(dt-timedelta(minutes=10)),help.stamp(dt-timedelta(minutes=1))))
        sender=Mock(return_value=True)
        with patch.object(help,'now',return_value=dt):self.assertEqual(help.notify(sender,True)['accepted'],1)
        with store.db() as conn:conn.execute("UPDATE setup_help_requests SET notification_status='sending',first_attempt_at=?,due_at=?",(help.stamp(dt-timedelta(hours=24)),help.stamp(dt-timedelta(minutes=1))))
        sender.reset_mock()
        with patch.object(help,'now',return_value=dt):self.assertEqual(help.notify(sender,True)['accepted'],0)
        sender.assert_not_called();self.assertEqual(help.report()['requests'][0]['notification_status'],'uncertain')
    def test_admin_access_csrf_status_and_escaped_content(self):
        data=self.form();self.client.post('/setup-help',data={**data,'name':'<script>alert(1)</script>','note':'<img src=x onerror=alert(1)>'})
        self.assertEqual(self.client.get('/admin/setup-help',follow_redirects=False).headers['location'],'/login')
        pid=store.create_provider('Admin sample',secrets.token_hex(6)+'@example.invalid','password123');session=store.create_session(pid)
        self.client.cookies.set(app.SESSION_COOKIE,session)
        self.assertEqual(self.client.get('/admin/setup-help').status_code,404)
        row=help.report()['requests'][0]
        with patch.object(app,'is_admin',return_value=True):
            r=self.client.get('/admin/setup-help');self.assertEqual(r.status_code,200);self.assertEqual(r.headers['x-robots-tag'],'noindex')
            self.assertIn('&lt;script&gt;',r.text);self.assertNotIn('<img src=x',r.text);self.assertIn('How to help a provider',r.text)
            payload={'request_id':row['id'],'status':'contacted','admin_note':'Reply sent; waiting for billing details.'}
            self.assertEqual(self.client.post('/admin/setup-help/update',data=payload).status_code,403)
            payload['csrf_token']=app.csrf_token_for_session(session)
            self.assertEqual(self.client.post('/admin/setup-help/update',data={**payload,'status':'invalid'}).status_code,400)
            self.assertEqual(self.client.post('/admin/setup-help/update',data=payload,follow_redirects=False).status_code,303)
            self.assertEqual(help.report()['requests'][0]['status'],'contacted')
            self.assertEqual(self.client.post('/admin/setup-help/update',data={**payload,'request_id':999999}).status_code,404)
            self.assertEqual(self.client.post('/admin/setup-help/notifications',data={'csrf_token':payload['csrf_token']},follow_redirects=False).status_code,303)
        self.assertEqual(self.sender.call_count,1)
    def test_session_and_expired_accounts_can_request_help_without_account_changes(self):
        pid=store.create_provider('Sample',secrets.token_hex(6)+'@example.invalid','password123');store.set_subscription(pid,'micro','expired')
        session=store.create_session(pid);self.client.cookies.set(app.SESSION_COOKIE,session)
        data=self.form();self.assertEqual(data['csrf_token'],app.csrf_token_for_session(session))
        self.assertEqual(self.client.post('/setup-help',data=data).status_code,200)
        self.assertEqual(store.get_subscription(pid)['status'],'expired')
    def test_privacy_disclosures_are_visible_and_help_event_respects_opt_out(self):
        r=self.client.get('/privacy');title=re.search('<title>(.*?)</title>',r.text,re.S).group(1)
        self.assertNotIn('<section',title);self.assertIn('<strong>Setup-help requests:</strong>',r.text);self.assertIn('Free documents and partner links',r.text)
        self.client.headers['dnt']='1';data=self.form();self.client.post('/setup-help',data=data)
        self.assertEqual(self.count(),1)
        with store.db() as conn:
            vid=growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM growth_events WHERE visitor_id=? AND event='setup_help_requested'",(vid,)).fetchone()['n'],0)

    def test_hourly_scheduler_retries_saved_notifications_with_cron_auth(self):
        data=self.form()
        with patch.object(app,'EMAIL_ACTIVE',False):self.client.post('/setup-help',data=data)
        self.assertEqual(self.client.get('/internal/run-reminders').status_code,401)
        with patch.object(app,'INTERNAL_CRON_TOKEN','setup-test-cron'),patch.object(app,'run_reminders',return_value=[]),patch.object(app.email_engagement,'run',return_value={'accepted':0}):
            response=self.client.get('/internal/run-reminders?token=setup-test-cron')
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['setup_help']['accepted'],1)
        self.sender.assert_called_once();self.assertEqual(self.sender.call_args.args[0],'rob@tuitionping.com')

    def test_older_requests_remain_accessible_after_first_page(self):
        data=self.form()
        for _ in range(101):help.create({**data,'request_key':secrets.token_hex(16)})
        first=help.report();older=help.report(first['next'])
        self.assertEqual(len(first['requests']),100);self.assertEqual(len(older['requests']),1)
        self.assertIsNone(older['next']);self.assertEqual(older['previous'],0)
        self.assertNotIn(older['requests'][0]['id'],{r['id'] for r in first['requests']})
        self.assertEqual(help.report(-100)['offset'],0);self.sender.assert_not_called()
