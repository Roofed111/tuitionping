"""Public pricing, plan selection and resource-listing regression checks."""
import re
import unittest
import zipfile
from io import BytesIO
from unittest.mock import patch
from xml.etree import ElementTree as ET
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import billing
import partner_resources


class PricingSeoTest(unittest.TestCase):
    def test_pricing_is_public_canonical_and_in_the_sitemap(self):
        provider = {'id': 1, 'email': 'test@example.invalid', 'name': 'Test',
                    'suspended': False, 'is_admin': False}
        with patch.object(store, 'get_provider_by_session', return_value=provider), \
                patch.object(store, 'get_subscription', return_value={'status': 'expired'}), \
                TestClient(app.app) as client:
            client.cookies.set(app.SESSION_COOKIE, 'test-session')
            response = client.get('/pricing?utm_source=sample', follow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('noindex', response.headers.get('x-robots-tag', ''))
            self.assertIn('href="https://www.tuitionping.com/pricing"', response.text)
            self.assertEqual(len(re.findall(r'<h1[ >]', response.text)), 1)
            urls = {e.text for e in ET.fromstring(client.get('/sitemap.xml').text).iter()
                    if e.tag.endswith('}loc')}
            self.assertIn('https://www.tuitionping.com/pricing', urls)
        with TestClient(app.app, base_url='https://tuitionping.com') as client:
            response = client.get('/pricing?utm_source=sample', follow_redirects=False)
            self.assertEqual(response.status_code, 308)
            self.assertEqual(response.headers['location'],
                             'https://www.tuitionping.com/pricing?utm_source=sample')

    def test_plan_cards_use_the_billing_catalog_and_valid_signup_targets(self):
        with TestClient(app.app) as client:
            page = client.get('/pricing').text
            for key, plan in billing.PLANS.items():
                with self.subTest(plan=key):
                    card = re.search(r'<article[^>]+id="plan-' + key + r'">(.*?)</article>', page, re.S).group(1)
                    self.assertIn(f'${plan["price"]}<span>/month', card)
                    self.assertIn(f'${plan["price"] * 10}/year', card)
                    self.assertIn(plan['blurb'], card)
                    self.assertIn('/signup?plan=' + key, card)
                    self.assertEqual(client.get('/signup?plan=' + key).status_code, 200)
            self.assertIn('full yearly price after the trial', page)
            self.assertIn('requires a card', page)
            self.assertEqual(client.get('/static/pricing.css').status_code, 200)
            # A changed catalog must render dynamically instead of leaving a
            # stale price in the page, headline, metadata or yearly display.
            with patch.dict(billing.PLANS['micro'], {'price': 12, 'families': 11,
                                                     'blurb': 'Up to 11 families · 1 location'}):
                changed = client.get('/pricing').text
                self.assertIn('from $12/month', changed)
                self.assertIn('$120/year', changed)
                self.assertIn('Up to 11 families', changed)

    def test_buyer_pages_link_to_pricing_and_describe_payment_review(self):
        with TestClient(app.app) as client:
            for path in ['/', '/home-daycare-tuition-reminders', '/small-daycare-tuition-reminders']:
                page = client.get(path).text
                with self.subTest(path=path):
                    self.assertIn('href="/pricing', page)
                    self.assertIn('PAID', page)
                    self.assertIn('monthly', page.lower())
                    self.assertEqual(len(re.findall(r'<h1[ >]', page)), 1)
            home = client.get('/home-daycare-tuition-reminders').text
            center = client.get('/small-daycare-tuition-reminders').text
            self.assertIn('/pricing#plan-micro', home)
            self.assertIn('/pricing#plan-starter', center)
            self.assertIn('does not move money', home)
            self.assertIn('does not establish that a parent read', center)

    def test_directory_copy_and_kit_preserve_assigned_resource_links(self):
        with TestClient(app.app) as client:
            page = client.get('/partners').text
            self.assertIn('id="reminder-listing"', page)
            self.assertIn('Copy generator description and link', page)
            self.assertIn('separate paid service', page)
        for code in ['', 'test-association']:
            with self.subTest(partner=code):
                archive = zipfile.ZipFile(BytesIO(partner_resources.package(code)))
                self.assertIsNone(archive.testzip())
                copy = archive.read('Ready-to-share-copy.txt').decode()
                self.assertIn('RESOURCE DIRECTORY LISTING', copy)
                self.assertIn(partner_resources.resource_link(code, 'tuition-reminder-templates'), copy)
                self.assertIn(partner_resources.resource_link(code, 'tuition-payment-tracker'), copy)


if __name__ == '__main__':
    unittest.main()
