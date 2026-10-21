"""Framing isolation, iframe analytics boundaries and partner-to-account attribution."""
import json
import re
import secrets
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import billing
import growth
import partner_resources
import public_growth_tools as public

class EmbedFinderTest(unittest.TestCase):
    def setUp(self):
        growth.ensure_tables()
        self.client=TestClient(app.app,base_url='https://www.tuitionping.com',headers={'user-agent':'Mozilla/5.0 Chrome/131 Safari/537.36'})
        self.addCleanup(self.client.close)
        self.code='embed-'+secrets.token_hex(5)
        with store.db() as conn:
            conn.execute('INSERT INTO growth_partners (code,name,kind,created_at) VALUES (?,?,?,?)',(self.code,'Fixture association','association',store.now_iso()))

    def event(self,event,path,detail=''):
        token=re.search(r'name="tp-analytics-token" content="([a-f0-9]+)"',self.client.get(path).text).group(1)
        return self.client.post('/analytics/event',headers={'x-tp-analytics':token},json={'event':event,'path':path,'detail':detail})

    def test_only_standalone_widget_is_frameable_and_it_has_no_account_or_growth_scripts(self):
        page=self.client.get(public.EMBED_PATH)
        self.assertEqual(page.status_code,200)
        self.assertNotIn('x-frame-options',page.headers)
        csp=page.headers['content-security-policy']
        self.assertIn('frame-ancestors https:',csp)
        self.assertIn('form-action \'none\'',csp)
        self.assertIn('sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox',csp)
        self.assertIn('default-src \'none\'',csp)
        self.assertEqual(page.headers['x-robots-tag'],'noindex')
        self.assertNotIn('tp-analytics-token',page.text)
        self.assertNotIn('/static/growth.js',page.text)
        self.assertNotIn('/signup',page.text)
        for path in ('/','/login','/admin','/setup',public.EMBED_GUIDE,public.FINDER_PATH,public.EMBED_PATH+'/private'):
            self.assertEqual(self.client.get(path,follow_redirects=False).headers['x-frame-options'],'DENY',path)

    def test_embedded_loads_set_no_cookie_and_create_no_analytics_visitors_or_partner_touches(self):
        with store.db() as conn:
            before={table:conn.execute('SELECT COUNT(*) n FROM '+table).fetchone()['n'] for table in ('growth_visitors','growth_events','growth_partner_visitors')}
        with patch.object(store,'log_site_visit') as log:
            for _ in range(8):
                page=self.client.get(public.EMBED_PATH+'?partner='+self.code)
                self.assertEqual(page.status_code,200)
                self.assertNotIn('set-cookie',page.headers)
            log.assert_not_called()
        with store.db() as conn:
            for table,count in before.items():self.assertEqual(conn.execute('SELECT COUNT(*) n FROM '+table).fetchone()['n'],count)

    def test_clickthrough_preserves_partner_and_first_touch_for_later_account(self):
        self.client.get(public.EMBED_PATH+'?partner='+self.code)
        self.assertEqual(self.client.get(partner_resources.link(self.code,public.FINDER_PATH)).status_code,200)
        vid=growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        self.assertEqual(self.event('solution_recommended',public.FINDER_PATH,'reminder_subscription').status_code,204)
        pid=store.create_provider('Fixture',secrets.token_hex(8)+'@example.invalid','password123')
        growth.bind_account(vid,pid)
        partner_resources.bind_account(vid,pid)
        with store.db() as conn:
            first=conn.execute('SELECT * FROM growth_visitors WHERE visitor_id=?',(vid,)).fetchone()
            touch=conn.execute('SELECT * FROM growth_partner_accounts WHERE provider_id=?',(pid,)).fetchone()
        self.assertEqual(first['landing_path'],public.FINDER_PATH)
        self.assertEqual(first['source'],self.code)
        self.assertEqual(touch['partner_code'],self.code)
        self.assertEqual(self.event('paid_customer',public.FINDER_PATH).status_code,400)

    def test_partner_builder_validates_codes_and_never_echoes_hostile_input(self):
        page=self.client.get(public.EMBED_GUIDE+'?partner='+self.code)
        self.assertIn('Fixture association',page.text)
        self.assertIn('partner='+self.code,page.text)
        self.assertIn('sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"',page.text)
        bad=self.client.get(public.EMBED_GUIDE,params={'partner':'\"><script>alert(1)</script>'})
        self.assertIn('That partner code was not found',bad.text)
        self.assertNotIn('alert(1)',bad.text)
        widget=self.client.get(public.EMBED_PATH,params={'partner':'https://evil.invalid','tuition':'<script>private()</script>'})
        self.assertNotIn('evil.invalid',widget.text)
        self.assertNotIn('private()',widget.text)

    def test_finder_catalog_follows_prices_capacities_and_location_limits(self):
        with patch.dict(billing.PLANS['micro'],{'price':12,'families':11}):
            page=self.client.get(public.FINDER_PATH).text
        catalog=json.loads(re.search(r'id="finder-catalog">(.*?)</script>',page,re.S).group(1))
        self.assertEqual(catalog[0]['price'],12)
        self.assertEqual(catalog[0]['families'],11)
        self.assertEqual(catalog[0]['locations'],1)
        self.assertIsNone(next(p for p in catalog if p['key']=='growth')['locations'])

    def test_new_events_are_fixed_path_bound_deduplicated_and_exclude_automated_conversions(self):
        counts=lambda report:{row['event']:row['count'] for row in report['stages']}
        before_human=counts(growth.report())
        before_audit=counts(growth.report(kind='all'))
        for event,path,detail in [('solution_finder_started',public.FINDER_PATH,''),('solution_recommended',public.FINDER_PATH,'tracker'),('solution_action_click',public.FINDER_PATH,'tracker'),('embed_code_copied',public.EMBED_GUIDE,'')]:
            for _ in range(2):self.assertEqual(self.event(event,path,detail).status_code,204)
            self.assertEqual(self.event(event,'/pricing',detail).status_code,400)
            self.assertEqual(self.event(event,path,'family-private-input').status_code,400)
        vid=growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        with store.db() as conn:
            events=conn.execute("SELECT * FROM growth_events WHERE visitor_id=? AND event IN ('solution_finder_started','solution_recommended','solution_action_click','embed_code_copied')",(vid,)).fetchall()
            conn.execute("UPDATE growth_visitors SET classification='KNOWN BOT' WHERE visitor_id=?",(vid,))
        self.assertEqual(len(events),4)
        human=counts(growth.report()); audit=counts(growth.report(kind='all'))
        for event in ('solution_finder_started','solution_recommended','solution_action_click','embed_code_copied'):
            self.assertEqual(human[event],before_human[event])
            self.assertEqual(audit[event],before_audit[event]+1)

    def test_public_pages_in_sitemap_widget_excluded_and_optouts_remain_usable(self):
        urls={el.text for el in ET.fromstring(self.client.get('/sitemap.xml').text).iter() if el.tag.endswith('}loc')}
        for path in (public.FINDER_PATH,public.EMBED_GUIDE):self.assertIn(public.BASE+path,urls)
        self.assertNotIn(public.BASE+public.EMBED_PATH,urls)
        for header in ('dnt','sec-gpc'):
            with TestClient(app.app,base_url=public.BASE,headers={header:'1'}) as client:
                for path in (public.FINDER_PATH,public.EMBED_GUIDE,public.EMBED_PATH):
                    page=client.get(path)
                    self.assertEqual(page.status_code,200)
                    self.assertNotIn('tp-analytics-token',page.text)
                    self.assertNotIn(growth.COOKIE,page.cookies)
