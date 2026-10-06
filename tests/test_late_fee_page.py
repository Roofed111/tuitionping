"""Public resource and analytics checks; no external services are called."""
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

# Keep database side effects outside the repository and away from any live DB.
os.environ.pop('DATABASE_URL', None)
os.environ['DEMO_MODE'] = '1'
import store
_test_dir = tempfile.TemporaryDirectory()
store.DB_PATH = os.path.join(_test_dir.name, 'test.db')
import app
from fastapi.testclient import TestClient

class CalculatorPageTest(unittest.TestCase):
    def test_public_page_metadata_and_assets(self):
        with TestClient(app.app) as client:
            response = client.get('/tools/late-fee-calculator')
            self.assertEqual(response.status_code, 200)
            self.assertIn('Daycare Late Fee Calculator:', response.text)
            self.assertIn('https://www.tuitionping.com/tools/late-fee-calculator', response.text)
            schema = json.loads(re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>',response.text,re.S).group(1))
            self.assertEqual(schema['@graph'][0]['@type'], 'WebApplication')
            for asset in ['late-fee-calculator.css','late-fee-math.js','late-fee-calculator.js']:
                self.assertEqual(client.get('/static/'+asset).status_code,200)
            self.assertIn('/tools/late-fee-calculator',client.get('/sitemap.xml').text)

    def test_expired_subscriber_can_use_free_tool(self):
        provider = {'id':1,'email':'test@example.invalid','suspended':False,'name':'Test'}
        with patch.object(store,'get_provider_by_session',return_value=provider), patch.object(store,'get_subscription',return_value={'status':'expired'}):
            with TestClient(app.app) as client:
                client.cookies.set(app.SESSION_COOKIE,'test-session')
                response=client.get('/tools/late-fee-calculator',follow_redirects=False)
                self.assertEqual(response.status_code,200)

    def test_tracks_referral_without_calculation_query(self):
        with patch.object(store,'log_site_visit') as log:
            with TestClient(app.app) as client:
                response=client.get('/tools/late-fee-calculator?tuition=1234',headers={'referer':'https://example.org/childcare-resources'})
                self.assertEqual(response.status_code,200)
                log.assert_called_once()
                self.assertEqual(log.call_args.args[1],'/tools/late-fee-calculator')
                self.assertEqual(log.call_args.args[2],'https://example.org/childcare-resources')

if __name__ == '__main__':
    unittest.main()
