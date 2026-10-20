"""Public-page access, canonicalization and downloadable asset checks.
No credentials, external messages or production databases are used.
"""
import os
import re
import json
import unittest
import zipfile
from io import BytesIO
from unittest.mock import patch
from xml.etree import ElementTree as ET
from test_late_fee_page import app, store
from fastapi.testclient import TestClient

class PublicResourcesTest(unittest.TestCase):
    def test_free_pages_remain_free_to_expired_subscribers(self):
        provider={'id':1,'email':'test@example.invalid','suspended':False,'name':'Test','is_admin':False}
        with patch.object(store,'get_provider_by_session',return_value=provider), patch.object(store,'get_subscription',return_value={'status':'expired'}):
            with TestClient(app.app) as client:
                client.cookies.set(app.SESSION_COOKIE,'test-session')
                for path in ['/guides','/guide','/late-fee-policy','/guides/tuition-reminder-templates','/guides/handling-late-paying-parents','/tools/tuition-payment-tracker','/about']:
                    with self.subTest(path=path):self.assertEqual(client.get(path,follow_redirects=False).status_code,200)
                self.assertEqual(client.get('/dashboard',follow_redirects=False).status_code,303)

    def test_public_redirect_preserves_query_but_not_account_or_post_paths(self):
        with TestClient(app.app,base_url='https://tuitionping.com') as client:
            r=client.get('/tools/late-fee-calculator?type=daily&amount=5',follow_redirects=False)
            self.assertEqual(r.status_code,308)
            self.assertEqual(r.headers['location'],'https://www.tuitionping.com/tools/late-fee-calculator?type=daily&amount=5')
            self.assertEqual(client.get('/login',follow_redirects=False).status_code,200)
            self.assertEqual(client.get('/healthz',follow_redirects=False).status_code,200)
            self.assertNotEqual(client.post('/guide',follow_redirects=False).status_code,308)
        with TestClient(app.app) as client:self.assertEqual(client.get('/guide',follow_redirects=False).status_code,200)

    def test_sitemap_dates_do_not_depend_on_deployment_timestamps(self):
        with patch.object(os.path,'getmtime',side_effect=AssertionError('filesystem timestamp should not be used')):
            with TestClient(app.app) as client:
                xml=ET.fromstring(client.get('/sitemap.xml').text)
                ns={'s':'http://www.sitemaps.org/schemas/sitemap/0.9'}
                urls=[el.text for el in xml.findall('s:url/s:loc',ns)]
                self.assertIn('https://www.tuitionping.com/tools/tuition-payment-tracker',urls)
                self.assertIn('https://www.tuitionping.com/about',urls)
                self.assertNotIn('https://www.tuitionping.com/signup',urls)
                self.assertNotIn('https://www.tuitionping.com/login',urls)
                for el in xml.findall('s:url/s:lastmod',ns):self.assertRegex(el.text,r'^\d{4}-\d{2}-\d{2}$')

    def test_auth_and_download_headers(self):
        with TestClient(app.app) as client:
            for path in ['/login','/signup']:
                self.assertEqual(client.get(path).headers['x-robots-tag'],'noindex')
            self.assertNotIn('x-robots-tag',client.get('/guides').headers)
            for file,signature in [('daycare-late-fee-policy.pdf',b'%PDF'),('daycare-late-fee-policy.docx',b'PK'),('daycare-tuition-reminders.pdf',b'%PDF'),('daycare-tuition-reminders.docx',b'PK'),('daycare-tuition-payment-tracker.xlsx',b'PK'),('daycare-tuition-collection-kit.zip',b'PK')]:
                r=client.get('/static/downloads/'+file);self.assertEqual(r.status_code,200);self.assertTrue(r.content.startswith(signature));self.assertEqual(r.headers['x-robots-tag'],'noindex')
            kit=zipfile.ZipFile(BytesIO(client.get('/static/downloads/daycare-tuition-collection-kit.zip').content))
            self.assertIsNone(kit.testzip());self.assertEqual(len(kit.namelist()),6)

    def test_internal_resource_links_and_assets_resolve(self):
        with TestClient(app.app) as client:
            for path in ['/','/guides','/guide','/late-fee-policy','/guides/tuition-reminder-templates','/guides/handling-late-paying-parents','/tools/tuition-payment-tracker','/about']:
                r=client.get(path);self.assertEqual(r.status_code,200)
                self.assertEqual(len(re.findall(r'<h1[ >]',r.text)),1)
                for link in set(re.findall(r'(?:href|src)="(/[^"#?]*)',r.text)):
                    if link in app.PUBLIC_PAGES or link.startswith('/static/'):
                        with self.subTest(path=path,link=link):self.assertEqual(client.get(link).status_code,200)

    def test_guide_visits_tracked_without_private_query_values(self):
        with patch.object(store,'log_site_visit') as log:
            with TestClient(app.app) as client:
                r=client.get('/tools/tuition-payment-tracker?private=123',headers={'referer':'https://example.org/resources'})
                self.assertEqual(r.status_code,200);log.assert_called_once()
                self.assertEqual(log.call_args.args[1],'/tools/tuition-payment-tracker')
                self.assertEqual(log.call_args.args[2],'https://example.org/resources')

    def test_shared_policy_and_bilingual_messages_appear_on_pages(self):
        with TestClient(app.app) as client:
            policy=client.get('/late-fee-policy').text
            self.assertIn('October 7',policy);self.assertIn('October 8',policy)
            messages=client.get('/guides/tuition-reminder-templates').text
            self.assertIn('not yet verified',messages);self.assertIn('provider verified',messages)
            self.assertEqual(messages.count('<h3 lang="es">'),6)

if __name__=='__main__':unittest.main()
