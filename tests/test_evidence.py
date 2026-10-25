from dataclasses import replace
from io import BytesIO
import json
import time
import unittest
from unittest.mock import patch

from PIL import Image

from evidence import EvidenceOptions, EvidenceUnavailable, FrigateEvidence, select_frames
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
        with patch.object(client, "tableware_events", return_value=[]), \
                patch.object(client, "get", side_effect=EvidenceUnavailable("missing")) as get:
            with self.assertRaises(EvidenceUnavailable):
                client.composite(candidate)
        self.assertEqual(get.call_count, 3)
        self.assertTrue(all("/recordings/" in c.args[0] for c in get.call_args_list))

    def test_selects_tableware_phases_before_clearing_not_three_end_frames(self):
        events = [{"start": a, "end": b, "snapshot": t, "score": s} for a, b, t, s in
                  [(120, 180, 150, 0.8), (420, 480, 450, 0.8),
                   (720, 780, 750, 0.8), (725, 775, 751, 0.9)]]
        frames = select_frames(0, 1000, events)
        self.assertEqual([f["phase"] for f in frames], ["early", "middle", "late"])
        self.assertEqual([f["timestamp"] for f in frames], [150, 450, 750])
        self.assertEqual(frames[-1]["tracked_objects"], 2)
        # A clearing/lingering interval must not move all frames to the end.
        self.assertEqual(select_frames(0, 1200, events), frames)

    def test_long_track_covers_phases_and_short_intervals_do_not_duplicate_frames(self):
        frames = select_frames(0, 1000, [{"start": 100, "end": 900, "snapshot": 500, "score": 0.7}])
        self.assertEqual(len(frames), 3)
        self.assertLess(frames[0]["timestamp"], 400)
        self.assertEqual(frames[1]["timestamp"], 500)
        self.assertGreater(frames[-1]["timestamp"], 600)
        point = select_frames(100, 100, [{"start": 100, "end": 100, "snapshot": 100, "score": 0.7}])
        self.assertEqual(len(point), 1)

    def test_metadata_filters_camera_zone_false_positives_and_clamps_overlapping_tracks(self):
        client = FrigateEvidence("https://proxy.example/service", "private-key", "test-camera", "table")
        event = {"camera": "test-camera", "label": "bowl", "zones": ["table"],
                 "start_time": 50, "end_time": 400, "data": {"snapshot_frame_time": 80, "top_score": 0.8}}
        rows = [event, {**event, "camera": "other"}, {**event, "zones": ["other"]},
                {**event, "false_positive": True}, {**event, "label": "person"},
                {**event, "start_time": 500, "end_time": 600}]
        with patch.object(client, "get", return_value=json.dumps(rows)) as get:
            events = client.tableware_events(100, 300)
        self.assertEqual(events, [{"start": 100, "end": 300, "snapshot": 200, "score": 0.8}])
        self.assertIn("include_thumbnails=0", get.call_args.args[0])
        with patch.object(client, "get", return_value=json.dumps([event] * 1000)):
            with self.assertRaises(EvidenceUnavailable):
                client.tableware_events(100, 300)

    def test_native_table_crop_preserves_detail_and_one_composite_budget(self):
        client = FrigateEvidence("https://proxy.example/service", "private-key", "test-camera", "table",
                                 EvidenceOptions(crop_padding=0))
        blob = BytesIO()
        Image.new("RGB", (1920, 1080), "white").save(blob, format="JPEG")
        geometry = {"cameras": {"test-camera": {"zones": {"table": {"coordinates": "0.25,0.25,0.75,0.25,0.75,0.75,0.25,0.75"}}}}}
        def get(path, **kwargs):
            return json.dumps(geometry) if path == "api/config" else blob.getvalue()
        candidate = {"meal_id": "synthetic-meal", "camera": "test-camera", "started_at": 100, "ended_at": 400}
        with patch.object(client, "tableware_events", return_value=[]), patch.object(client, "get", side_effect=get) as request:
            result = client.composite(candidate)
        self.assertEqual(len(result["frames"]), 3)
        self.assertEqual(result["frames"][0]["source_size"], [1920, 1080])
        self.assertEqual(result["frames"][0]["crop_pixels"], [480, 270, 1440, 810])
        self.assertEqual(result["frames"][0]["panel_size"], [960, 540])
        self.assertTrue(any("evenly spaced" in w for w in result["warnings"]))
        images = [c.args[0] for c in request.call_args_list if "/recordings/" in c.args[0]]
        self.assertEqual(len(images), 3)
        self.assertTrue(all("height=" not in path for path in images))
        import base64
        with Image.open(BytesIO(base64.b64decode(result["image_base64"]))) as image:
            self.assertEqual(image.size, (960, 3 * (540 + 28)))
        geometry["cameras"]["test-camera"]["zones"]["table"]["coordinates"] = "0.5,0.5,0.5,0.5,0.5,0.5"
        with patch.object(client, "get", return_value=json.dumps(geometry)), self.assertRaises(EvidenceUnavailable):
            client.table_crop()

    def test_bad_zone_and_missing_metadata_are_explicit_without_live_fallback(self):
        client = FrigateEvidence("https://proxy.example/service", "private-key", "test-camera", "table")
        blob = BytesIO()
        Image.new("RGB", (640, 360), "white").save(blob, format="JPEG")
        candidate = {"meal_id": "synthetic-meal", "camera": "test-camera", "started_at": 100, "ended_at": 200}
        with patch.object(client, "tableware_events", side_effect=EvidenceUnavailable("metadata_offline")), \
                patch.object(client, "table_crop", side_effect=EvidenceUnavailable("table_zone_unavailable")), \
                patch.object(client, "get", return_value=blob.getvalue()) as get:
            result = client.composite(candidate)
        self.assertIn("metadata_offline", result["warnings"])
        self.assertIn("table_zone_unavailable", result["warnings"])
        self.assertIsNone(result["crop_normalized"])
        self.assertEqual(result["frame_timestamps"], [120, 150, 180])
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
