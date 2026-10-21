"""Public discovery, metadata and privacy-safe event boundaries for both pages."""
import json
import re
import unittest
from xml.etree import ElementTree as ET
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import growth

WATCH = '/how-it-works'
NOTICE = '/tools/daycare-past-due-letter'

class VideoNoticeTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app, base_url='https://www.tuitionping.com', headers={'user-agent':'Mozilla/5.0 Chrome/131 Safari/537.36'})
        self.addCleanup(self.client.close)

    def event(self, event, path, detail=''):
        token = re.search(r'name="tp-analytics-token" content="([a-f0-9]+)"', self.client.get(path).text).group(1)
        return self.client.post('/analytics/event',json={'event':event,'path':path,'detail':detail},headers={'x-tp-analytics':token})

    def test_both_pages_have_canonical_single_h1_unique_ids_and_discovery_links(self):
        sitemap = ET.fromstring(self.client.get('/sitemap.xml').text)
        urls = {el.text for el in sitemap.iter() if el.tag.endswith('}loc')}
        for path in (WATCH, NOTICE):
            page = self.client.get(path+'?utm_source=example')
            self.assertEqual(page.status_code,200)
            self.assertEqual(len(re.findall(r'<h1[ >]',page.text)),1)
            ids = re.findall(r'\bid="([^"]+)"',page.text)
            self.assertEqual(len(ids),len(set(ids)))
            self.assertIn('href="https://www.tuitionping.com'+path+'"',page.text)
            self.assertNotIn('noindex',page.headers.get('x-robots-tag',''))
            self.assertIn('https://www.tuitionping.com'+path,urls)
            for linking in ('/','/guides'):
                self.assertIn('href="'+path+'"',self.client.get(linking).text)

    def test_watch_metadata_matches_accessible_player_and_transcript(self):
        page = self.client.get(WATCH).text
        schema = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>',page,re.S).group(1))
        self.assertEqual(schema['@type'],'VideoObject')
        self.assertEqual(schema['duration'],'PT48S')
        self.assertRegex(schema['uploadDate'],r'^2026-10-20T.*-07:00$')
        self.assertIn(schema['contentUrl'].replace('https://www.tuitionping.com',''),page)
        self.assertIn(schema['thumbnailUrl'][0].replace('https://www.tuitionping.com',''),page)
        for text in ('Video transcript','0:43','Confirm payment received','fictional data','controls playsinline','kind="captions"','preload="none"'):
            self.assertIn(text,page)
        self.assertNotIn('autoplay',page)

    def test_watch_and_notice_events_are_deduplicated_and_path_bound(self):
        for event,path in (('video_started',WATCH),('video_completed',WATCH),('notice_created',NOTICE)):
            for _ in range(2):self.assertEqual(self.event(event,path).status_code,204)
            self.assertEqual(self.event(event,'/pricing').status_code,400)
            self.assertEqual(self.event(event,path,'private-family-data').status_code,400)
        vid = growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        with store.db() as conn:
            rows = conn.execute("SELECT event FROM growth_events WHERE visitor_id=? AND event IN ('video_started','video_completed','notice_created')",(vid,)).fetchall()
        self.assertEqual(len(rows),3)
        self.assertEqual(self.event('paid_customer',NOTICE).status_code,400)
        self.assertEqual(self.event('resource_next_step',NOTICE,'notice').status_code,204)
        self.assertEqual(self.event('resource_demo_click',WATCH,'notice').status_code,400)

    def test_letter_use_is_a_resource_not_a_paid_conversion_and_bots_are_auditable(self):
        self.assertEqual(self.event('notice_created',NOTICE).status_code,204)
        vid = growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        human = growth.report()
        row = next(r for r in human['pages'] if r['key']==(NOTICE,))
        self.assertEqual(row['resources'],1)
        self.assertEqual(row['paid'],0)
        with store.db() as conn:conn.execute("UPDATE growth_visitors SET classification='KNOWN BOT' WHERE visitor_id=?",(vid,))
        human = growth.report()
        audit = growth.report(kind='all')
        self.assertFalse(any(r['key']==(NOTICE,) for r in human['pages']))
        self.assertTrue(any(r['key']==(NOTICE,) and r['resources']>=1 for r in audit['pages']))

    def test_optouts_keep_both_pages_usable_without_event_collection(self):
        for header in ('dnt','sec-gpc'):
            with TestClient(app.app,base_url='https://www.tuitionping.com',headers={header:'1','user-agent':'Mozilla/5.0 Safari/605.1'}) as client:
                for path in (WATCH,NOTICE):
                    response = client.get(path)
                    self.assertEqual(response.status_code,200)
                    self.assertNotIn('tp-analytics-token',response.text)
                    self.assertNotIn(growth.COOKIE,response.cookies)

    def test_notice_assets_and_only_letter_print_styles(self):
        for path in ('/static/past-due.css','/static/past-due.js'):
            self.assertEqual(self.client.get(path).status_code,200)
        css = self.client.get('/static/past-due.css').text
        self.assertIn('@media print',css)
        self.assertIn('.resource-next-step',css)
        self.assertIn('@page{size:letter',css)
