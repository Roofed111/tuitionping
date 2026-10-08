"""Scanner evidence, migrated probes, durable counts and complete request audit."""
import secrets
import unittest
from datetime import timedelta
from unittest.mock import patch
import test_traffic as traffic_tests
CHROME, SAFARI, IPHONE = traffic_tests.CHROME, traffic_tests.SAFARI, traffic_tests.IPHONE
from test_late_fee_page import app, store
import growth
import traffic

class ScannerTest(unittest.TestCase):
    setUp = traffic_tests.TrafficTest.setUp
    hit = traffic_tests.TrafficTest.hit
    row = traffic_tests.TrafficTest.row
    counts = traffic_tests.TrafficTest.counts

    def test_single_failed_wordpress_recon_is_likely_bot_with_two_evidence_facts(self):
        for path in ('/wp-login.php','/wp-admin/','/xmlrpc.php','/wp-json/','/wp-content/plugins/sample/readme.txt','/wp-includes/js/example.js','/wordpress/wp-admin/'):
            with self.subTest(path=path):
                vid=self.hit(path=path,status_code=404,ip='probe-'+path)
                r=self.row(vid)
                self.assertEqual(r['classification'],'LIKELY BOT')
                self.assertEqual(r['bot_score'],75)
                self.assertIn('WordPress endpoint',r['scan_reasons'])
                self.assertIn('404',r['scan_reasons'])
                self.assertIn(vid,{h['visitor_id'] for h in traffic.report('automated')['hits']})
        self.assertEqual(traffic.report()['human_total'],0)

    def test_explicit_exploit_targets_are_known_bot_even_with_browser_user_agent(self):
        paths=['/.env','/.env.production','/wp-config.php.bak','/.git/config','/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php','/shell.php','/.aws/credentials','/.vscode/sftp.json','/web.config','/etc/passwd','/proc/self/environ','/%252eenv']
        for path in paths:
            with self.subTest(path=path):
                vid=self.hit(path=path,status_code=404,ua=IPHONE,ip='exploit-'+path)
                self.assertEqual(self.row(vid)['classification'],'KNOWN BOT')
        self.assertEqual(traffic.report()['human_total'],0)

    def test_other_admin_and_application_scans_are_likely_bot(self):
        for path in ('/phpmyadmin/','/adminer.php','/actuator/health','/server-status','/cgi-bin/test.cgi','/manager/html','/owa/auth/logon.aspx','/autodiscover/autodiscover.xml','/.DS_Store'):
            vid=self.hit(path=path,status_code=404,ip=path)
            self.assertEqual(self.row(vid)['classification'],'LIKELY BOT')

    def test_normal_missing_pages_and_legitimate_one_page_browsers_stay_eligible(self):
        for path in ('/not-found','/guides/wordpress-security','/wp-administrator-guide','/privacy','/postcard'):
            vid=self.hit(path=path,status_code=404 if path.startswith(('/not','/guides','/wp-administrator')) else 200,ua=SAFARI,ip=path)
            self.assertEqual(self.row(vid)['classification'],'LIKELY HUMAN')
        self.assertEqual(traffic.scanner_evidence('/wp-login.php',200),('','',''))

    def test_anonymous_scans_cannot_rotate_sessions_uas_or_time_slots_into_visitors(self):
        ids=[]
        for n in range(120):
            ids.append(self.hit(path='/wp-login.php',ua=CHROME+' '+str(n),at=n*1801,status_code=404,vid=secrets.token_hex(16)))
        self.assertEqual(len(set(ids)),1)
        report=traffic.report('automated')
        self.assertEqual((report['automated_total'],report['human_total'],report['all_total']), (1,0,1))
        self.assertEqual(report['hits_total'],120)
        self.assertEqual(report['rows'][0]['hit_count'],120)
        self.assertTrue(report['hits_next'])
        second=traffic.report('automated',hit_offset=100)
        self.assertEqual(len(second['hits']),20)
        self.assertFalse(set(h['id'] for h in report['hits']) & set(h['id'] for h in second['hits']))
        self.assertIn('kind=automated',report['hits_next_url'])

    def test_rotating_signed_browser_cookies_keep_raw_records_but_one_automated_source(self):
        vids=[]
        for n in range(15):
            vid=self.hit(path='/demo',identity_kind='browser',ua=CHROME+' '+str(n))
            growth.record(vid,'page_view',path='/demo')
            growth.bind_account(vid,90000+n)
            self.hit(vid,path='/wp-login.php',status_code=404,identity_kind='browser',ua=CHROME+' '+str(n))
            vids.append(vid)
        report=traffic.report('automated')
        self.assertEqual((report['automated_total'],report['human_total'],report['all_total']), (1,0,1))
        self.assertEqual(report['filtered_total'],15)
        self.assertEqual(report['hits_total'],30)
        self.assertEqual(self.counts()['page_view'],0)
        self.assertEqual(self.counts()['signup'],0)
        audit=growth.report(kind='all')
        self.assertEqual(audit['rate_denominator'],1)
        self.assertEqual(audit['converting_visitors'],1)
        self.assertEqual(audit['conversion_rate'],100)
        self.assertEqual(audit['stages'][0]['count'],1)
        self.assertEqual(self.counts('all')['signup'],15)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM growth_accounts').fetchone()['n'],15)

    def test_same_network_human_is_not_tainted_by_a_separate_scanner(self):
        human=self.hit(identity_kind='browser')
        with patch.object(traffic,'now',return_value=self.base+timedelta(seconds=1)):
            traffic.verify_browser(human,'/demo')
        scanner=self.hit(path='/wp-login.php',status_code=404)
        self.assertNotEqual(human,scanner)
        self.assertEqual(self.row(human)['classification'],'HUMAN')
        self.assertEqual(traffic.report()['human_total'],1)

    def test_v1_probe_records_are_reclassified_without_resetting_history(self):
        vids=[]
        with store.db() as conn:
            for n in range(4):
                vid=secrets.token_hex(16);vids.append(vid)
                conn.execute("INSERT INTO growth_visitors (visitor_id,first_seen,last_seen,source,medium,campaign,landing_path,last_path,ua,ip_hash,hit_count,page_count,classification,classification_version,history) VALUES (?,?,?,?,?,?,?,?,?,?,1,1,'LIKELY HUMAN',1,0)",(vid,traffic.stamp(self.base),traffic.stamp(self.base),'direct','none','','/[probe]/WordPress endpoint','/[probe]/WordPress endpoint',CHROME,'same-v1-hash'))
                conn.execute("INSERT INTO site_visits (ts,ip_hash,path,ua,visitor_id,status_code) VALUES (?,?,?,?,?,404)",(traffic.stamp(self.base),'same-v1-hash','/[probe]/WordPress endpoint',CHROME,vid))
                growth_id=90020+n
                conn.execute("INSERT INTO growth_accounts (provider_id,visitor_id) VALUES (?,?)",(growth_id,vid))
        for vid in vids:
            growth.record(vid,'page_view',path='/demo')
        with store.db() as conn:
            before=[tuple(r) for r in conn.execute('SELECT id,ts,path,ua,visitor_id FROM site_visits ORDER BY id').fetchall()]
        report=traffic.report('automated')
        self.assertEqual(report['automated_total'],1)
        self.assertEqual(report['human_total'],0)
        self.assertEqual(report['filtered_total'],4)
        self.assertTrue(all(self.row(v)['classification']=='LIKELY BOT' for v in vids))
        traffic.refresh_candidates()
        with store.db() as conn:
            self.assertEqual(before,[tuple(r) for r in conn.execute('SELECT id,ts,path,ua,visitor_id FROM site_visits ORDER BY id').fetchall()])
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM growth_accounts').fetchone()['n'],4)
        self.assertEqual(self.counts()['page_view'],0)

    def test_post_scans_and_forged_logged_in_cookie_are_audited_without_blocking(self):
        self.client.cookies.set(app.SESSION_COOKIE,'not-a-real-login')
        response=self.client.post('/wp-login.php',content='private form value')
        self.assertEqual(response.status_code,403)  # existing CSRF response is preserved
        report=traffic.report('automated')
        self.assertEqual(report['hits_total'],1)
        self.assertEqual(report['hits'][0]['method'],'POST')
        self.assertEqual(report['hits'][0]['probe_category'],'WordPress endpoint')
        self.assertNotIn('private form value',str(report))

    def test_exploit_query_is_classified_but_payload_is_not_retained(self):
        vid=self.hit(path='/demo',scan_query='x=%3C%3Fphp%20private_payload',identity_kind='browser')
        self.assertEqual(self.row(vid)['classification'],'KNOWN BOT')
        self.assertNotIn('private_payload',str(traffic.report('automated')))
        self.assertEqual(traffic.report('automated')['hits'][0]['probe_category'],'Exploit query payload')

    def test_known_bot_anonymous_ids_are_stable_across_time_and_version(self):
        a=traffic.fallback_id('sample','curl/8',self.base)
        b=traffic.fallback_id('sample','curl/9',self.base+timedelta(days=2))
        self.assertEqual(a,b)
        self.assertNotEqual(traffic.fallback_id('sample',CHROME,self.base),traffic.fallback_id('sample',CHROME,self.base+timedelta(hours=1)))

if __name__=='__main__':unittest.main()
