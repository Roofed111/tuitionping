"""Strict cohorts, first-touch sources, keyword imports and no invented activity."""
import json
import re
import secrets
import unittest
from datetime import timedelta
from unittest.mock import patch
import test_traffic as fixture
import growth
import traffic
import search_reporting

class EngagementSourcesTest(unittest.TestCase):
    hit = fixture.TrafficTest.hit
    verify = fixture.TrafficTest.verify
    row = fixture.TrafficTest.row

    def setUp(self):
        fixture.TrafficTest.setUp(self)
        search_reporting.ensure_table()
        with fixture.store.db() as conn:
            conn.execute('DELETE FROM search_query_imports')

    def engage(self,vid,signal='interaction',at=9):
        with patch.object(traffic,'now',return_value=self.base+timedelta(seconds=at)):
            return traffic.confirm_engagement(vid,'/demo',signal)

    def test_browser_execution_alone_no_longer_enters_primary_count(self):
        for ua in (fixture.CHROME,fixture.SAFARI,fixture.IPHONE):
            vid=self.hit(ua=ua); self.verify(vid)
            self.assertEqual(self.row(vid)['classification'],'HUMAN')
            self.assertFalse(traffic.eligible('engaged',self.row(vid)))
            self.assertTrue(self.engage(vid))
            self.assertTrue(traffic.eligible('engaged',self.row(vid)))
        r=traffic.report('engaged')
        self.assertEqual((r['engaged_total'],r['filtered_total'],r['all_total']),(3,3,3))
        self.assertEqual(r['hits_total'],3)

    def test_timing_render_path_and_negative_evidence_are_required(self):
        vid=self.hit()
        self.assertFalse(self.engage(vid))
        self.verify(vid)
        self.assertFalse(self.engage(vid,at=7.99))
        self.assertFalse(self.engage(vid,signal='active_reading',at=29.99))
        self.assertTrue(self.engage(vid,signal='active_reading',at=30))
        for _ in range(3):self.assertTrue(self.engage(vid,at=31))
        self.assertEqual((self.row(vid)['hit_count'],self.row(vid)['page_count']),(1,1))
        self.hit(vid,path='/wp-login.php',status_code=404,identity_kind='browser',at=32)
        self.assertEqual(self.row(vid)['classification'],'LIKELY BOT')
        self.assertFalse(self.engage(vid,at=40))
        self.assertEqual(traffic.report('engaged')['filtered_total'],0)
        self.assertEqual(traffic.report('automated')['filtered_total'],1)

    def test_unconfirmed_and_quiet_browser_visits_are_preserved_not_called_bots(self):
        quiet=self.hit(); self.verify(quiet)
        unconfirmed=self.hit(at=1)
        self.assertEqual(traffic.report('browser')['filtered_total'],1)
        self.assertEqual(traffic.report('unconfirmed')['filtered_total'],1)
        self.assertEqual(traffic.report('human')['filtered_total'],2)
        self.assertEqual(traffic.report('engaged')['filtered_total'],0)
        self.assertTrue(all(self.row(v)['classification'] in traffic.HUMAN_TYPES for v in (quiet,unconfirmed)))

    def test_strict_conversion_rate_and_broad_audit_share_legitimate_milestones(self):
        vids=[]
        for _ in range(5):
            vid=self.hit();growth.record(vid,'page_view',path='/demo');vids.append(vid)
        self.verify(vids[0]);self.assertTrue(self.engage(vids[0]))
        for _ in range(2):
            pid=fixture.store.create_provider('Sample',secrets.token_hex(8)+'@example.invalid','password123')
            growth.bind_account(vids[1],pid)
        r=growth.report(kind='engaged')
        self.assertEqual((r['rate_denominator'],r['converting_visitors'],r['conversion_rate']),(2,1,50.0))
        self.assertEqual(next(s['count'] for s in r['stages'] if s['event']=='signup'),2)
        self.assertEqual(growth.report(kind='human')['conversion_rate'],20.0)
        self.assertEqual((r['source_quality'][0]['visitors'],r['source_quality'][0]['engaged'],r['source_quality'][0]['unconfirmed']),(5,2,3))

    def test_real_prior_account_evidence_can_qualify_but_bot_accounts_cannot(self):
        human=self.hit();bot=self.hit(ua='curl/8')
        for vid in (human,bot):
            pid=fixture.store.create_provider('Sample',secrets.token_hex(8)+'@example.invalid','password123')
            growth.bind_account(vid,pid)
        with fixture.store.db() as conn:
            conn.execute('UPDATE growth_visitors SET engagement_confirmed=0')
        traffic.refresh_account_engagement()
        self.assertTrue(traffic.eligible('engaged',self.row(human)))
        self.assertFalse(traffic.eligible('engaged',self.row(bot)))

    def source(self,path='/demo',ref=''):
        self.client.cookies.clear()
        self.client.get(path,headers={'referer':ref})
        vid=growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        return vid,json.loads(self.row(vid)['attribution_json'])

    def test_search_social_and_ad_sources_are_precise_and_private(self):
        cases=[('https://www.google.com/search?q=secret+keyword','/demo','google','organic'),
               ('https://www.google.co.uk/','/demo','google','organic'),
               ('https://www.bing.com/search?q=secret','/demo','bing','organic'),
               ('https://mail.google.com/mail/u/0/#inbox','/demo','mail.google.com','referral'),
               ('https://www.google.com/maps','/demo','www.google.com','referral'),
               ('https://www.youtube.com/watch?v=secret','/demo','www.youtube.com','social'),
               ('https://www.google.com.evil.invalid/search','/demo','www.google.com.evil.invalid','referral'),
               ('','/demo?gclid=private_click_id','google','cpc')]
        for ref,path,source,medium in cases:
            with self.subTest(ref=ref,path=path):
                vid,detail=self.source(path,ref)
                self.assertEqual((self.row(vid)['source'],self.row(vid)['medium']),(source,medium))
                self.assertNotIn('secret',json.dumps(detail));self.assertNotIn('private_click_id',json.dumps(detail))
                self.assertEqual(detail['term'],'')

    def test_campaign_keyword_is_labeled_and_first_touch_never_overwritten(self):
        vid,detail=self.source('/demo?utm_source=google&utm_medium=cpc&utm_campaign=fall&utm_content=video-one&utm_term=daycare%20tuition&email=private@example.invalid')
        self.assertEqual((detail['term'],detail['content']),('daycare tuition','video-one'))
        self.client.get('/guides?utm_source=other&utm_term=other')
        self.assertEqual(json.loads(self.row(vid)['attribution_json']),detail)
        self.assertNotIn('private',str(self.row(vid)))
        _,detail=self.source('/demo?utm_source=test&utm_term=private@example.invalid&utm_content=123456789')
        self.assertEqual((detail['term'],detail['content']),('',''))

    def test_signed_engagement_endpoint_rejects_forgery_early_and_hidden_path(self):
        response=self.client.get('/demo')
        token=re.search('name="tp-analytics-token" content="([a-f0-9]+)"',response.text).group(1)
        vid=growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        event={'event':'visitor_engaged','detail':'interaction','path':'/demo'}
        self.assertEqual(self.client.post('/analytics/event',json=event,headers={'x-tp-analytics':'forged'}).status_code,403)
        self.assertEqual(self.client.post('/analytics/event',json=event,headers={'x-tp-analytics':token}).status_code,400)
        self.client.post('/analytics/event',json={'event':'browser_verified','path':'/demo'},headers={'x-tp-analytics':token})
        with patch.object(traffic,'now',return_value=self.base+timedelta(seconds=15)):
            self.assertEqual(self.client.post('/analytics/event',json=event,headers={'x-tp-analytics':token}).status_code,204)
        self.assertEqual(self.row(vid)['hit_count'],1)
        self.assertEqual(self.client.post('/analytics/event',json={**event,'path':'/admin'},headers={'x-tp-analytics':token}).status_code,400)

    def test_keywords_import_calculates_rates_and_retains_snapshots(self):
        raw=b'Top queries,Clicks,Impressions,CTR,Position\ndaycare tuition,2,20,10%,3.5\ntuition reminder,1,50,2%,9\n'
        search_reporting.save(raw,'2026-09-01','2026-09-30',1)
        report=search_reporting.report()
        self.assertEqual((report['clicks'],report['impressions']),(3,70))
        self.assertEqual(report['queries'][0]['ctr'],10.0)
        search_reporting.save(raw,'2026-09-01','2026-09-30',1)
        with fixture.store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM search_query_imports').fetchone()['n'],2)

    def test_invalid_keyword_exports_never_replace_existing_report(self):
        for raw in (b'Wrong,Columns\na,b\n',b'Top queries,Clicks,Impressions,Position\na,-1,10,3\n',b'Top queries,Clicks,Impressions,Position\na,1,10,nan\n',b'Top queries,Clicks,Impressions,Position\nprivate@example.invalid,1,10,3\n',b'x'*524289):
            with self.assertRaises(ValueError):search_reporting.save(raw,'2026-09-01','2026-09-30',1)
        self.assertEqual(search_reporting.report()['queries'],[])

    def test_admin_default_is_strict_keyword_upload_is_csrf_protected(self):
        self.client.cookies.set(fixture.app.SESSION_COOKIE,'test-session')
        admin={'id':1,'name':'Admin','is_admin':True}
        with patch.object(fixture.app,'require_admin',return_value=(admin,None)):
            r=self.client.get('/admin/visitors')
            self.assertEqual(r.context['report']['kind'],'engaged')
            self.assertIn('Engaged Visitors Today',r.text)
            r=self.client.get('/admin/conversions')
            self.assertEqual(r.context['report']['kind'],'engaged')
            self.assertIn('Google returned no reportable keyword rows',r.text)
            raw=b'Top queries,Clicks,Impressions,Position\ndaycare tuition,2,20,3.5\n'
            data={'start_date':'2026-09-01','end_date':'2026-09-30'}
            self.assertEqual(self.client.post('/admin/conversions/search-keywords',data=data,files={'file':('Queries.csv',raw)}).status_code,403)
            data['csrf_token']=fixture.app.csrf_token_for_session('test-session')
            r=self.client.post('/admin/conversions/search-keywords',data=data,files={'file':('Queries.csv',raw)},follow_redirects=False)
            self.assertEqual(r.status_code,303)
            self.assertIn('daycare tuition',self.client.get('/admin/conversions').text)
