"""Wizard ownership/sending guards and real partner-to-customer attribution.
All providers are temporary and all delivery is mocked.
"""
import io
import re
import secrets
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import growth
import setup_wizard
import partner_resources as partners

class SetupWizardTest(unittest.TestCase):
    def setUp(self):
        self.pid=store.create_provider('Sample owner',secrets.token_hex(6)+'@example.invalid','password123')
        store.set_subscription(self.pid,'micro','trialing')
        store.set_email_verified(self.pid,True)
        self.session=store.create_session(self.pid)
        self.client=TestClient(app.app)
        self.client.cookies.set(app.SESSION_COOKIE,self.session)
        self.addCleanup(self.client.close)
    def post(self,path,data=None,**kw):
        return self.client.post(path,data={**(data or {}),'csrf_token':app.csrf_token_for_session(self.session)},follow_redirects=False,**kw)
    def program(self):
        return self.post('/setup/program',dict(name='Sample daycare',timezone='America/Los_Angeles',quiet_start='07:00',quiet_end='21:00',payment_url='https://pay.example.invalid/pay'))
    def family(self):
        loc=store.list_locations(self.pid)[0];room=store.list_classrooms(loc['id'])[0]
        with patch.object(app,'send_welcome_text') as welcome:
            r=self.post('/families/add',dict(classroom_id=room['id'],first_name='Sample child',phone='+12025550123',tuition_amount='800',due_day='5',consent='on',return_to='setup'))
            welcome.assert_called_once()
        return r
    def acknowledge(self):
        state=setup_wizard.snapshot(store.get_provider(self.pid))
        return self.post('/setup/preview',dict(digest=state['digest'],reviewed='on'))
    def test_four_steps_persist_and_test_text_matches_preview(self):
        with patch('sms.send_sms') as send:
            self.assertIn('1. Program details',self.client.get('/setup?step=test').text)
            self.assertEqual(self.program().headers['location'],'/setup?step=families')
            self.assertEqual(self.program().status_code,303)
            self.assertEqual(len(store.list_locations(self.pid)),1)
            self.assertEqual(len(store.list_classrooms(store.list_locations(self.pid)[0]['id'])),1)
            self.assertEqual(self.family().headers['location'],'/setup?step=families')
            self.assertIn('Import families from CSV',self.client.get('/setup?step=families').text)
            self.assertIn('3. Preview',self.client.get('/setup?step=test').text)
            self.assertEqual(self.acknowledge().status_code,303)
            preview=app.test_text_preview(self.pid)
            self.assertIn('[Test]',self.client.get('/setup?step=test').text)
            send.assert_not_called()
            r=self.post('/test-text',dict(phone='+12025550123',return_to='setup',own_number='on'))
            self.assertEqual(r.headers['location'],'/setup?step=test&test=sent')
            send.assert_called_once_with('+12025550123',preview,self.pid,None)
        self.assertTrue(setup_wizard.snapshot(store.get_provider(self.pid))['complete'])
        self.assertNotIn('Continue setup',self.client.get('/dashboard').text)
    def test_auth_csrf_subscription_ownership_and_invalid_settings(self):
        self.assertEqual(self.client.post('/setup/program',data={'name':'No token'}).status_code,403)
        invalid=self.post('/setup/program',dict(name='Test',timezone='UTC',quiet_start='07:00',quiet_end='07:00'))
        self.assertEqual(invalid.status_code,400)
        for url in ['javascript:alert(1)','https://user:secret@example.com','https://[broken']:
            self.assertEqual(self.post('/setup/program',dict(name='Test',timezone='UTC',quiet_start='07:00',quiet_end='21:00',payment_url=url)).status_code,400)
        other=store.create_provider('Other',secrets.token_hex(6)+'@example.invalid','password123')
        loc=store.create_location(other,'Private other')
        self.assertEqual(self.post('/setup/program',dict(location_id=loc,name='Test',timezone='UTC',quiet_start='07:00',quiet_end='21:00')).status_code,404)
        self.assertEqual(store.get_location(loc,other)['name'],'Private other')
        store.set_subscription(self.pid,'micro','expired')
        self.assertEqual(self.client.get('/setup',follow_redirects=False).headers['location'],'/billing?checkout=required')
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/setup',follow_redirects=False).headers['location'],'/login')
    def test_preview_and_test_invalidate_when_settings_change(self):
        self.program();self.family();self.acknowledge()
        with patch('sms.send_sms'):
            self.post('/test-text',dict(phone='+12025550123',return_to='setup',own_number='on'))
        self.assertTrue(setup_wizard.snapshot(store.get_provider(self.pid))['complete'])
        store.set_location_payment_url(store.list_locations(self.pid)[0]['id'],self.pid,'https://pay.example.invalid/new')
        self.assertFalse(setup_wizard.snapshot(store.get_provider(self.pid))['complete'])
        with patch('sms.send_sms') as send:
            self.assertEqual(self.post('/test-text',dict(phone='+12025550123',return_to='setup',own_number='on')).headers['location'],'/setup?step=preview')
            send.assert_not_called()
    def test_stale_preview_checkbox_and_failed_test_do_not_complete(self):
        self.program();self.family()
        self.assertEqual(self.post('/setup/preview',dict(digest='stale',reviewed='on')).status_code,409)
        state=setup_wizard.snapshot(store.get_provider(self.pid))
        self.assertEqual(self.post('/setup/preview',dict(digest=state['digest'])).status_code,409)
        self.acknowledge()
        with patch('sms.send_sms',side_effect=RuntimeError('provider failure')):
            self.assertEqual(self.post('/test-text',dict(phone='+12025550123',return_to='setup',own_number='on')).status_code,502)
        self.assertFalse(setup_wizard.snapshot(store.get_provider(self.pid))['complete'])
        self.assertFalse(store.has_ever_sent_test_text(self.pid))
        self.assertFalse(setup_wizard.snapshot(store.get_provider(self.pid))['complete'])
        self.client.get('/setup?step=test&test=sent')
        self.assertFalse(setup_wizard.snapshot(store.get_provider(self.pid))['complete'])
    def test_test_limits_and_explicit_own_number(self):
        self.program();self.family();self.acknowledge()
        with patch('sms.send_sms') as send:
            self.assertEqual(self.post('/test-text',dict(phone='+12025550123',return_to='setup')).status_code,400)
            for _ in range(3):self.assertEqual(self.post('/test-text',dict(phone='+12025550123',return_to='setup',own_number='on')).status_code,303)
            self.assertEqual(self.post('/test-text',dict(phone='+12025550123',return_to='setup',own_number='on')).status_code,400)
            self.assertEqual(send.call_count,3)
    def test_csv_returns_to_wizard_and_retains_row_errors(self):
        self.program();room=store.list_classrooms(store.list_locations(self.pid)[0]['id'])[0]
        data=b'name,phone,tuition,due_day,language\nSample A,+12025550123,500,5,es\nSample B,invalid,500,5,en\n'
        with patch.object(app,'send_welcome_text') as welcome:
            r=self.post('/families/import',dict(classroom_id=room['id'],consent='on',return_to='setup'),files={'file':('sample.csv',data,'text/csv')})
            self.assertEqual(r.status_code,200);welcome.assert_called_once()
        self.assertIn('Back to setup',r.text);self.assertIn('1</strong> family',r.text);self.assertIn('Row',r.text)
        self.assertEqual(store.list_families(room['id'])[0]['language'],'es')
    def test_unverified_cannot_add_family_and_setup_pages_private(self):
        self.program();store.set_email_verified(self.pid,False)
        with patch.object(app,'EMAIL_ACTIVE',True),patch.object(app,'send_welcome_text') as send:
            room=store.list_classrooms(store.list_locations(self.pid)[0]['id'])[0]
            r=self.post('/families/add',dict(classroom_id=room['id'],first_name='Test',phone='+12025550123',tuition_amount='800',due_day='5',consent='on',return_to='setup'))
            self.assertEqual(r.status_code,403);send.assert_not_called()
            self.assertIn('Verify your email',self.client.get('/setup?step=families').text)
        response=self.client.get('/setup');self.assertEqual(response.headers['x-robots-tag'],'noindex');self.assertIn('no-store',response.headers['cache-control'])
    def test_existing_multiple_locations_load_selected_details_without_deletion(self):
        store.set_company(self.pid,'Existing program')
        first=store.create_location(self.pid,'First','One','https://first.example.invalid')
        second=store.create_location(self.pid,'Second','Two','https://second.example.invalid')
        store.create_classroom(first,'Original classroom')
        r=self.client.get('/setup?step=program&location_id='+str(second))
        self.assertIn('value="Second"',r.text);self.assertIn('value="https://second.example.invalid"',r.text)
        self.assertEqual(self.post('/setup/program',dict(location_id=second,name='Second renamed',timezone='UTC',quiet_start='07:00',quiet_end='21:00')).status_code,303)
        self.assertEqual(store.get_location(first,self.pid)['name'],'First')
        self.assertEqual(store.list_classrooms(first)[0]['label'],'Original classroom')

class PartnerResourcesTest(unittest.TestCase):
    def setUp(self):
        growth.ensure_tables()
        with store.db() as conn:
            for table in ['growth_partner_accounts','growth_partner_visitors','growth_partners','growth_events','growth_accounts','growth_visitors']:conn.execute('DELETE FROM '+table)
            for code in ['group-one','group-two']:conn.execute('INSERT INTO growth_partners (code,name,kind,created_at) VALUES (?,?,?,?)',(code,code,'provider_group',store.now_iso()))
        self.client=TestClient(app.app,base_url='https://www.tuitionping.com',headers={'user-agent':'Mozilla/5.0 Test browser'})
        self.addCleanup(self.client.close)
    def test_public_resources_metadata_and_expired_accounts(self):
        for path in ['/tools/daycare-invoice-receipt','/guides/tuition-collection','/partners']:
            r=self.client.get(path);self.assertEqual(r.status_code,200);self.assertEqual(len(re.findall('<h1[ >]',r.text)),1)
            self.assertIn('https://www.tuitionping.com'+path,r.text);self.assertIn(path,self.client.get('/sitemap.xml').text)
            self.assertIn('/demo',r.text)
        provider={'id':999,'name':'Sample','email':'sample@example.invalid','suspended':False,'is_admin':False}
        self.client.cookies.set(app.SESSION_COOKIE,'sample')
        with patch.object(store,'get_provider_by_session',return_value=provider),patch.object(store,'get_subscription',return_value={'status':'expired'}):
            self.assertEqual(self.client.get('/tools/daycare-invoice-receipt',follow_redirects=False).status_code,200)
            self.assertEqual(self.client.get('/partners/download',follow_redirects=False).status_code,200)
    def test_personalized_package_is_valid_and_links_have_assigned_code(self):
        r=self.client.get('/partners?partner=group-one');self.assertEqual(r.status_code,200)
        self.assertIn('group-one',r.text)
        package=zipfile.ZipFile(io.BytesIO(self.client.get('/partners/download?partner=group-one').content))
        self.assertIsNone(package.testzip());self.assertEqual(len(package.namelist()),9)
        for name in ['Start-here.html','Ready-to-share-copy.txt','READ-ME.txt']:
            self.assertIn('partner=group-one',package.read(name).decode())
        self.assertIn('group-one',partners.report()[0]['code']+partners.report()[1]['code'])
        row=next(r for r in partners.report() if r['code']=='group-one')
        self.assertEqual(row['visitors'],1);self.assertEqual(row['downloads'],1)
    def test_partner_credit_survives_existing_direct_first_touch_and_locks_at_signup(self):
        self.client.get('/guides')
        self.client.get('/partners?partner=group-one')
        self.client.get('/partners?partner=group-two')
        visitor=growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        pid=store.create_provider('Sample',secrets.token_hex(6)+'@example.invalid','password123')
        growth.bind_account(visitor,pid)
        growth.milestone(pid,'trial_started');growth.milestone(pid,'paid_customer')
        self.client.get('/partners?partner=group-one');growth.bind_account(visitor,pid)
        rows={p['code']:p for p in partners.report()}
        self.assertEqual(rows['group-two']['signups'],1);self.assertEqual(rows['group-two']['trials'],1);self.assertEqual(rows['group-two']['paid'],1)
        self.assertEqual(rows['group-one']['paid'],0)
        self.assertEqual(growth.report()['sources'][0]['key'],('direct','none',''))
    def test_unknown_code_and_opt_outs_do_not_receive_partner_credit(self):
        for headers in [{'dnt':'1'},{'sec-gpc':'1'},{'user-agent':'ExampleBot'}]:
            with TestClient(app.app,headers={'user-agent':'Mozilla/5.0',**headers}) as c:c.get('/partners?partner=group-one')
        self.client.get('/partners?partner=unknown-code')
        self.assertTrue(all(p['visitors']==0 for p in partners.report()))
        self.assertNotIn('unknown-code',self.client.get('/partners?partner=unknown-code').text)
    def test_admin_only_and_csrf_validation(self):
        self.assertEqual(self.client.get('/admin/partners',follow_redirects=False).headers['location'],'/login')
        pid=store.create_provider('Admin sample',secrets.token_hex(6)+'@example.invalid','password123')
        session=store.create_session(pid);self.client.cookies.set(app.SESSION_COOKIE,session)
        self.assertEqual(self.client.get('/admin/partners').status_code,404)
        with patch.object(app,'is_admin',return_value=True):
            r=self.client.get('/admin/partners');self.assertEqual(r.status_code,200);self.assertEqual(r.headers['x-robots-tag'],'noindex')
            self.assertEqual(self.client.post('/admin/partners',data={'name':'Sample','code':'new-group','kind':'association'}).status_code,403)
            data={'name':'Sample association','code':'new-group','kind':'association','csrf_token':app.csrf_token_for_session(session)}
            self.assertEqual(self.client.post('/admin/partners',data=data,follow_redirects=False).status_code,303)
            self.assertEqual(partners.get_partner('new-group')['name'],'Sample association')
            self.assertIn('duplicate',self.client.post('/admin/partners',data=data,follow_redirects=False).headers['location'])
            self.assertIn('invalid',self.client.post('/admin/partners',data={**data,'code':'private@example.com'},follow_redirects=False).headers['location'])
    def test_partner_window_retention_and_document_events_without_form_values(self):
        self.client.get('/partners?partner=group-one')
        vid=growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        old=(datetime.now(timezone.utc)-timedelta(days=31)).isoformat(timespec='seconds')
        with store.db() as conn:conn.execute('UPDATE growth_partner_visitors SET touched_at=? WHERE visitor_id=?',(old,vid))
        pid=store.create_provider('Sample',secrets.token_hex(6)+'@example.invalid','password123');growth.bind_account(vid,pid)
        self.assertTrue(all(p['signups']==0 for p in partners.report()))
        response=self.client.get('/tools/daycare-invoice-receipt')
        token=re.search('name="tp-analytics-token" content="([a-f0-9]+)"',response.text).group(1)
        event={'event':'document_created','detail':'receipt','path':'/tools/daycare-invoice-receipt'}
        self.assertEqual(self.client.post('/analytics/event',json=event,headers={'x-tp-analytics':token}).status_code,204)
        self.assertEqual(self.client.post('/analytics/event',json={**event,'detail':'secret family $500'},headers={'x-tp-analytics':token}).status_code,400)
        self.assertEqual(self.client.post('/analytics/event',json={**event,'path':'/partners'},headers={'x-tp-analytics':token}).status_code,400)
        old=(datetime.now(timezone.utc)-timedelta(days=91)).isoformat(timespec='seconds')
        with store.db() as conn:conn.execute('UPDATE growth_visitors SET first_seen=? WHERE visitor_id=?',(old,vid))
        growth.register(secrets.token_hex(16),'direct','none','','/guides')
        with store.db() as conn:self.assertEqual(conn.execute('SELECT COUNT(*) AS c FROM growth_partner_visitors WHERE visitor_id=?',(vid,)).fetchone()['c'],0)
