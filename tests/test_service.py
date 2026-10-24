from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from aggregation import Rules, wrap_review
from config import Config
from service import State, create_app, digest


def review_event(identity="review-one", *, end=None):
    now = time.time()
    row = {"id": identity, "camera": "test-camera", "start_time": now - 100,
           "end_time": now - 10 if end is None else end, "severity": "alert",
           "data": {"objects": ["person", "plate"], "zones": ["test-seats", "test-table"], "detections": []}}
    return wrap_review({"type": "end", "before": row, "after": row}, "urn:test:frigate")


def turn(event):
    return {"conversation": "conversation-one", "stream": True, "input": [], "metadata": {},
            "event_context": {"schema_version": 1, "channel_id": "test-channel", "conversation_id": "conversation-one",
                              "event_ref": "test-reference", "activity": {
                                  "event_id": event["id"], "kind": {"type": "event", "ce": event}}}}


def binding(token):
    profile = {"schema_version": 1, "metadata": {"provider": "frigate_rules", "name": "Test rules",
               "owner": {"kind": "human", "subject": "synthetic-owner"}}, "model": {}, "permission": {}, "context": {}}
    return {"schema_version": 1, "profile": profile, "profile_digest": digest(profile), "agent_key": token, "tools": {}}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Config(Rules(camera="test-camera", seats_zone="test-seats", table_zone="test-table",
                                   merge_seconds=5, delivery_grace_seconds=2, minimum_seconds=20),
                             Path(self.directory.name), "p" * 48, "a" * 48, "urn:test:frigate", tick_seconds=1)
        self.client = TestClient(create_app(self.config))
        self.client.__enter__()
        self.provider = {"Authorization": "Bearer " + self.config.provider_token}
        self.operator = {"Authorization": "Bearer " + self.config.api_token}
        self.bind()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def bind(self):
        response = self.client.put("/api/v1/cmaeg/bindings/binding-one", json=binding(self.config.provider_token), headers=self.provider)
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.put("/api/v1/cmaeg/bindings/binding-one/conversations/conversation-one", json={}, headers=self.provider)
        self.assertEqual(response.status_code, 200, response.text)

    def post_turn(self, value, key="event-key"):
        return self.client.post("/api/v2/responses", json=value, headers={**self.provider, "Idempotency-Key": key})

    def test_health_is_public_and_metadata_requires_operator_auth(self):
        self.assertEqual(self.client.get("/healthz").json()["model_calls"], 0)
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/api/status", headers=self.provider).status_code, 401)
        self.assertEqual(self.client.get("/api/status", headers=self.operator).status_code, 200)

    def test_card_only_advertises_l1(self):
        self.assertEqual(self.client.get("/.well-known/agent-card.json").json()["capabilities"], {"streaming": True})

    def test_sse_receipt_is_replayed_without_another_turn(self):
        value = turn(review_event())
        first = self.post_turn(value)
        second = self.post_turn(value)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertIn("response.created", first.text)
        self.assertIn("response.completed", first.text)
        self.assertEqual(first.text, second.text)
        status = self.client.get("/api/status", headers=self.operator).json()
        self.assertEqual(status["turns"], 1)

    def test_context_survives_restart_and_response_identity_is_stable(self):
        value = turn(review_event())
        first = self.post_turn(value)
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(self.config))
        self.client.__enter__()
        self.assertEqual(first.text, self.post_turn(value).text)
        event_id = value["event_context"]["activity"]["event_id"]
        path = f"/api/v1/cmaeg/bindings/binding-one/conversations/conversation-one/events/{event_id}"
        self.assertEqual(self.client.get(path, headers=self.provider).json(), value["event_context"])

    def test_concurrent_duplicate_admission_is_atomic(self):
        value = turn(review_event())
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.post_turn(value), range(6)))
        self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual(len({r.text for r in results}), 1)
        self.assertEqual(self.client.get("/api/status", headers=self.operator).json()["turns"], 1)

    def test_changed_request_under_same_id_is_rejected(self):
        value = turn(review_event())
        self.assertEqual(self.post_turn(value).status_code, 200)
        changed = deepcopy(value)
        changed["event_context"]["activity"]["kind"]["ce"]["data"]["after"]["data"]["objects"].append("cup")
        self.assertEqual(self.post_turn(changed).status_code, 409)

    def test_context_and_admission_roll_back_together(self):
        value = turn(review_event())
        value["event_context"]["activity"]["kind"]["ce"]["data"]["after"]["end_time"] = -1
        self.assertEqual(self.post_turn(value).status_code, 400)
        state = self.client.app.state.worker
        engine = state.engines["binding-one"]
        self.assertEqual(engine.db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
        self.assertEqual(engine.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 0)

    def test_owner_and_profile_cannot_be_changed_by_rebind(self):
        value = binding(self.config.provider_token)
        value["profile"]["metadata"]["owner"]["subject"] = "different-synthetic-owner"
        value["profile_digest"] = digest(value["profile"])
        response = self.client.put("/api/v1/cmaeg/bindings/binding-one", json=value, headers=self.provider)
        self.assertEqual(response.status_code, 403)

    def test_revocation_denies_old_context_and_replay(self):
        value = turn(review_event())
        self.assertEqual(self.post_turn(value).status_code, 200)
        response = self.client.delete("/api/v1/cmaeg/bindings/binding-one", headers=self.provider)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.post_turn(value).status_code, 404)
        self.assertEqual(self.client.put("/api/v1/cmaeg/bindings/binding-one", json=binding(self.config.provider_token), headers=self.provider).status_code, 409)

    def test_heartbeat_required_before_automatic_finalization(self):
        value = turn(review_event())
        self.post_turn(value)
        state = self.client.app.state.worker
        now = time.time()
        with patch("service.time.time", return_value=now + 3):
            state.flush()
        self.assertEqual(self.client.get("/api/bindings/binding-one/decisions", headers=self.operator).json()["decisions"], [])
        heartbeat = {"specversion": "1.0", "source": self.config.event_source, "id": "heartbeat-one",
                     "type": "frigate.bridge.heartbeat", "subject": "test-camera", "data": {"healthy": True, "resynced": True}}
        self.assertEqual(self.post_turn(turn(heartbeat), "heartbeat-key").status_code, 200)
        with patch("service.time.time", return_value=now + 3):
            state.flush()
        result = self.client.get("/api/bindings/binding-one/decisions", headers=self.operator).json()["decisions"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["decision"], "candidate")
        self.assertEqual(result[0]["images"], [])

    def test_expired_or_unresynced_heartbeat_blocks_finalization(self):
        self.post_turn(turn(review_event()))
        heartbeat = {"specversion": "1.0", "source": self.config.event_source, "id": "heartbeat-one",
                     "type": "frigate.bridge.heartbeat", "subject": "test-camera", "data": {"healthy": True, "resynced": False}}
        self.post_turn(turn(heartbeat), "heartbeat-key")
        state = self.client.app.state.worker
        with patch("service.time.time", return_value=time.time() + 3):
            self.assertEqual(state.flush(), 0)
        heartbeat["id"] = "heartbeat-two"
        heartbeat["data"]["resynced"] = True
        self.post_turn(turn(heartbeat), "heartbeat-key-two")
        with patch("service.time.time", return_value=time.time() + 1000):
            self.assertEqual(state.flush(), 0)

    def test_metadata_only_and_bounded_body(self):
        value = turn(review_event())
        value["event_context"]["activity"]["kind"]["attachments"] = [{"kind": "image"}]
        self.assertEqual(self.post_turn(value).status_code, 400)
        response = self.client.post("/api/v2/responses", content=b"x" * 262145, headers=self.provider)
        self.assertEqual(response.status_code, 413)

    def test_mongodb_snapshot_restores_binding_after_local_files_are_lost(self):
        snapshots = {}

        class Collection:
            def find(self, *_args):
                return list(snapshots.values())

            def replace_one(self, _query, document, upsert=False):
                if not upsert:
                    raise AssertionError("snapshot writes must upsert")
                snapshots[document["_id"]] = document

        class Database:
            def __getitem__(self, _name):
                return Collection()

        class Admin:
            def command(self, _name):
                return {"ok": 1}

        class Client:
            def __init__(self, *_args, **_kwargs):
                self.admin = Admin()

            def __getitem__(self, _name):
                return Database()

            def close(self):
                pass

        profile = binding(self.config.provider_token)["profile"]
        body = {"schema_version": 1, "profile": profile, "profile_digest": digest(profile),
                "agent_key": self.config.provider_token, "tools": {}}
        with patch("service.MongoClient", Client):
            config = replace(self.config, mongo_uri="mongodb://synthetic")
            state = State(config)
            state.bind("snapshot-binding", body)
            state.close()
            for path in Path(self.directory.name).glob("*.sqlite3*"):
                path.unlink()
            restored = State(config)
            try:
                self.assertIn("snapshot-binding", restored.engines)
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
