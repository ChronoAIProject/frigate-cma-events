from dataclasses import replace
from io import BytesIO
import json
import time
import unittest
from unittest.mock import patch

from PIL import Image

from evidence import EvidenceUnavailable, FrigateEvidence
from tests import test_service


class EvidenceTests(unittest.TestCase):
    setUp = test_service.ServiceTests.setUp
    tearDown = test_service.ServiceTests.tearDown
    bind = test_service.ServiceTests.bind
    def enable(self):
        state = self.client.app.state.worker
        state.config = replace(state.config, evidence_token="e" * 48, evidence_service="synthetic-evidence")
        state.evidence = FrigateEvidence("https://proxy.example/service", "private-key", "test-camera")
        self.evidence_auth = {"Authorization": "Bearer " + state.config.evidence_token}
        return state

    def test_only_stored_candidates_are_accessible_and_tokens_are_separate(self):
        self.enable()
        path = "/evidence/nonexistent"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers=self.operator).status_code, 401)
        self.assertEqual(self.client.get(path, headers=self.evidence_auth).status_code, 404)
        self.assertEqual(self.client.post("/api/validation", headers=self.evidence_auth).status_code, 401)

    def test_test_evidence_is_bounded_cached_and_cannot_be_reported_as_a_meal(self):
        state = self.enable()
        with patch.object(state.evidence, "reviews", return_value=[test_service.review_event()["data"]["after"]]):
            response = self.client.post("/api/validation", headers=self.operator)
        self.assertEqual(response.status_code, 200, response.text)
        meal_id = response.json()["meal_id"]
        blob = BytesIO()
        Image.new("RGB", (640, 360), "white").save(blob, format="JPEG")
        with patch.object(state.evidence, "get", return_value=blob.getvalue()) as get:
            first = self.client.get("/evidence/" + meal_id, headers=self.evidence_auth)
            again = self.client.get("/evidence/" + meal_id, headers=self.evidence_auth)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), again.json())
        self.assertEqual(len(first.json()["frame_timestamps"]), 1)
        self.assertEqual(get.call_count, 1)
        self.assertIn("/recordings/", get.call_args.args[0])
        self.assertNotIn("latest", get.call_args.args[0])
        self.assertEqual(self.client.post("/results/" + meal_id, headers=self.evidence_auth,
                                         json={"status": "recorded"}).status_code, 400)
        self.assertEqual(self.client.post("/results/" + meal_id, headers=self.evidence_auth,
                                         json={"status": "integration_test", "sheet_range": "synthetic!A2:J2"}).status_code, 200)
        self.assertEqual(self.client.get("/api/results/" + meal_id, headers=self.operator).json()["result"]["status"], "integration_test")
        self.assertEqual(state._relay_engine().db.execute("SELECT count(*) FROM reviews").fetchone()[0], 0)

    def test_missing_historical_frames_never_fall_back_to_live(self):
        client = FrigateEvidence("https://proxy.example/service", "private-key", "test-camera")
        candidate = {"meal_id": "meal-test", "camera": "test-camera", "started_at": 100, "ended_at": 200}
        with patch.object(client, "get", side_effect=EvidenceUnavailable("missing")) as get:
            with self.assertRaises(EvidenceUnavailable):
                client.composite(candidate)
        self.assertEqual(get.call_count, 3)
        self.assertTrue(all("/recordings/" in c.args[0] for c in get.call_args_list))

    def test_rest_repair_is_required_even_if_relay_claims_resynchronized(self):
        state = self.enable()
        event = test_service.review_event()
        state.ingest_relay(event)
        state.ingest_relay({"specversion": "1.0", "source": state.config.event_source, "id": "health-one",
                            "type": "frigate.bridge.heartbeat", "subject": "test-camera",
                            "data": {"healthy": True, "resynced": True}})
        engine = state._relay_engine()
        with engine.db:
            engine.db.execute("UPDATE reviews SET received=?", (time.time() - 10,))
        with patch.object(state.evidence, "reviews", side_effect=EvidenceUnavailable("offline")):
            self.assertEqual(state.flush(), 0)
        with patch.object(state.evidence, "reviews", return_value=[event["data"]["after"]]):
            self.assertEqual(state.flush(), 1)

    def test_proxy_urls_reject_redirect_credential_and_query_traps(self):
        for url in ("http://plain.example", "https://user:pass@proxy.example", "https://proxy.example/?x=1"):
            with self.assertRaises(ValueError):
                FrigateEvidence(url, "private-key", "synthetic-camera")
