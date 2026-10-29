import unittest
from copy import deepcopy
from cma_context import ContextCleanup


class ContextCleanupTests(unittest.TestCase):
    def setup_cleanup(self, target='profile', phase='per_event', status='ready', owned=True):
        cfg = {'cma_url':'https://example.invalid/cma',
               'agent_profile_ids':['agp_example123'], 'workspace_id':'wks_example123', 'grace_seconds':300}
        obj = ContextCleanup(cfg, 'synthetic-owner-credential-for-test-only')
        definition = {'provider':{'slug':'cma_codex','target':{'type':target},'agent_profile_ids':['agp_example123']},
                      'partition':{'key':phase},'profile':{}}
        self.deletes = []
        def call(base, path, method='GET'):
            if method == 'DELETE':self.deletes.append(path);return None
            if path.startswith('channel-records/'):return {'channel_id':'opaque_example', 'definition':definition}
            if path.startswith('channels/'):return {'items':[{'consumers':[{
                'agent_id':'agt_example123','state':'completed','completed_at':'1970-01-01T00:10:00Z'}]}]}
            if path.startswith('agents?'):return {'items':[{'id':'agt_example123','workspace_id':'wks_example123','profile':{'id':'agp_example123' if owned else 'agp_other12345'},'status':status,'updated_at':'1970-01-01T00:10:00Z'}]}
            if path.endswith('/channel-usage'):return {'bindings':[{'agent_profile_ids':['agp_example123'] if owned else 'another_binding'}]}
            return {'workspace':{'id':'wks_example123'},'profile':{'id':'agp_example123'},'status':'deleted' if self.deletes else status,'updated_at':'1970-01-01T00:10:00Z'}
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
