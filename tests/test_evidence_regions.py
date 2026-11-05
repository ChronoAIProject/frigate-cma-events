"""Native region pixels, source identity and the shared evidence budget."""
import asyncio
import base64
from io import BytesIO
import json
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageStat
import httpx

from evidence import EvidenceOptions, EvidenceUnavailable, FrigateEvidence
from tests import test_evidence as base_tests


def fixture():
    options = EvidenceOptions(max_width=320, max_height=180, sample_frames=6,
                              selected_frames=3, detail_regions=8)
    client = FrigateEvidence('https://proxy.example/service', 'key', 'test-camera', options=options)
    source = Image.new('RGB', (1920, 1080), '#102030')
    draw = ImageDraw.Draw(source)
    draw.rectangle((652, 367, 806, 453), fill='#ed7144')
    for x in range(652, 807, 4):
        draw.line((x, 367, x, 453), fill='#ffeecc')
    encoded = BytesIO(); source.save(encoded, 'JPEG', quality=95)
    records = []
    with patch.object(client, 'get', return_value=encoded.getvalue()):
        for timestamp, phase in [(100, 'early'), (150, 'middle'), (200, 'late')]:
            records.append(client.snapshot_record({'timestamp': timestamp, 'phase': phase,
                'candidate_phase': phase, 'tracked_objects': 1, 'sampling': 'interval_coverage'},
                timestamp, [.1, .1, .9, .9], 5))
    candidate = {'candidate_id': 'native-detail-test', 'subject': 'test-camera',
                 'started_at': 100, 'ended_at': 200}
    pack = client.pack(candidate, records, records, [], 3, [.1, .1, .9, .9], [], [])
    return client, candidate, pack


class RegionTests(unittest.TestCase):
    def test_native_pixels_not_overview_enlargement_and_no_new_source_reads(self):
        client, candidate, pack = fixture()
        self.assertEqual(len(pack['images']), 1)
        self.assertEqual(pack['frames'][0]['source_size'], [1920, 1080])
        self.assertEqual(pack['frames'][0]['panel_size'], [320, 180])
        self.assertNotIn('_source_jpeg', json.dumps(pack))
        self.assertNotIn('sources', json.dumps(pack))
        request = {'regions': [{'frame_id': 'F01', 'box': [.3, .3, .4, .4]}]}
        with patch.object(client, 'get', side_effect=AssertionError('unexpected network read')):
            result = client.regions(pack, request)
        region = result['regions'][0]
        self.assertEqual(region['source_crop_pixels'], [652, 367, 807, 454])
        self.assertEqual(region['native_crop_size'], [155, 87])
        self.assertEqual(region['panel_size'], [155, 87])
        # That same region would have only ~32x18 pixels in the overview panel.
        self.assertGreater(region['panel_size'][0], 4 * .1 * 320)
        self.assertEqual(region['frame_id'], 'F01')
        self.assertEqual(region['timestamp'], 100)
        self.assertEqual(result['source_requests'], 0)
        self.assertEqual(region['page'], 2)
        image = Image.open(BytesIO(base64.b64decode(result['images'][0]['image_base64'])))
        pixels = image.crop((0, 28, 155, 28 + 87))
        self.assertGreater(ImageStat.Stat(pixels).stddev[1], 25)
        self.assertEqual(pack['evidence_budget']['max_images'], 3)

    def test_one_batch_is_idempotent_and_eight_regions_fit_remaining_two_images(self):
        client, candidate, pack = fixture()
        request = {'regions': [{'frame_id': 'F01', 'box': [.01 + i * .1, .2, .09 + i * .1, .3]}
                               for i in range(8)]}
        first = client.regions(pack, request)
        self.assertIs(client.regions(pack, json.loads(json.dumps(request))), first)
        self.assertEqual(len(first['regions']), 8)
        self.assertEqual(len(first['images']), 2)
        self.assertEqual(len(pack['images']) + len(first['images']), pack['evidence_budget']['max_images'])
        request['regions'].pop()
        with self.assertRaisesRegex(EvidenceUnavailable, 'already_committed'):
            client.regions(pack, request)

    def test_rejects_unbounded_unknown_duplicate_and_invalid_coordinates(self):
        client, candidate, pack = fixture()
        good = {'frame_id': 'F01', 'box': [.3, .3, .4, .4]}
        requests = [{}, {'regions': []}, {'regions': [good] * 9}, {'regions': [good, good]},
                    {'regions': [{**good, 'frame_id': 'F99'}]},
                    {'regions': [good], 'timestamp': 300},
                    {'regions': [{**good, 'url': 'https://example.com'}]}]
        for box in ([0, 0, 1, 1], [-.1, .2, .3, .4], [.4, .2, .3, .4],
                    [.2, .2, .2, .4], [.2, .2, True, .4], [.2, .2, float('nan'), .4],
                    [.2, .2, float('inf'), .4], [.2, .2, .20001, .20001]):
            requests.append({'regions': [{**good, 'box': box}]})
        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                client.regions(pack, request)
        self.assertIsNone(pack.detail_request)
        self.assertEqual(len(client.regions(pack, {'regions': [good]})['regions']), 1)

    def test_synthetic_budget_stays_one_image_and_never_exposes_regions(self):
        client, candidate, pack = fixture()
        sources = list(pack.sources.values())
        records = []
        for raw, frame in sources[:1]:
            with patch.object(client, 'get', return_value=raw):
                records.append(client.snapshot_record({'timestamp': 100, 'phase': 'validation'},
                               100, [.1, .1, .9, .9], 5))
        result = client.pack({**candidate, 'test': True}, records, records, [], 1,
                             [.1, .1, .9, .9], [], [])
        self.assertEqual(result['evidence_budget']['max_images'], 1)
        self.assertNotIn('region_views', result)
        with self.assertRaisesRegex(EvidenceUnavailable, 'disabled'):
            client.regions(result, {'regions': [{'frame_id': 'F01', 'box': [.3, .3, .4, .4]}]})

    def test_detail_options_and_broad_sampling_stay_bounded(self):
        from evidence import sample_frames
        for value in [-1, 9, True, 1.2]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                EvidenceOptions(detail_regions=value)
        options = EvidenceOptions(detail_regions=8, sample_frames=48, selected_frames=12)
        tracks = [{'start': 900, 'end': 1500, 'snapshot': 1200, 'score': .9}]
        planned = sample_frames(0, 3600, tracks, options)
        self.assertEqual(len(planned), 48)
        self.assertIn(1, [r['timestamp'] for r in planned[:9]])
        self.assertEqual(options.budget()['max_images'], 3)


class RegionServiceTests(unittest.TestCase):
    setUp = base_tests.EvidenceTests.setUp
    tearDown = base_tests.EvidenceTests.tearDown
    bind = base_tests.EvidenceTests.bind
    enable = base_tests.EvidenceTests.enable

    def test_crop_work_keeps_event_loop_responsive_and_serializes_media(self):
        state = self.enable()
        client, candidate, pack = fixture()
        state.evidence = client
        engine = state._relay_engine()
        with engine.db:
            engine.db.execute('INSERT INTO validations VALUES(?,?)',
                              (candidate['candidate_id'], json.dumps(candidate)))
        self.client.app.state.monitor.media_cache[('default', 1, candidate['candidate_id'])] = pack
        path = '/evidence/' + candidate['candidate_id']
        request = {'regions': [{'frame_id': 'F01', 'box': [.3, .3, .4, .4]}]}
        entered, release = threading.Event(), threading.Event()
        original = client.regions

        def slow_regions(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('event loop blocked during crop processing')
            return original(*args)

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.client.app),
                                         base_url='http://testserver') as http:
                first = asyncio.create_task(http.post(path, headers=self.evidence_auth, json=request))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    health = await asyncio.wait_for(http.get('/healthz'), 1)
                    self.assertEqual(health.status_code, 200)
                    second = await asyncio.wait_for(http.post(path, headers=self.evidence_auth, json=request), 1)
                    self.assertEqual(second.status_code, 202)
                    self.assertFalse(first.done())
                finally:
                    release.set()
                    response = await first
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(self.client.app.state.monitor.media_busy)

        with patch.object(client, 'regions', side_effect=slow_regions):
            asyncio.run(exercise())

    def test_exact_evidence_endpoint_auth_cache_expiry_and_immutable_batch(self):
        state = self.enable()
        client, candidate, pack = fixture()
        state.evidence = client
        engine = state._relay_engine()
        with engine.db:
            engine.db.execute('INSERT INTO validations VALUES(?,?)',
                              (candidate['candidate_id'], json.dumps(candidate)))
        path = '/evidence/' + candidate['candidate_id']
        request = {'regions': [{'frame_id': 'F01', 'box': [.3, .3, .4, .4]}]}
        self.assertEqual(self.client.post(path, json=request).status_code, 401)
        self.assertEqual(self.client.post(path, headers=self.operator, json=request).status_code, 401)
        self.assertEqual(self.client.post(path, headers=self.evidence_auth, json=request).status_code, 410)
        self.assertEqual(self.client.post('/evidence/unknown', headers=self.evidence_auth, json=request).status_code, 404)
        with patch.object(client, 'composite', return_value=pack) as extract:
            overview = self.client.get(path, headers=self.evidence_auth)
            self.assertEqual(overview.status_code, 200)
            self.assertNotIn('sources', overview.json())
            first = self.client.post(path, headers=self.evidence_auth, json=request)
            again = self.client.post(path, headers=self.evidence_auth, json=request)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json(), again.json())
            changed = {'regions': [{'frame_id': 'F01', 'box': [.2, .2, .3, .3]}]}
            self.assertEqual(self.client.post(path, headers=self.evidence_auth, json=changed).status_code, 409)
            self.assertEqual(extract.call_count, 1)
        self.client.app.state.monitor.media_cache.clear()
        self.assertEqual(self.client.post(path, headers=self.evidence_auth, json=request).status_code, 410)
        self.assertEqual(engine.db.execute('SELECT count(*) FROM analysis_results').fetchone()[0], 0)
