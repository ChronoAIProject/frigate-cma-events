import unittest
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit
from cma_context import ContextCleanup


class ContextCleanupTests(unittest.TestCase):
    def setup_cleanup(self, target='profile', phase='per_event', status='ready', owned=True):
        cfg = {'cma_url':'https://example.invalid/cma',
               'agent_profile_ids':['agp_example123'], 'workspace_id':'wks_example123', 'grace_seconds':300}
        obj = ContextCleanup(cfg, 'synthetic-owner-credential-for-test-only')
        definition = {'provider':{'slug':'cma_codex','target':{'type':target},'agent_profile_ids':['agp_example123']},
                      'partition':{'key':phase},'profile':{}}
        self.deletes = []
        self.stopped = False
        def call(base, path, method='GET'):
            if method == 'POST':self.stopped=True;return None
            if method == 'DELETE':self.deletes.append(path);return None
            if path.startswith('channel-records/'):return {'channel_id':'opaque_example', 'definition':definition}
            if path.startswith('channels/'):return {'items':[{'consumers':[{
                'agent_id':'agt_example123','state':'completed','completed_at':'1970-01-01T00:10:00Z'}]}]}
            if path.startswith('agents?'):
                requested = parse_qs(urlsplit(path).query)['status'][0]
                return {'items':[] if requested != status else [{'id':'agt_example123','workspace_id':'wks_example123','profile':{'id':'agp_example123' if owned else 'agp_other12345'},'status':status,'updated_at':'1970-01-01T00:10:00Z'}]}
            if path.endswith('/channel-usage'):return {'bindings':[{'agent_profile_ids':['agp_example123'] if owned else 'another_binding'}]}
            return {'workspace':{'id':'wks_example123'},'profile':{'id':'agp_example123'},'status':'deleted' if self.deletes else ('stopped' if self.stopped else status),'updated_at':'1970-01-01T00:10:00Z'}
        obj.call = call
        return obj

    def test_only_completed_owned_idle_native_context_is_retired(self):
        obj = self.setup_cleanup()
        self.assertIsNone(obj.run(now=1000)['error'])
        self.assertEqual(self.deletes,['agents/agt_example123'])
        self.assertEqual(obj.status['retired'],1)
        obj.run(now=1001)
        self.assertEqual(len(self.deletes),1)

    def test_other_profiles_active_and_recent_contexts_are_preserved(self):
        for settings in ({'status':'working'},{'status':'waiting_approval'},{'owned':False}):
            with self.subTest(settings=settings):
                obj=self.setup_cleanup(**settings);obj.run(now=1000)
                self.assertFalse(self.deletes)
        obj=self.setup_cleanup();obj.run(now=700);self.assertFalse(self.deletes)

    def test_already_stopped_native_resource_can_finish_async_cleanup(self):
        obj=self.setup_cleanup(status='stopped')
        obj.run(now=700)
        self.assertEqual(self.deletes,['agents/agt_example123'])

    def test_deleted_history_is_excluded_from_inventory_queries(self):
        obj = self.setup_cleanup()
        original = obj.call
        requested = []
        def call(base, path, method='GET'):
            if path.startswith('agents?'):
                state = parse_qs(urlsplit(path).query).get('status', [None])[0]
                requested.append(state)
                if state is None:
                    # The old unfiltered scan filled with retained tombstones.
                    return {'items':[{'id':f'agt_deleted{i:08}', 'status':'deleted'}
                                     for i in range(50)], 'next_cursor':'more_history'}
            return original(base, path, method)
        obj.call = call
        self.assertIsNone(obj.run(now=1000)['error'])
        self.assertEqual(requested, ['ready', 'stopped', 'failed'])
        self.assertEqual(self.deletes, ['agents/agt_example123'])

    def test_full_filtered_page_is_drained_and_reports_more_work(self):
        obj = self.setup_cleanup()
        stopped, deleted = set(), set()
        def call(base, path, method='GET'):
            if path.startswith('agents?'):
                state = parse_qs(urlsplit(path).query)['status'][0]
                return {'items':[] if state != 'ready' else [
                    {'id':f'agt_example{i:08}', 'workspace_id':'wks_example123',
                     'profile':{'id':'agp_example123'}, 'status':'ready',
                     'updated_at':'1970-01-01T00:10:00Z'} for i in range(50)],
                    **({'next_cursor':'another_page'} if state == 'ready' else {})}
            aid = path.split('/')[1]
            if method == 'POST':stopped.add(aid);return None
            if method == 'DELETE':deleted.add(aid);return None
            return {'workspace':{'id':'wks_example123'}, 'profile':{'id':'agp_example123'},
                    'status':'deleted' if aid in deleted else 'stopped' if aid in stopped else 'ready',
                    'updated_at':'1970-01-01T00:10:00Z'}
        obj.call = call
        status = obj.run(now=1000)
        self.assertIsNone(status['error'])
        self.assertTrue(status['scan_limited'])
        self.assertEqual(len(deleted), 50)
        self.assertEqual(status['retired'], 50)
        self.assertEqual(status['pending'], 0)

    def test_response_failure_is_reported_without_credentials_or_body(self):
        obj=self.setup_cleanup()
        obj.call=lambda *args: (_ for _ in ()).throw(ValueError('private upstream message'))
        self.assertEqual(obj.run(now=1000)['error'],'ValueError')

    def test_urls_ids_and_retention_are_bounded(self):
        obj=self.setup_cleanup()
        for patch in ({'cma_url':'http://example.invalid'},{'cma_url':'https://user:private@example.invalid'},
                      {'workspace_id':'../other'},{'grace_seconds':0},{'agent_profile_ids':['../invalid']}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                ContextCleanup(obj.config | patch,obj.token)
