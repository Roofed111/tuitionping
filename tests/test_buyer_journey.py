"""Buyer-page discoverability and resource-to-account measurement integrity."""
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


class BuyerJourneyTest(unittest.TestCase):
    def setUp(self):
        growth.ensure_tables()
        with store.db() as conn:
            for table in ('growth_events', 'growth_accounts', 'growth_visitors'):
                conn.execute('DELETE FROM ' + table)
            conn.execute("UPDATE growth_report_state SET event_floor=0, generation='', reset_at='', reset_by=NULL WHERE id=1")
        self.client = TestClient(app.app, base_url='https://www.tuitionping.com',
                                 headers={'user-agent': 'Mozilla/5.0 Buyer journey browser'})

    def tearDown(self):
        self.client.close()

    def visit(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.token = re.search(r'name="tp-analytics-token" content="([a-f0-9]+)"', response.text).group(1)
        return response.text

    def event(self, event, path, detail=''):
        return self.client.post('/analytics/event', headers={'x-tp-analytics': self.token},
                                json={'event': event, 'path': path, 'detail': detail})

    def test_new_pages_are_public_canonical_unique_and_linked_in_sitemap(self):
        for path in ('/preschool-tuition-reminders', '/compare/spreadsheets'):
            page = self.visit(path + '?utm_source=test')
            self.assertEqual(len(re.findall(r'<h1[ >]', page)), 1)
            self.assertIn('href="https://www.tuitionping.com' + path + '"', page)
            self.assertIn('href="/demo"', page)
            self.assertIn('href="/pricing', page)
            self.assertNotIn('noindex', self.client.get(path).headers.get('x-robots-tag', ''))
            self.assertIn(path, self.client.get('/guides').text)
        urls = {e.text for e in ET.fromstring(self.client.get('/sitemap.xml').text).iter() if e.tag.endswith('}loc')}
        self.assertIn('https://www.tuitionping.com/preschool-tuition-reminders', urls)
        self.assertIn('https://www.tuitionping.com/compare/spreadsheets', urls)
        preschool = self.client.get('/preschool-tuition-reminders').text
        schema = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', preschool, re.S).group(1))
        self.assertEqual(schema['@type'], 'FAQPage')
        self.assertIn('school calendar', preschool)
        self.assertIn('monthly due-day', preschool)
        with TestClient(app.app, base_url='https://tuitionping.com') as bare:
            response = bare.get('/compare/spreadsheets?utm_source=test', follow_redirects=False)
            self.assertEqual(response.status_code, 308)
            self.assertEqual(response.headers['location'], 'https://www.tuitionping.com/compare/spreadsheets?utm_source=test')

    def test_prices_and_trial_terms_follow_the_existing_catalog(self):
        with patch.dict(billing.PLANS['micro'], {'price': 12, 'families': 11}), \
                patch.dict(billing.PLANS['starter'], {'price': 23, 'families': 31}):
            comparison = self.client.get('/compare/spreadsheets').text
            self.assertIn('$12/month for up to 11', comparison)
            self.assertIn('$23/month for up to 31', comparison)
            for path in growth.RESOURCE_EVENT_PATHS.values():
                page = self.client.get(path).text
                self.assertIn('$12/month for up to 11', page)
                self.assertIn('requires a card', page)
                self.assertIn('not copied into the demo', page)

    def test_resource_labels_are_path_bound_and_cannot_inject_private_data(self):
        self.visit('/guides/tuition-reminder-templates')
        for event in ('resource_next_step', 'resource_demo_click'):
            self.assertEqual(self.event(event, '/guides/tuition-reminder-templates', 'reminder').status_code, 204)
            self.assertEqual(self.event(event, '/pricing', 'reminder').status_code, 400)
            self.assertEqual(self.event(event, '/guides/tuition-reminder-templates', 'private-family-name').status_code, 400)
        self.assertEqual(self.event('demo_completed', '/pricing').status_code, 400)
        self.assertEqual(self.event('paid_customer', '/demo').status_code, 400)

    def test_resource_demo_and_account_stages_share_the_original_landing_page(self):
        landing = '/tools/tuition-payment-tracker'
        self.visit(landing + '?utm_source=sample&utm_medium=partner')
        self.client.get('/static/downloads/daycare-tuition-payment-tracker.xlsx')
        for _ in range(2):
            self.assertEqual(self.event('resource_next_step', landing, 'tracker').status_code, 204)
            self.assertEqual(self.event('resource_demo_click', landing, 'tracker').status_code, 204)
        self.visit('/demo')
        self.assertEqual(self.event('demo_started', '/demo').status_code, 204)
        self.assertEqual(self.event('demo_completed', '/demo').status_code, 204)
        vid = growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        pid = store.create_provider('Fictional program', secrets.token_hex(8) + '@example.invalid', 'password123')
        growth.bind_account(vid, pid)
        report = growth.report()
        row = report['pages'][0]
        self.assertEqual(row['key'], (landing,))
        self.assertEqual((row['visitors'], row['resources'], row['demo'], row['demo_completed'], row['signups']), (1, 1, 1, 1, 1))
        self.assertEqual((row['trials'], row['paid']), (0, 0))
        self.assertEqual(report['sources'][0]['key'], ('sample', 'partner', ''))
        with store.db() as conn:
            prompt_events = conn.execute("SELECT COUNT(*) AS n FROM growth_events WHERE event='resource_next_step'").fetchone()['n']
        self.assertEqual(prompt_events, 1)

    def test_bot_traffic_stays_out_of_human_resource_and_demo_counts(self):
        self.visit('/tools/tuition-payment-tracker')
        self.event('resource_next_step', '/tools/tuition-payment-tracker', 'tracker')
        self.visit('/demo')
        self.event('demo_completed', '/demo')
        vid = growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        with store.db() as conn:
            conn.execute("UPDATE growth_visitors SET classification='KNOWN BOT' WHERE visitor_id=?", (vid,))
        human = {row['event']: row['count'] for row in growth.report()['stages']}
        audit = {row['event']: row['count'] for row in growth.report(kind='all')['stages']}
        self.assertEqual(human['resource_next_step'], 0)
        self.assertEqual(human['demo_completed'], 0)
        self.assertEqual(audit['resource_next_step'], 1)
        self.assertEqual(audit['demo_completed'], 1)


if __name__ == '__main__':
    unittest.main()
