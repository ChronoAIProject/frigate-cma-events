from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import time
import unittest

from pipeline import Pipeline
from scenarios import catalog, render_template, validate_analysis
from examples.worker.render import render as render_worker
from tests import test_monitoring


class ScenarioTests(unittest.TestCase):
    setUp = test_monitoring.MonitoringTests.setUp
    tearDown = test_monitoring.MonitoringTests.tearDown
    restart = test_monitoring.MonitoringTests.restart

    def parameters(self, template):
        required = {'camera': 'example-camera', 'seats-zone': 'example-seats',
                    'table-zone': 'example-table', 'zone': 'example-gate', 'source': 'urn:test:events'}
        return {k: required[k] for k, spec in template['parameters'].items() if 'default' not in spec}

    def test_shared_worker_bootstrap_has_only_explicit_grants_and_no_site_workflow(self):
        settings = {'owner_id': 'example-owner', 'model_service': 'example-model',
                    'model': 'example-model-name', 'services': ['example-monitor', 'example-records']}
        channel = render_worker(settings)
        self.assertEqual(channel['profile']['permission']['service_scope'], settings['services'])
        self.assertEqual(channel['partition']['key'], 'per_event')
        self.assertIn('data.analysis', channel['profile']['context']['system_prompt'])
        self.assertNotIn('spreadsheet_id', json.dumps(channel))
        self.assertEqual(channel['credentials']['agent_key'], 'SET_DISTINCT_NYXID_CHANNEL_KEY')
        with self.assertRaises(ValueError):
            render_worker({**settings, 'api_key': 'not-supported'})

    def test_three_scenarios_and_independent_sites_share_the_same_runtime(self):
        templates = catalog()
        self.assertEqual({t['id'] for t in templates}, {'dining', 'driveway', 'ha-state'})
        event_types = set()
        for template in templates:
            before = deepcopy(template)
            params = self.parameters(template)
            pipeline = render_template(template, params)
            event_types.add(pipeline['output']['event_type'])
            self.assertEqual(pipeline['analysis']['template']['version'], template['version'])
            self.assertEqual(len(pipeline['analysis']['template']['sha256']), 64)
            if 'camera' in params:
                other = render_template(template, {**params, 'camera': 'another-site-camera'})
                self.assertNotEqual(pipeline['source']['subject'], other['source']['subject'])
                self.assertEqual(pipeline['analysis']['template'], other['analysis']['template'])
            self.assertEqual(template, before)
        self.assertEqual(len(event_types), 3)

    def test_external_template_import_needs_no_catalog_or_image_change(self):
        template = deepcopy(next(t for t in catalog() if t['id'] == 'ha-state'))
        template.update(id='third-party-lab', version='2.1.0', title='Independent lab workflow')
        template['pipeline']['analysis']['instructions'] = 'Summarize laboratory transitions; do not infer occupancy.'
        headers = self.owner
        before = self.client.get('/api/rules', headers=headers).json()
        self.assertEqual(self.client.get('/api/scenarios').status_code, 401)
        self.assertEqual(self.client.post('/api/scenarios/render', json={'template': template}).status_code, 401)
        response = self.client.post('/api/scenarios/render', headers=headers,
                                    json={'template': template, 'parameters': {'source': 'urn:test:events'}})
        self.assertEqual(response.status_code, 200, response.text)
        pipeline = response.json()['pipeline']
        self.assertEqual(pipeline['analysis']['template']['id'], 'third-party-lab')
        self.assertNotIn('third-party-lab', [t['id'] for t in self.client.get('/api/scenarios', headers=headers).json()['templates']])
        self.assertEqual(self.client.get('/api/rules', headers=headers).json(), before)
        created = self.client.post('/api/rules', headers=headers, json={'id': 'lab', 'pipeline': pipeline})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertFalse(created.json()['enabled'])
        self.assertFalse(created.json()['delivery_enabled'])
        self.restart()
        self.assertEqual(self.client.get('/api/rules/lab', headers=headers).json()['pipeline'], pipeline)

    def test_rejects_unsupported_schemas_secrets_types_and_undeclared_parameters(self):
        template = next(t for t in catalog() if t['id'] == 'ha-state')
        for changes, params in [({'schema': 'future.v2'}, {'source': 'urn:test:events'}),
                                ({}, {'source': True}), ({}, {}),
                                ({}, {'source': 'urn:test:events', 'unknown': 'x'}),
                                ({}, {'source': 'urn:test:events', 'workflow': {'token': 'do-not-store'}})]:
            with self.subTest(changes=changes, params=params), self.assertRaises(ValueError):
                render_template({**template, **changes}, params)
        for analysis in [{'version': 2, 'instructions': 'x'}, {'version': 1, 'instructions': ''},
                         {'version': 1, 'instructions': 'x', 'shell': 'not-supported'},
                         {'version': 1, 'instructions': 'x', 'context': {'Authorization': 'not-supported'}},
                         {'version': 1, 'instructions': 'x' * 16385}]:
            with self.subTest(analysis=analysis), self.assertRaises(ValueError):
                validate_analysis(analysis)
        response = self.client.post('/api/scenarios/render', headers=self.owner, json={'id': '../outside', 'parameters': {}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'invalid_scenario')

    def test_parameter_values_are_not_recursively_executed_or_expanded(self):
        template = next(t for t in catalog() if t['id'] == 'ha-state')
        context = {'text': '${UNEXPANDED}', 'literal': {'$param': 'source'}}
        pipeline = render_template(template, {'source': 'urn:test:events', 'workflow': context})
        self.assertEqual(pipeline['analysis']['context']['config'], context)
        broken = deepcopy(template)
        broken['pipeline']['source']['subject'] = {'$param': 'missing'}
        with self.assertRaises(ValueError):
            render_template(broken, {'source': 'urn:test:events'})

    def test_analysis_edit_is_hot_revision_owned_and_does_not_rewrite_queued_work(self):
        self.restart(replace(self.config, cma_event_url='https://cma.example/events'))
        pipeline = test_monitoring.policy()
        pipeline['analysis'] = {'version': 1, 'instructions': 'First criteria', 'context': {'site': 'example'}}
        original_digest = Pipeline.parse(pipeline).state_document()
        created = self.client.post('/api/rules', headers=self.owner,
            json={'id': 'hot-plan', 'enabled': True, 'delivery_enabled': True, 'pipeline': pipeline})
        self.assertEqual(created.status_code, 201, created.text)
        monitor = self.client.app.state.monitor
        old_worker = monitor.worker('hot-plan', 1)
        payload = {'candidate_id': 'example-old', 'subject': 'test-camera', 'decision': 'candidate'}
        old_engine = old_worker._relay_engine()
        old_worker._queue_cma_candidate(old_engine, payload, time.time())
        old_body = old_engine.db.execute('SELECT body FROM cma_deliveries').fetchone()[0]
        pipeline['analysis']['instructions'] = 'Updated criteria without a deployment'
        self.assertEqual(Pipeline.parse(pipeline).state_document(), original_digest)
        updated = self.client.patch('/api/rules/hot-plan', headers=self.owner,
            json={'expected_revision': 1, 'pipeline': pipeline})
        self.assertEqual(updated.status_code, 200, updated.text)
        new_worker = monitor.worker('hot-plan', 2)
        new_engine = new_worker._relay_engine()
        new_worker._queue_cma_candidate(new_engine, {**payload, 'candidate_id': 'example-new'}, time.time())
        new_body = new_engine.db.execute('SELECT body FROM cma_deliveries').fetchone()[0]
        self.assertEqual(json.loads(old_body)['data']['analysis']['instructions'], 'First criteria')
        self.assertEqual(json.loads(new_body)['data']['analysis']['instructions'], pipeline['analysis']['instructions'])
        self.assertEqual(json.loads(new_body)['data']['monitoring_rule']['revision'], 2)
        self.assertEqual(old_engine.db.execute('SELECT body FROM cma_deliveries').fetchone()[0], old_body)
        self.restart(replace(self.config, cma_event_url='https://cma.example/events'))
        self.assertEqual(self.client.get('/api/rules/hot-plan', headers=self.owner).json()['revision'], 2)
        self.assertEqual(self.client.app.state.monitor.worker('hot-plan', 1)._relay_engine().db.execute('SELECT body FROM cma_deliveries').fetchone()[0], old_body)
