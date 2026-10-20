"""Public reminder drafting and privacy-safe acquisition events. No texts sent."""
import re
import unittest
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import growth

PATH='/guides/tuition-reminder-templates'

class ReminderGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.client=TestClient(app.app,base_url='https://www.tuitionping.com',headers={'user-agent':'Mozilla/5.0 Test browser'})
        self.addCleanup(self.client.close)
        response=self.client.get(PATH+'?utm_source=seo-test&utm_medium=organic')
        self.token=re.search('name="tp-analytics-token" content="([a-f0-9]+)"',response.text).group(1)
        self.visitor=growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
    def post(self,event='reminder_generated',detail='before',path=PATH):
        return self.client.post('/analytics/event',json={'event':event,'detail':detail,'path':path},headers={'x-tp-analytics':self.token})
    def test_drafts_deduplicate_and_do_not_create_paid_or_trial_conversions(self):
        for _ in range(3):self.assertEqual(self.post().status_code,204)
        with store.db() as conn:
            rows=[dict(r) for r in conn.execute('SELECT * FROM growth_events WHERE visitor_id=?',(self.visitor,)).fetchall()]
            source=conn.execute('SELECT source,medium,landing_path FROM growth_visitors WHERE visitor_id=?',(self.visitor,)).fetchone()
        drafts=[r for r in rows if r['event']=='reminder_generated']
        self.assertEqual(len(drafts),1);self.assertEqual(drafts[0]['detail'],'before');self.assertIsNone(drafts[0]['provider_id'])
        self.assertFalse(any(r['event'] in ('signup','trial_started','paid_customer') for r in rows))
        self.assertEqual(tuple(source),('seo-test','organic',PATH))
    def test_wrong_paths_private_details_and_business_events_are_rejected(self):
        for path in ['/demo','/','/guides']:
            self.assertEqual(self.post(path=path).status_code,400)
        self.assertEqual(self.post(detail='Private daycare / $300 / 2026-10-23').status_code,400)
        self.assertEqual(self.post(event='paid_customer',detail='').status_code,400)
        self.assertEqual(self.post(detail='late').status_code,204)
    def test_crawlable_examples_downloads_and_assets_stay_public(self):
        response=self.client.get(PATH)
        self.assertEqual(response.status_code,200);self.assertNotIn('x-robots-tag',response.headers)
        self.assertIn('https://www.tuitionping.com'+PATH,response.text)
        self.assertIn('<noscript>',response.text);self.assertEqual(response.text.count('<h3 lang="es">'),6)
        self.assertEqual(len(re.findall('<h1[ >]',response.text)),1)
        for asset in ['/static/reminder-generator.js','/static/reminder-generator.css','/static/downloads/daycare-tuition-reminders.docx','/static/downloads/daycare-tuition-reminders.pdf']:
            self.assertEqual(self.client.get(asset).status_code,200)

if __name__=='__main__':unittest.main()
