"""Public playback assets and isolated, consent-aware engagement measurement."""
import json
import re
import unittest
from fastapi.testclient import TestClient
from test_late_fee_page import app, store
import growth


class WalkthroughTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app, base_url='https://www.tuitionping.com', headers={'user-agent': 'Mozilla/5.0 Test browser'})
        self.addCleanup(self.client.close)

    def test_homepage_has_accessible_opt_in_video_and_complete_workflow(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        video = re.search(r'<video\b.*?</video>', page.text, re.S).group()
        self.assertIn('preload="none"', video)
        self.assertIn('controls playsinline', video)
        self.assertIn('width="1280" height="720"', video)
        self.assertNotIn('autoplay', video)
        self.assertIn('kind="captions"', video)
        self.assertIn('aria-describedby="walkthrough-caption"', video)
        for wording in ('Read the walkthrough', 'Confirm payment received', 'Reported paid', 'fictional data'):
            self.assertIn(wording, page.text)
        self.assertIn('href="#walkthrough"', page.text)
        self.assertIn('href="/demo"', page.text)

    def test_video_supports_range_requests_and_caption_poster_types(self):
        root = '/static/media/tuitionping-walkthrough'
        video = self.client.get(root + '.mp4', headers={'range': 'bytes=0-31'})
        self.assertEqual(video.status_code, 206)
        self.assertEqual(len(video.content), 32)
        self.assertIn('video/mp4', video.headers['content-type'])
        self.assertIn('bytes 0-31/', video.headers['content-range'])
        self.assertIn(b'ftyp', video.content)
        captions = self.client.get(root + '.vtt')
        self.assertEqual(captions.status_code, 200)
        self.assertIn('text/vtt', captions.headers['content-type'])
        self.assertTrue(captions.text.startswith('WEBVTT'))
        self.assertIn('00:43.000 --> 00:48.000', captions.text)
        self.assertIn('Confirm payment received', captions.text)
        poster = self.client.get(root + '-poster.webp')
        self.assertEqual(poster.status_code, 200)
        self.assertIn('image/webp', poster.headers['content-type'])

    def test_video_events_are_deduplicated_and_only_accepted_on_homepage(self):
        page = self.client.get('/')
        token = re.search('name="tp-analytics-token" content="([a-f0-9]+)"', page.text).group(1)
        def record(event, path='/', detail=''):
            return self.client.post('/analytics/event', json={'event': event, 'path': path, 'detail': detail}, headers={'x-tp-analytics': token})
        for event in ('video_started', 'video_completed'):
            self.assertEqual(record(event).status_code, 204)
            self.assertEqual(record(event).status_code, 204)
            self.assertEqual(record(event, '/demo').status_code, 400)
            self.assertEqual(record(event, detail='private content').status_code, 400)
        vid = growth.visitor_from_cookie(self.client.cookies.get(growth.COOKIE))
        with store.db() as conn:
            rows = conn.execute("SELECT event FROM growth_events WHERE visitor_id = ? AND event IN ('video_started', 'video_completed')", (vid,)).fetchall()
        self.assertEqual(len(rows), 2)
        labels = {r['event']: r['label'] for r in growth.report()['stages']}
        self.assertEqual(labels['video_started'], 'Walkthrough played')
        self.assertEqual(labels['video_completed'], 'Walkthrough completed')

    def test_privacy_optout_preserves_video_access_without_tracking(self):
        with TestClient(app.app, base_url='https://www.tuitionping.com', headers={'user-agent': 'Mozilla/5.0', 'dnt': '1'}) as client:
            response = client.get('/')
            self.assertIn('id="walkthrough-video"', response.text)
            self.assertNotIn('tp-analytics-token', response.text)
            self.assertNotIn(growth.COOKIE, response.cookies)


if __name__ == '__main__':
    unittest.main()
