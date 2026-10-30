from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from aggregation import wrap_review
from config import Config
from pipeline import Pipeline
from service import create_app


def policy(subject='test-camera', *, source='urn:test:events'):
    return {'version': 1, 'name': 'activity-rule', 'source': {'adapter': 'cloudevents', 'source': source,
            'subject': subject, 'types': ['example.motion'], 'health': {'mode': 'observed_events'}},
            'window': {'merge_seconds': 1, 'settle_seconds': 1, 'maximum_seconds': 100},
            'limits': {'cooldown_seconds': 1, 'max_per_day': 10}}


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Config(Pipeline.parse(policy('unmatched')), Path(self.directory.name),
                             'p' * 48, 'a' * 48, 'urn:test:events', relay_token='r' * 48,
                             evidence_token='w' * 48, tick_seconds=60)
        self.client = TestClient(create_app(self.config))
        self.client.__enter__()
        self.owner = {'Authorization': 'Bearer ' + self.config.api_token}
        self.relay = {'Authorization': 'Bearer ' + self.config.relay_token}
        self.worker_auth = {'Authorization': 'Bearer ' + self.config.evidence_token}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def restart(self, config=None, **kwargs):
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(config or self.config), **kwargs)
        self.client.__enter__()

    def create(self, identity='camera-one', **changes):
        value = {'id': identity, 'pipeline': policy(), **changes}
        response = self.client.post('/api/rules', headers=self.owner, json=value)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def update(self, doc, **changes):
        response = self.client.patch('/api/rules/' + doc['id'],
                                     headers={**self.owner, 'If-Match': str(doc['revision'])}, json=changes)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ingest(self, doc, identity='event-one', at=None):
        at = at if at is not None else doc['created_at'] + 0.01
        return self.client.post('/ingest', headers=self.relay, json={'specversion': '1.0', 'id': identity,
             'source': 'urn:test:events', 'subject': 'test-camera', 'type': 'example.motion', 'time': at, 'data': {}})

    def flush(self, doc):
        with patch('service.time.time', return_value=doc['created_at'] + 5):
            self.client.app.state.monitor.flush()

    def test_import_once_preserves_effective_evidence_and_original_state(self):
        initial = self.client.get('/api/rules', headers=self.owner).json()['rules'][0]
        self.assertEqual(initial['id'], 'default')
        self.assertTrue(initial['legacy'])
        worker = self.client.app.state.worker
        worker._relay_engine()
        self.restart(replace(self.config, rules=Pipeline.parse(policy('changed-startup-option'))))
        current = self.client.get('/api/rules/default', headers=self.owner).json()
        self.assertEqual(initial, current)
        self.assertIn('__relay_ingest__', self.client.app.state.worker.engines)

    def test_create_edit_and_restart_are_durable_and_no_credentials_in_rule(self):
        doc = self.create()
        self.assertFalse(doc['enabled'])
        self.assertFalse(doc['delivery_enabled'])
        updated = self.update(doc, label='门口活动', enabled=True)
        self.restart()
        response = self.client.get('/api/rules/camera-one', headers=self.owner)
        self.assertEqual(response.json(), updated)
        self.assertEqual(response.headers['etag'], '"2"')
        self.assertEqual(len(self.client.get('/api/rules/camera-one/revisions', headers=self.owner).json()['revisions']), 2)
        self.assertNotIn(self.config.api_token, response.text)
        self.assertEqual((Path(self.directory.name) / 'monitoring.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_invalid_policy_rolls_back_and_path_ids_are_rejected(self):
        doc = self.create()
        for identity in ('../escape', 'default', 'bad space'):
            self.assertEqual(self.client.post('/api/rules', headers=self.owner,
                             json={'id': identity, 'pipeline': policy()}).status_code, 400)
        for changes in ({'pipeline': {'version': 999}}, {'enabled': 'true'}, {'api_token': 'replacement'},
                        {'pipeline': {**policy(), 'evidence': {'adapter': 'frigate'}}}):
            response = self.client.patch('/api/rules/camera-one', headers={**self.owner, 'If-Match': '1'}, json=changes)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.get('/api/rules/camera-one', headers=self.owner).json(), doc)

    def test_optimistic_revision_is_atomic_under_concurrent_edits(self):
        self.create()
        def write(index):
            return self.client.patch('/api/rules/camera-one', headers={**self.owner, 'If-Match': '1'}, json={'label': str(index)})
        with ThreadPoolExecutor(max_workers=5) as pool:
            codes = [r.status_code for r in pool.map(write, range(5))]
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(409), 4)
        self.assertEqual(self.client.patch('/api/rules/camera-one', headers=self.owner, json={'enabled': True}).status_code, 428)

    def test_source_and_subject_fanout_and_rule_identity_isolation(self):
        first = self.create(enabled=True)
        second = self.create('camera-two', enabled=True)
        response = self.ingest(second)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()['rules']), {'camera-one', 'camera-two'})
        self.flush(first)
        identities = []
        for identity in ('camera-one', 'camera-two'):
            rows = self.client.get(f'/api/rules/{identity}/revisions/1/decisions', headers=self.owner).json()['decisions']
            self.assertEqual(len(rows), 1)
            identities.append(rows[0]['candidate']['candidate_id'])
        self.assertNotEqual(*identities)
        self.assertEqual(self.client.get('/api/rules/default/revisions/1/decisions', headers=self.owner).json()['decisions'], [])
        self.assertEqual(self.ingest(second).status_code, 200)

    def test_disabled_archived_and_new_revisions_do_not_replay_old_events(self):
        doc = self.create()
        self.assertEqual(self.ingest(doc).json()['accepted'], 'ignored')
        doc = self.update(doc, enabled=True)
        self.assertEqual(self.ingest(doc, at=doc['created_at'] - 10).json()['accepted'], 'before_revision')
        self.ingest(doc, 'fresh')
        self.flush(doc)
        doc = self.update(doc, enabled=False)
        self.assertEqual(self.ingest(doc, 'disabled').json()['accepted'], 'ignored')
        self.restart()
        history = self.client.get('/api/rules/camera-one/revisions/2/decisions', headers=self.owner).json()['decisions']
        self.assertEqual(len(history), 1)
        response = self.client.delete('/api/rules/camera-one', headers={**self.owner, 'If-Match': '3'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['archived'])
        self.assertEqual(self.client.patch('/api/rules/camera-one', headers={**self.owner, 'If-Match': '4'}, json={'enabled': True}).status_code, 409)
        self.assertEqual(self.client.get('/api/rules/camera-one/revisions/2/decisions', headers=self.owner).json()['decisions'], history)

    def test_old_callback_and_result_resolve_after_policy_edit_and_restart(self):
        doc = self.create(enabled=True)
        self.ingest(doc)
        self.flush(doc)
        record = self.client.get('/api/rules/camera-one/revisions/1/decisions', headers=self.owner).json()['decisions'][0]
        candidate = record['candidate']['candidate_id']
        self.update(doc, pipeline=policy('another-camera'))
        self.restart()
        response = self.client.post('/rules/camera-one/revisions/1/results/' + candidate,
                                    headers=self.worker_auth, json={'status': 'processed', 'reference': 'local:test'})
        self.assertEqual(response.status_code, 200, response.text)
        result = self.client.get('/api/rules/camera-one/revisions/1/results/' + candidate, headers=self.owner)
        self.assertEqual(result.json()['result']['reference'], 'local:test')
        self.assertEqual(self.client.get('/api/rules/camera-one/revisions/2/results/' + candidate, headers=self.owner).status_code, 404)

    def test_signed_event_uses_namespaced_paths_and_retired_outbox_is_paused(self):
        self.restart(replace(self.config, cma_event_url='https://cma.example/events', cma_hmac_key_id='test',
                             cma_hmac_secret='x' * 32, evidence_service='test-service'))
        doc = self.create(enabled=True, delivery_enabled=True)
        self.ingest(doc)
        with patch('service.urlopen', side_effect=TimeoutError()):
            self.flush(doc)
        state = self.client.app.state.monitor.worker('camera-one', 1)
        row = state._relay_engine().db.execute('SELECT body FROM cma_deliveries').fetchone()
        event = json.loads(row[0])
        self.assertEqual(event['data']['result_callback']['path'], 'rules/camera-one/revisions/1/results/' + event['data']['candidate_id'])
        self.assertEqual(event['data']['monitoring_rule'], {'id': 'camera-one', 'revision': 1})
        self.update(doc, enabled=False)
        with patch('service.urlopen') as send:
            self.flush(doc)
            send.assert_not_called()
        self.assertEqual(self.client.get('/api/status', headers=self.owner).json()['cma_deliveries']['pending'], 1)

    def test_global_bridge_heartbeat_reaches_each_camera(self):
        for identity, camera in [('cam-a', 'camera-a'), ('cam-b', 'camera-b')]:
            p = {'version': 1, 'name': identity, 'source': {'adapter': 'frigate_reviews', 'source': 'urn:test:frigate', 'subject': camera}}
            self.create(identity, pipeline=p, enabled=True)
        response = self.client.post('/ingest', headers=self.relay, json={'specversion': '1.0', 'source': 'urn:test:frigate',
            'id': 'heartbeat', 'type': 'frigate.bridge.heartbeat', 'subject': '*', 'data': {'healthy': True, 'resynced': True}})
        self.assertEqual(response.json()['rules'], {'cam-a': 'heartbeat_recorded', 'cam-b': 'heartbeat_recorded'})

    def test_management_rejects_worker_relay_and_spoofed_ingress_headers(self):
        for headers in ({}, self.worker_auth, self.relay, {'Authorization': 'Bearer ' + self.config.provider_token},
                        {'X-Ingress-Path': '/api/hassio_ingress/forged', 'X-Remote-User-Id': 'forged'}):
            for path in ('/api/rules', '/api/management', '/api/cameras'):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 401)
        self.restart(replace(self.config, ha_ingress=True))
        self.assertEqual(self.client.get('/api/rules', headers={'X-Ingress-Path': '/api/hassio_ingress/forged',
                        'X-Remote-User-Id': 'forged'}).status_code, 401)

    def test_trusted_ingress_requires_user_and_csrf_header_and_cannot_act_as_worker(self):
        self.restart(replace(self.config, ha_ingress=True), client=('172.30.32.2', 50000))
        headers = {'X-Ingress-Path': '/api/hassio_ingress/session', 'X-Remote-User-Id': 'test-ha-user'}
        self.assertEqual(self.client.get('/api/rules', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/api/rules', headers={'X-Ingress-Path': '/api/hassio_ingress/session'}).status_code, 401)
        value = {'id': 'cam', 'pipeline': policy()}
        self.assertEqual(self.client.post('/api/rules', headers=headers, json=value).status_code, 403)
        self.assertEqual(self.client.post('/api/rules', headers={**headers, 'X-Monitoring-Request': '1'}, json=value).status_code, 201)
        self.assertEqual(self.client.post('/ingest', headers=headers, json={}).status_code, 401)
        self.assertEqual(self.client.post('/results/example', headers=headers, json={}).status_code, 401)

    def test_ui_assets_ship_with_no_secrets_and_strict_csp(self):
        for path in ('/', '/ui/app.js', '/ui/style.css'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(self.config.api_token, response.text)
        self.assertIn("script-src 'self'", self.client.get('/').headers['content-security-policy'])
        self.assertEqual(self.client.get('/ui/monitoring.py').status_code, 404)
        self.assertNotIn('localStorage', self.client.get('/ui/app.js').text)

    def test_camera_inventory_never_returns_source_credentials(self):
        self.restart(replace(self.config, frigate_proxy_url='http://frigate:5000', frigate_direct=True))
        with patch('evidence.FrigateEvidence.get', return_value=json.dumps({'cameras': {'example': {
                'ffmpeg': {'inputs': [{'path': 'rtsp://private-password'}]},
                'zones': {'entrance': {}}, 'objects': {'track': ['person']}, 'record': {'enabled': True}}}}).encode()):
            response = self.client.get('/api/cameras', headers=self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('private-password', response.text)
        self.assertEqual(response.json()['cameras'][0]['zones'], ['entrance'])

    def test_projected_subject_is_used_for_generic_dispatch(self):
        p = policy()
        p['source']['fields'] = {'subject': 'data.entity'}
        doc = self.create(enabled=True, pipeline=p)
        response = self.client.post('/ingest', headers=self.relay, json={'specversion': '1.0', 'id': 'projected',
            'source': 'urn:test:events', 'type': 'example.motion', 'time': doc['created_at'] + 0.01,
            'subject': 'other-envelope-subject', 'data': {'entity': 'test-camera'}})
        self.assertEqual(response.json()['accepted'], 'stored')
        self.flush(doc)
        rows = self.client.get('/api/rules/camera-one/revisions/1/decisions', headers=self.owner).json()['decisions']
        self.assertEqual(rows[0]['candidate']['subject'], 'test-camera')

    def test_no_image_rule_still_repairs_frigate_history_before_finalizing(self):
        self.restart(replace(self.config, frigate_proxy_url='http://frigate:5000', frigate_direct=True))
        p = {'version': 1, 'name': 'no-images', 'source': {'adapter': 'frigate_reviews',
             'source': 'urn:test:events', 'subject': 'test-camera'},
             'window': {'merge_seconds': 1, 'settle_seconds': 1, 'maximum_seconds': 100}}
        doc = self.create(enabled=True, pipeline=p)
        row = {'id': 'review', 'camera': 'test-camera', 'start_time': doc['created_at'] + .01,
               'end_time': doc['created_at'] + .02, 'data': {}}
        self.client.post('/ingest', headers=self.relay, json=wrap_review({'type': 'end', 'before': row, 'after': row}, 'urn:test:events'))
        self.client.post('/ingest', headers=self.relay, json={'specversion': '1.0', 'id': 'hb', 'source': 'urn:test:events',
            'type': 'frigate.bridge.heartbeat', 'subject': '*', 'data': {'healthy': True, 'resynced': False}})
        state = self.client.app.state.monitor.worker(doc['id'], 1)
        self.assertIsNone(state.evidence)
        with patch.object(state.source_client, 'reviews', return_value=[row]) as repair:
            self.flush(doc)
            repair.assert_called_once()
        rows = self.client.get('/api/rules/camera-one/revisions/1/decisions', headers=self.owner).json()['decisions']
        self.assertEqual(len(rows), 1)

    def test_unknown_huge_revision_returns_404_and_partial_export_is_refused(self):
        self.create()
        response = self.client.get('/api/rules/camera-one/revisions/' + '9'*80 + '/status', headers=self.owner)
        self.assertEqual(response.status_code, 404)
        self.client.app.state.worker.config = replace(self.config, processing_enabled=False)
        response = self.client.get('/api/state/export', headers=self.owner)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'managed_state_requires_full_data_backup')

    def test_evidence_concurrency_and_cache_budget_are_shared_across_rules(self):
        self.restart(replace(self.config, frigate_proxy_url='http://frigate:5000', frigate_direct=True,
                             evidence_service='test-service'))
        started, release = threading.Event(), threading.Event()
        docs = []
        for identity in ('cam-a', 'cam-b'):
            p = {'version': 1, 'name': identity, 'source': {'adapter': 'frigate_reviews',
                 'source': 'urn:test:events', 'subject': identity}, 'evidence': {'adapter': 'frigate'}}
            doc = self.create(identity, pipeline=p, enabled=True)
            docs.append(doc)
            state = self.client.app.state.monitor.worker(identity, 1)
            engine = state._relay_engine()
            for n in range(5):
                engine.db.execute('INSERT INTO validations VALUES(?,?)', (f'candidate-{n}', json.dumps({
                    'candidate_id': f'candidate-{n}', 'subject': identity, 'started_at': 1, 'ended_at': 2})))
            engine.db.commit()
        first = self.client.app.state.monitor.worker('cam-a', 1)
        second = self.client.app.state.monitor.worker('cam-b', 1)
        def slow(candidate):
            started.set()
            if not release.wait(5):
                raise AssertionError('test extraction did not release')
            return {'candidate_id': candidate['candidate_id'], 'images': []}
        with patch.object(first.evidence, 'composite', side_effect=slow):
            with ThreadPoolExecutor(max_workers=2) as pool:
                job = pool.submit(self.client.get, '/rules/cam-a/revisions/1/evidence/candidate-0', headers=self.worker_auth)
                self.assertTrue(started.wait(5))
                try:
                    busy = self.client.get('/rules/cam-b/revisions/1/evidence/candidate-0', headers=self.worker_auth)
                    self.assertEqual(busy.status_code, 202)
                finally:
                    release.set()
                self.assertEqual(job.result().status_code, 200)
        with patch.object(second.evidence, 'composite', side_effect=lambda c: {'candidate_id': c['candidate_id'], 'images': []}) as collect:
            for n in range(5):
                self.assertEqual(self.client.get(f'/rules/cam-b/revisions/1/evidence/candidate-{n}', headers=self.worker_auth).status_code, 200)
            self.assertEqual(self.client.get('/rules/cam-b/revisions/1/evidence/candidate-4', headers=self.worker_auth).status_code, 200)
            self.assertEqual(collect.call_count, 5)
        self.assertEqual(len(self.client.app.state.monitor.media_cache), 4)

    def test_one_rule_failure_does_not_stop_other_rules_or_the_next_tick(self):
        first = self.create('camera-a', enabled=True)
        second = self.create('camera-b', enabled=True)
        self.ingest(second)
        state = self.client.app.state.monitor.worker('camera-a', 1)
        with patch.object(state, 'flush', side_effect=OSError('private diagnostic')):
            self.flush(first)
        status = self.client.get('/api/status', headers=self.owner)
        self.assertEqual(status.json()['runtime_errors'], {'camera-a': {'revision': 1, 'type': 'OSError'}})
        self.assertNotIn('private diagnostic', status.text)
        self.assertEqual(len(self.client.get('/api/rules/camera-b/revisions/1/decisions', headers=self.owner).json()['decisions']), 1)
        self.flush(first)
        self.assertEqual(self.client.get('/api/status', headers=self.owner).json()['runtime_errors'], {})

    def test_revision_limit_does_not_prevent_stopping_an_active_rule(self):
        doc = self.create(enabled=True)
        self.client.app.state.monitor.MAX_REVISIONS = 2
        self.assertEqual(self.client.patch('/api/rules/camera-one', headers={**self.owner, 'If-Match': '1'}, json={'label': 'over limit'}).status_code, 409)
        doc = self.update(doc, enabled=False)
        self.assertFalse(doc['enabled'])
        response = self.client.delete('/api/rules/camera-one', headers={**self.owner, 'If-Match': '2'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['archived'])

    def test_body_precondition_preserves_atomic_updates_when_proxy_strips_headers(self):
        self.create()
        def write(index):
            return self.client.patch('/api/rules/camera-one', headers=self.owner,
                                     json={'expected_revision': 1, 'label': str(index)})
        with ThreadPoolExecutor(max_workers=5) as pool:
            codes = [r.status_code for r in pool.map(write, range(5))]
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(409), 4)
        response = self.client.request('DELETE', '/api/rules/camera-one', headers=self.owner,
                                       json={'expected_revision': 2})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['archived'])

    def test_invalid_or_conflicting_preconditions_cannot_update_or_archive(self):
        original = self.create()
        for value in (True, '1', 0, -1):
            response = self.client.patch('/api/rules/camera-one', headers=self.owner,
                                         json={'expected_revision': value, 'enabled': True})
            self.assertEqual(response.status_code, 400)
        response = self.client.patch('/api/rules/camera-one', headers={**self.owner, 'If-Match': '1'},
                                     json={'expected_revision': 2, 'enabled': True})
        self.assertEqual(response.status_code, 400)
        response = self.client.request('DELETE', '/api/rules/camera-one', headers=self.owner,
                                       json={'expected_revision': 1, 'enabled': True})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/api/rules/camera-one', headers=self.owner).json(), original)
