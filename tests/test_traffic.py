"""Traffic classification, shared conversions, history, and full-list sorting.

Uses only temporary local data; no real billing, SMS, or external APIs.
"""
import json
import asyncio
import re
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import billing
import growth
import partner_resources
import traffic

CHROME = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36'
SAFARI = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15'
IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1'


class TrafficTest(unittest.TestCase):
    def setUp(self):
        growth.ensure_tables()
        with store.db() as conn:
            for table in ('traffic_classifications', 'site_visits', 'growth_events', 'growth_accounts',
                          'growth_partner_accounts', 'growth_partner_visitors', 'growth_visitors', 'growth_partners'):
                conn.execute('DELETE FROM ' + table)
            conn.execute("UPDATE growth_report_state SET event_floor=0,generation='',reset_at='',reset_by=NULL WHERE id=1")
        self.base = datetime.now(timezone.utc).replace(microsecond=0)
        self.client = TestClient(app.app, base_url='https://www.tuitionping.com', headers={'user-agent': CHROME})
        self.addCleanup(self.client.close)

    def hit(self, vid=None, path='/demo', ua=CHROME, at=0, ip='203.0.113.10', **kwargs):
        with patch.object(traffic, 'now', return_value=self.base + timedelta(seconds=at)):
            return traffic.observe(ip, path, '', ua, visitor_id=vid or secrets.token_hex(16), **kwargs)

    def row(self, vid):
        with store.db() as conn:
            return dict(conn.execute('SELECT * FROM growth_visitors WHERE visitor_id=?', (vid,)).fetchone())

    def verify(self, vid, path='/demo', at=1, webdriver=False):
        with patch.object(traffic, 'now', return_value=self.base + timedelta(seconds=at)):
            self.assertTrue(traffic.verify_browser(vid, path, webdriver))

    def counts(self, kind='human'):
        return {s['event']: s['count'] for s in growth.report(kind=kind)['stages']}

    def test_chrome_safari_iphone_direct_and_single_page_stay_human_eligible(self):
        for ua in (CHROME, SAFARI, IPHONE, CHROME.replace('Windows NT 10.0', 'Android; CUBOT X30')):
            with self.subTest(ua=ua):
                vid = self.hit(ua=ua)
                with patch.object(traffic, 'now', return_value=self.base + timedelta(seconds=45)):
                    traffic.refresh_candidates()
                r = self.row(vid)
                self.assertEqual(r['classification'], 'LIKELY HUMAN')
                self.assertEqual((r['hit_count'], r['page_count'], r['source']), (1, 1, 'direct'))
                self.verify(vid)
                self.assertEqual(self.row(vid)['classification'], 'HUMAN')

    def test_known_user_agents_and_verified_automation_remain_auditable(self):
        agents = ['Googlebot/2.1', 'bingbot/2.0', 'DuckDuckBot/1.1', 'YandexBot/3.0', 'Baiduspider',
                  'AhrefsBot/7', 'SemrushBot/7', 'MJ12bot/v1', 'facebookexternalhit/1.1', 'Twitterbot/1.0',
                  'LinkedInBot/1', 'Slackbot-LinkExpanding/1', 'Discordbot/2', 'WhatsApp/2.26', 'Applebot/0.1',
                  'UptimeRobot/2', 'Pingdom.com_bot_version_1.4', 'kube-probe/1.20', 'Nuclei', 'sqlmap/1',
                  'Mozilla/5.0 HeadlessChrome/130', 'curl/8.5', 'Wget/1.21', 'python-requests/2.32',
                  'python-httpx/0.28', 'Go-http-client/1.1', 'axios/1.0', 'ExampleBot', 'generic crawler']
        for ua in agents:
            with self.subTest(ua=ua):
                vid = self.hit(ua=ua)
                self.assertEqual(self.row(vid)['classification'], 'KNOWN BOT')
                self.assertTrue(json.loads(self.row(vid)['classification_reasons']))
        vid = self.hit()
        self.verify(vid, webdriver=True)
        self.verify(vid, webdriver=False)
        self.assertEqual(self.row(vid)['classification'], 'KNOWN BOT')
        report = traffic.report('all')
        self.assertEqual(report['human_total'], 0)
        self.assertEqual(report['automated_total'], 1)
        self.assertEqual(report['all_total'], 1)
        self.assertEqual(report['hits_total'], len(agents) + 1)

    def test_direct_qr_and_privacy_visitors_are_not_bots(self):
        for headers in ({}, {'dnt': '1'}, {'sec-gpc': '1'}):
            with self.subTest(headers=headers), TestClient(app.app, base_url='https://www.tuitionping.com', headers={'user-agent': IPHONE, **headers}) as client:
                response = client.get('/postcard?utm_campaign=qr&email=private@example.invalid')
                self.assertEqual(response.status_code, 200)
                with store.db() as conn:
                    latest = dict(conn.execute('SELECT * FROM growth_visitors ORDER BY last_seen DESC LIMIT 1').fetchone())
                self.assertEqual(latest['classification'], 'LIKELY HUMAN')
                self.assertEqual(latest['source'], 'postcard')
                if headers:
                    self.assertNotIn(growth.COOKIE, client.cookies)
                    self.assertNotIn('tp-analytics-token', response.text)
                self.assertNotIn('private@example', str(latest))

    def test_signed_browser_beacon_is_after_hit_and_does_not_inflate_counts(self):
        response = self.client.get('/demo')
        token = re.search('name="tp-analytics-token" content="([a-f0-9]+)"', response.text).group(1)
        vid = growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        event = {'event': 'browser_verified', 'path': '/demo'}
        for _ in range(2):
            self.assertEqual(self.client.post('/analytics/event', json=event, headers={'x-tp-analytics': token}).status_code, 204)
        self.assertEqual((self.row(vid)['hit_count'], self.row(vid)['page_count']), (1, 1))
        self.assertEqual(self.row(vid)['classification'], 'HUMAN')
        self.assertEqual(self.counts()['page_view'], 1)
        with store.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM growth_events WHERE event='browser_verified'").fetchone()['n'], 0)
        self.assertEqual(self.client.post('/analytics/event', json=event, headers={'x-tp-analytics': 'forged'}).status_code, 403)
        self.assertEqual(self.client.post('/analytics/event', json={**event, 'path': '/guides'}, headers={'x-tp-analytics': token}).status_code, 400)
        self.assertFalse(traffic.verify_browser('f' * 32, '/demo'))

    def test_normal_navigation_and_refreshes_do_not_create_duplicate_pages(self):
        vid = self.hit()
        self.verify(vid)
        for at, path in [(3,'/guides'), (7,'/'), (12,'/demo'), (13,'/demo'), (15,'/demo'), (18,'/guides')]:
            self.hit(vid, path=path, at=at)
        r = self.row(vid)
        self.assertEqual(r['classification'], 'HUMAN')
        self.assertEqual((r['hit_count'], r['page_count']), (7, 3))

    def test_one_weak_signal_is_insufficient(self):
        missing = self.hit(ua='')
        self.assertEqual(self.row(missing)['classification'], 'LIKELY HUMAN')
        fast = self.hit()
        for n in range(1, 12):
            self.hit(fast, path='/page-' + str(n), at=n / 20)
        self.assertEqual(self.row(fast)['classification'], 'LIKELY HUMAN')
        # Velocity is one group; no-JS is not inferred before the grace period.
        self.assertEqual(self.row(fast)['bot_score'], 40)

    def test_rapid_repetition_and_missing_execution_need_independent_signals(self):
        vid = self.hit()
        for n in range(1, 22):
            self.hit(vid, at=n / 100)
        self.assertEqual(self.row(vid)['classification'], 'LIKELY BOT')
        reasons = json.loads(self.row(vid)['classification_reasons'])
        self.assertTrue(any('10 seconds' in s for s in reasons))
        self.assertTrue(any('Repetitive' in s for s in reasons))
        with patch.object(traffic, 'now', return_value=self.base + timedelta(minutes=3)):
            traffic.refresh_candidates()
        self.assertEqual(self.row(vid)['classification'], 'LIKELY BOT')
        # A calm browser-confirmed return can recover a likely-bot false positive.
        self.hit(vid, at=180)
        self.verify(vid, at=185)
        self.assertEqual(self.row(vid)['classification'], 'HUMAN')

    def test_many_urls_without_js_become_likely_bot_after_grace(self):
        vid = self.hit()
        for n in range(1, 12):
            self.hit(vid, path='/different-' + str(n), at=n / 50)
        self.assertEqual(self.row(vid)['classification'], 'LIKELY HUMAN')
        with patch.object(traffic, 'now', return_value=self.base + timedelta(seconds=25)):
            traffic.refresh_candidates()
        self.assertEqual(self.row(vid)['classification'], 'LIKELY BOT')

    def test_delayed_report_still_reviews_the_original_burst(self):
        vid = self.hit()
        for n in range(1, 12):
            self.hit(vid, path='/different-' + str(n), at=n / 50)
        with patch.object(traffic, 'now', return_value=self.base + timedelta(hours=2)):
            traffic.refresh_candidates()
        self.assertEqual(self.row(vid)['classification'], 'LIKELY BOT')
        self.assertEqual(self.row(vid)['review_pending'], 0)

    def test_simultaneous_sessions_require_cluster_and_no_execution(self):
        vids = [self.hit(identity_kind='browser', at=.1) for _ in range(14)]
        self.assertTrue(all(self.row(v)['classification'] == 'LIKELY HUMAN' for v in vids))
        with patch.object(traffic, 'now', return_value=self.base + timedelta(seconds=25)):
            traffic.refresh_candidates()
        self.assertTrue(all(self.row(v)['classification'] == 'LIKELY BOT' for v in vids))
        self.assertTrue(any('14 browser identifiers' in s for s in json.loads(self.row(vids[0])['classification_reasons'])))

    def test_shared_network_verified_browsers_are_not_removed_by_cluster_alone(self):
        vids = []
        for _ in range(14):
            vid = self.hit(identity_kind='browser', at=.1)
            self.verify(vid, at=.2)
            vids.append(vid)
        with patch.object(traffic, 'now', return_value=self.base + timedelta(seconds=25)):
            traffic.refresh_candidates()
        self.assertTrue(all(self.row(v)['classification'] in traffic.HUMAN_TYPES for v in vids))

    def test_scanner_paths_are_categorized_without_private_urls(self):
        vid = self.hit(ua='', identity_kind='browser')
        for n, path in enumerate(['/.env?secret=private', '/.git/config', '/wp-login.php']):
            self.hit(vid, path=path, ua='', status_code=404, at=n + 1, identity_kind='browser')
        self.assertEqual(self.row(vid)['classification'], 'KNOWN BOT')
        with store.db() as conn:
            saved = str([dict(r) for r in conn.execute('SELECT * FROM site_visits').fetchall()])
        self.assertNotIn('secret=private', saved)
        self.assertNotIn('203.0.113.10', saved)
        self.assertIn('/[probe]/', saved)

    def test_scanners_monitors_and_bots_are_logged_without_blocking(self):
        for ua, path, status in [('Googlebot/2.1','/demo',200), ('curl/8','/',200), ('UptimeRobot/2','/healthz',200), ('Nuclei','/.env',404)]:
            response = self.client.get(path, headers={'user-agent': ua})
            self.assertEqual(response.status_code, status)
        self.assertEqual(traffic.report()['automated_total'], 1)
        self.assertEqual(self.counts()['page_view'], 0)
        before = traffic.report('all')['all_total']
        self.client.get('/static/growth.js')
        self.assertEqual(traffic.report('all')['all_total'], before)

    def test_history_is_preserved_classified_only_with_available_evidence(self):
        old = (self.base - timedelta(days=120)).isoformat(timespec='seconds')
        with store.db() as conn:
            for ua in ('Googlebot/2.1', SAFARI, ''):
                for _ in range(15):
                    conn.execute('INSERT INTO site_visits (ts,ip_hash,path,referrer,ua) VALUES (?,?,?,?,?)', (old,'oldhash','/demo','https://example.org/?email=private',ua))
            before = [tuple(r) for r in conn.execute('SELECT id,ts,ip_hash,path,referrer,ua FROM site_visits').fetchall()]
            growth_cookie = secrets.token_hex(16)
            conn.execute('INSERT INTO growth_visitors (visitor_id,first_seen,source,medium,campaign,landing_path) VALUES (?,?,?,?,?,?)', (growth_cookie,old,'direct','none','','/'))
        report = traffic.report('all')
        self.assertEqual((report['human_total'],report['automated_total'],report['unknown_total']), (1,1,1))
        traffic.backfill()
        with store.db() as conn:
            after = [tuple(r) for r in conn.execute('SELECT id,ts,ip_hash,path,referrer,ua FROM site_visits').fetchall()]
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM traffic_classifications').fetchone()['n'], 2)
        self.assertEqual(before, after)
        self.assertEqual(self.row(growth_cookie)['classification'], 'UNKNOWN')
        self.assertEqual(sum(r['hit_count'] for r in report['rows']), 45)

    def test_human_only_conversion_rates_retain_bot_and_unknown_events(self):
        humans = [self.hit(ip='human-' + str(n)) for n in range(31)]
        for vid in humans:
            growth.record(vid, 'page_view', path='/demo')
        for n, vid in enumerate(humans[:3]):
            growth.bind_account(vid, 60000+n)
            growth.milestone(60000+n, 'paid_customer')
        known = self.hit(ua='Googlebot/2.1')
        likely = self.hit()
        for n in range(21):
            self.hit(likely, at=n / 100)
        unknown = secrets.token_hex(16)
        growth.register(unknown, 'direct', 'none', '', '/demo')
        for n, vid in enumerate((known, likely, unknown)):
            growth.record(vid, 'page_view', path='/demo')
            growth.bind_account(vid, 70000+n)
            growth.milestone(70000+n, 'paid_customer')
        report = growth.report()
        self.assertEqual((report['rate_denominator'], report['converting_visitors'], report['conversion_rate']), (31,3,9.68))
        self.assertEqual(report['paid_conversion_rate'], 9.68)
        self.assertEqual(self.counts()['paid_customer'], 3)
        self.assertEqual(self.counts('all')['paid_customer'], 6)
        self.assertEqual(self.counts('automated')['signup'], 2)
        self.assertEqual(self.counts('unknown')['signup'], 1)
        with store.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM growth_events WHERE event='paid_customer'").fetchone()['n'], 6)
        growth.bind_account(humans[0], 80000)
        self.assertEqual(growth.report()['conversion_rate'], 9.68)

    def test_paid_events_require_positive_live_subscription_confirmation(self):
        vid = self.hit()
        pid = store.create_provider('Billing sample', secrets.token_hex(6)+'@example.invalid', 'password123')
        growth.bind_account(vid, pid)
        with patch.object(billing, '_stripe_lib', return_value=MagicMock()), patch.object(store, 'get_provider_by_stripe_customer', return_value={'id':pid}), patch('referrals.record_paid_invoice'):
            for invoice in [dict(livemode=False,amount_paid=1900,status='paid'), dict(livemode=True,amount_paid=0,status='paid'), dict(livemode=True,amount_paid=1900,status='open'), dict(livemode=True,amount_paid=1900,status='paid',paid=False)]:
                billing.handle_stripe_event({'type':'invoice.paid','data':{'object':dict(customer='cus_fake',subscription='sub_fake',**invoice)}})
                self.assertEqual(self.counts()['paid_customer'], 0)
            billing.handle_stripe_event({'type':'invoice.paid','data':{'object':dict(livemode=True,customer='cus_fake',subscription='sub_fake',amount_paid=1900,status='paid',paid=True)}})
        self.assertEqual(self.counts()['paid_customer'], 1)

    def test_partner_and_qr_reports_exclude_bot_attribution_without_erasing_it(self):
        with store.db() as conn:
            conn.execute('INSERT INTO growth_partners (code,name,kind,created_at) VALUES (?,?,?,?)', ('sample-group','Sample','provider_group',store.now_iso()))
        accounts = []
        for n, ua in enumerate((CHROME, 'Googlebot/2.1')):
            vid = self.hit(path='/postcard', ua=ua)
            growth.record(vid, 'page_view', path='/postcard')
            partner_resources.touch(vid, 'sample-group')
            growth.bind_account(vid, 81000+n)
            growth.milestone(81000+n, 'paid_customer')
            accounts.append(dict(id=81000+n,signup_source='postcard',sub_status='active'))
        report = partner_resources.report()[0]
        self.assertEqual((report['visitors'],report['signups'],report['paid']), (1,1,1))
        qr = traffic.postcard_summary(accounts)
        self.assertEqual((qr['visits'],qr['attributed'],qr['customers'],qr['automated']), (1,1,1,1))
        self.assertEqual(accounts[1]['traffic_type'], 'Known Bot')
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM growth_partner_accounts').fetchone()['n'], 2)

    def test_filters_sort_every_column_across_pagination_and_reject_sql_input(self):
        for n in range(125):
            vid = self.hit(at=-172800+n, ip='sort-'+str(n), source='source-%03d' % (124-n), identity_kind='browser')
            with store.db() as conn:
                conn.execute('UPDATE growth_visitors SET hit_count=?,page_count=?,last_path=? WHERE visitor_id=?', (n+1,n+1,'/page-%03d' % (124-n),vid))
        high = traffic.report('human', sort='hits', direction='desc')
        low = traffic.report('human', offset=100, sort='hits', direction='desc')
        self.assertEqual((high['rows'][0]['hit_count'],low['rows'][0]['hit_count'],low['rows'][-1]['hit_count']), (125,25,1))
        self.assertIn('kind=human', high['next_url'])
        self.assertIn('sort=hits', high['next_url'])
        for key in traffic.SORT_COLUMNS:
            up = traffic.report('all', sort=key, direction='asc')
            down = traffic.report('all', sort=key, direction='desc')
            self.assertEqual(up['filtered_total'], 125)
            self.assertEqual(up['headers'][list(traffic.SORT_COLUMNS).index(key)]['aria_sort'], 'ascending')
            expression = {'visitor':'visitor_id','hits':'hit_count','pages':'page_count','first_seen':'first_seen','last_seen':'last_seen','last_page':'last_path','source':'source'}.get(key)
            if expression:
                vals = [r[expression] for r in up['rows']]
                self.assertEqual(vals, sorted(vals))
                self.assertNotEqual(up['rows'][0][expression], down['rows'][0][expression])
        bad = traffic.report("human'; DROP TABLE growth_visitors", sort='hit_count; DELETE FROM site_visits', direction='desc; DELETE')
        self.assertEqual((bad['kind'],bad['sort'],bad['direction']), ('engaged','last_seen','desc'))
        self.hit(ua='curl/8')
        self.assertEqual(traffic.report('automated')['filtered_total'], 1)
        self.assertEqual(traffic.report('human')['filtered_total'], 125)
        self.assertEqual(traffic.report('all')['filtered_total'], 126)

    def test_admin_sort_links_reasons_filters_and_pacific_dates_are_private(self):
        winter = datetime(2026,1,15,2,30,tzinfo=timezone.utc)
        with patch.object(traffic,'now',return_value=winter):
            traffic.observe('sample','/demo','https://example.org/?email=private',SAFARI)
        self.hit(ua='Googlebot/2.1')
        self.assertNotEqual(self.client.get('/admin/visitors',follow_redirects=False).status_code,200)
        with patch.object(app,'require_admin',return_value=({'id':1},None)):
            response = self.client.get('/admin/visitors?kind=all&sort=hits&direction=asc')
            self.assertEqual(response.status_code,200)
            self.assertIn('Engaged Visitors Today',response.text)
            self.assertIn('Automated Traffic Total',response.text)
            self.assertIn('aria-sort="ascending"',response.text)
            self.assertIn('2026-01-14 6:30:00 PM PST',response.text)
            self.assertIn('PDT',response.text)
            self.assertIn('User-Agent matched Google crawler',response.text)
            self.assertNotIn('email=private',response.text)
            self.assertEqual(response.headers['cache-control'],'private, no-store')
            self.assertEqual(response.headers['x-robots-tag'],'noindex')
            self.assertEqual(self.client.get('/admin/visitors?kind=automated').context['report']['filtered_total'],1)
            self.assertEqual(self.client.get('/admin/visitors?kind=human').context['report']['filtered_total'],1)

    def test_pacific_day_counts_any_hit_not_just_last_seen_and_dst_day_lengths(self):
        for start, end, hours in [('2026-03-08T08:00:00+00:00','2026-03-09T07:00:00+00:00',23),
                                  ('2026-11-01T07:00:00+00:00','2026-11-02T08:00:00+00:00',25)]:
            with self.subTest(hours=hours):
                with store.db() as conn:
                    conn.execute('DELETE FROM site_visits');conn.execute('DELETE FROM growth_visitors')
                a,b = traffic.parsed(start),traffic.parsed(end)
                self.assertEqual((b-a).total_seconds()/3600,hours)
                with patch.object(traffic,'now',return_value=a-timedelta(seconds=1)):
                    vid = traffic.observe('sample','/demo','',CHROME)
                with patch.object(traffic,'now',return_value=a):
                    traffic.observe('sample','/demo','',CHROME,visitor_id=vid)
                with patch.object(traffic,'now',return_value=b):
                    traffic.observe('sample','/demo','',CHROME,visitor_id=vid)
                    traffic.observe('another','/demo','',CHROME)
                with patch.object(traffic,'now',return_value=b-timedelta(seconds=1)):
                    self.assertEqual(traffic.report()['human_today'],1)
                with patch.object(traffic,'now',return_value=b):
                    self.assertEqual(traffic.report()['human_today'],2)

    def test_concurrent_hits_keep_counts_and_schema_migration_is_idempotent(self):
        vid = self.hit(at=-120)
        def hit(n):
            return traffic.observe('sample','/demo','',CHROME,visitor_id=vid)
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(len(list(pool.map(hit,range(15)))),15)
        self.assertEqual((self.row(vid)['hit_count'],self.row(vid)['page_count']),(16,1))
        with store.db() as conn:
            traffic.ensure_schema(conn);traffic.ensure_schema(conn)
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM site_visits WHERE visitor_id=?',(vid,)).fetchone()['n'],16)

    def test_background_maintenance_is_bounded_and_cancels_cleanly(self):
        async def check():
            completed = asyncio.Event()
            sleeps = []
            async def controlled_sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) > 1:
                    completed.set()
                    await asyncio.Future()
            with patch.object(app.asyncio, 'sleep', side_effect=controlled_sleep), patch.object(traffic,'backfill',return_value=0) as backfill, patch.object(traffic,'refresh_candidates') as refresh:
                async with app.analytics_lifespan(app.app):
                    await asyncio.wait_for(completed.wait(),timeout=2)
                backfill.assert_called_once_with()
                refresh.assert_called_once_with()
                self.assertEqual(sleeps, [30,30])
        asyncio.run(check())

    def test_automated_spike_and_public_fail_open(self):
        vid = self.hit(ua='curl/8')
        for _ in range(31):
            self.hit(vid,ua='curl/8')
        self.assertTrue(traffic.report()['spike'])
        with patch.object(store,'log_site_visit',side_effect=RuntimeError('offline')):
            self.assertEqual(self.client.get('/demo').status_code,200)


if __name__ == '__main__':
    unittest.main()
