from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from aggregation import Rules
from config import Config
from evidence import FrigateEvidence
from service import State, create_app
from state_transfer import restore_state
from tests.test_service import review_event


class StateTransferTests(unittest.TestCase):
    def test_paused_export_restore_preserves_receipts_results_and_pending_outbox(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            config = Config(Rules(camera="test-camera", seats_zone="test-seats", table_zone="test-table",
                                  merge_seconds=5, delivery_grace_seconds=2, minimum_seconds=20),
                            Path(source), "p"*48, "a"*48, "urn:test:frigate", evidence_token="w"*48,
                            relay_token="r"*48, cma_event_url="https://cma.example/events", cma_hmac_secret="s"*48)
            operator={"Authorization":"Bearer "+config.api_token}
            with TestClient(create_app(config)) as client:
                self.assertEqual(client.get('/api/state/export',headers=operator).status_code,409)
                state=client.app.state.worker;engine=state._relay_engine()
                event=review_event();now=time.time()
                engine.ingest(event,now-5)
                decision=engine.flush(now,source_healthy=True)[0]
                state._queue_cma_candidate(engine,decision,now)
                with engine.db:
                    engine.db.execute('INSERT INTO analysis_results VALUES(?,?)',
                                      (decision['candidate_id'],json.dumps({'status':'pending'})))
                state.config=replace(config,processing_enabled=False)
                self.assertEqual(client.get('/api/state/export').status_code,401)
                self.assertEqual(client.post('/ingest',headers={'Authorization':'Bearer '+config.relay_token},json=event).status_code,503)
                self.assertEqual(client.post('/api/validation',headers=operator).status_code,503)
                self.assertEqual(client.post('/results/'+decision['candidate_id'],headers={'Authorization':'Bearer '+config.evidence_token},json={'status':'pending'}).status_code,503)
                with patch('service.urlopen') as send:
                    self.assertEqual(state.flush(),0);send.assert_not_called()
                export=client.get('/api/state/export',headers=operator).json()
                self.assertEqual(restore_state(export,target,config.pipeline),1)
                with self.assertRaises(FileExistsError):restore_state(export,target,config.pipeline)
            moved=State(replace(config,data_dir=Path(target),processing_enabled=False))
            try:
                engine=moved._relay_engine()
                self.assertEqual(engine.ingest(event,now),'duplicate')
                self.assertEqual(moved.candidate(decision['candidate_id']),decision)
                self.assertEqual(engine.db.execute('SELECT count(*) FROM cma_deliveries WHERE delivered_at IS NULL').fetchone()[0],1)
                self.assertEqual(json.loads(engine.db.execute('SELECT payload FROM analysis_results').fetchone()[0]),{'status':'pending'})
                self.assertEqual(engine.flush(now,source_healthy=True),[])
            finally:moved.close()
            bad=deepcopy(export);bad['policy_digest']='sha256:invalid'
            with tempfile.TemporaryDirectory() as empty:
                with self.assertRaises(ValueError):restore_state(bad,empty,config.pipeline)
                self.assertEqual(list(Path(empty).glob('*.sqlite3')),[])
            bad=deepcopy(export);bad['databases'][0]['sha256']='invalid'
            with tempfile.TemporaryDirectory() as empty:
                with self.assertRaises(ValueError):restore_state(bad,empty,config.pipeline)
                self.assertEqual(list(Path(empty).glob('*.sqlite3')),[])

    def test_explicit_direct_frigate_omits_unused_bearer_and_disallows_redirect_traps(self):
        client=FrigateEvidence('http://example-frigate:5000','', 'example-camera',allow_http=True)
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):return False
            def read(self,*_):return b'{}'
        with patch.object(client.opener,'open',return_value=Response()) as request:
            self.assertEqual(client.get('api/config'),b'{}')
            self.assertNotIn('Authorization',dict(request.call_args.args[0].header_items()))
        with self.assertRaises(ValueError):FrigateEvidence('http://example-frigate:5000','','example-camera')
        for url in ('http://user:pass@example:5000','http://example:5000/?token=x'):
            with self.assertRaises(ValueError):FrigateEvidence(url,'','example-camera',allow_http=True)
