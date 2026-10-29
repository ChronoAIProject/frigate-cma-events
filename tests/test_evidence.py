from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import time
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFilter

from evidence import (EvidenceOptions, EvidenceUnavailable, FrigateEvidence, sample_frames,
                      select_diverse_frames, select_frames, visual_metrics)
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
        self.assertEqual(len(first.json()["images"]), 1)
        self.assertEqual(first.json()["evidence_budget"]["max_source_frames"], 1)
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
        candidate = {"candidate_id": "meal-test", "subject": "test-camera", "started_at": 100, "ended_at": 200}
        with patch.object(client, "object_events", return_value=[]), \
                patch.object(client, "get", side_effect=EvidenceUnavailable("missing")) as get:
            with self.assertRaises(EvidenceUnavailable):
                client.composite(candidate)
        self.assertEqual(get.call_count, 24)
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
        client = FrigateEvidence("https://proxy.example/service", "private-key", "test-camera", "table", labels=("bowl",))
        event = {"camera": "test-camera", "label": "bowl", "zones": ["table"],
                 "start_time": 50, "end_time": 400, "data": {"snapshot_frame_time": 80, "top_score": 0.8}}
        rows = [event, {**event, "camera": "other"}, {**event, "zones": ["other"]},
                {**event, "false_positive": True}, {**event, "label": "person"},
                {**event, "start_time": 500, "end_time": 600}]
        with patch.object(client, "get", return_value=json.dumps(rows)) as get:
            events = client.object_events(100, 300)
        self.assertEqual(events, [{"start": 100, "end": 300, "snapshot": 200, "score": 0.8}])
        self.assertIn("include_thumbnails=0", get.call_args.args[0])
        with patch.object(client, "get", return_value=json.dumps([event] * 1000)):
            with self.assertRaises(EvidenceUnavailable):
                client.object_events(100, 300)

    def test_native_table_crop_preserves_detail_and_deduplicates_static_views(self):
        client = FrigateEvidence("https://proxy.example/service", "private-key", "test-camera", "table",
                                 EvidenceOptions(crop_padding=0))
        blob = BytesIO()
        Image.new("RGB", (1920, 1080), "white").save(blob, format="JPEG")
        geometry = {"cameras": {"test-camera": {"zones": {"table": {"coordinates": "0.25,0.25,0.75,0.25,0.75,0.75,0.25,0.75"}}}}}
        def get(path, **kwargs):
            return json.dumps(geometry) if path == "api/config" else blob.getvalue()
        candidate = {"candidate_id": "synthetic-meal", "subject": "test-camera", "started_at": 100, "ended_at": 400}
        with patch.object(client, "object_events", return_value=[]), patch.object(client, "get", side_effect=get) as request:
            result = client.composite(candidate)
        self.assertEqual(len(result["frames"]), 3)
        self.assertEqual(result["coverage"]["tail_window"], {"start": 220, "end": 400})
        self.assertTrue(result["coverage"]["tail_available"])
        self.assertTrue(result["coverage"]["tail_selected_timestamps"])
        self.assertEqual(result["frames"][0]["source_size"], [1920, 1080])
        self.assertEqual(result["frames"][0]["crop_pixels"], [480, 270, 1440, 810])
        self.assertEqual(result["frames"][0]["panel_size"], [960, 540])
        self.assertTrue(any("interval and burst" in w for w in result["warnings"]))
        images = [c.args[0] for c in request.call_args_list if "/recordings/" in c.args[0]]
        self.assertEqual(len(images), 24)
        self.assertTrue(all("height=" not in path for path in images))
        import base64
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["sampling"], {"planned": 24, "attempted": 24, "available": 24, "selected": 3, "discarded": 21})
        with Image.open(BytesIO(base64.b64decode(result["images"][0]["image_base64"]))) as image:
            self.assertEqual(image.size, (1920, 2 * (540 + 28)))
        geometry["cameras"]["test-camera"]["zones"]["table"]["coordinates"] = "0.5,0.5,0.5,0.5,0.5,0.5"
        with patch.object(client, "get", return_value=json.dumps(geometry)), self.assertRaises(EvidenceUnavailable):
            client.crop_bounds()

    def test_bad_zone_and_missing_metadata_are_explicit_without_live_fallback(self):
        client = FrigateEvidence("https://proxy.example/service", "private-key", "test-camera", "table")
        blob = BytesIO()
        Image.new("RGB", (640, 360), "white").save(blob, format="JPEG")
        candidate = {"candidate_id": "synthetic-meal", "subject": "test-camera", "started_at": 100, "ended_at": 200}
        with patch.object(client, "object_events", side_effect=EvidenceUnavailable("metadata_offline")), \
                patch.object(client, "crop_bounds", side_effect=EvidenceUnavailable("crop_zone_unavailable")), \
                patch.object(client, "get", return_value=blob.getvalue()) as get:
            result = client.composite(candidate)
        self.assertIn("metadata_offline", result["warnings"])
        self.assertIn("crop_zone_unavailable", result["warnings"])
        self.assertIsNone(result["crop_normalized"])
        self.assertEqual([f["phase"] for f in result["frames"]], ["early", "middle", "late"])
        self.assertTrue(all(100 <= t <= 200 for t in result["frame_timestamps"]))
        self.assertTrue(all("/recordings/" in c.args[0] for c in get.call_args_list))

    def test_broad_samples_and_rotation_bursts_are_bounded_and_phase_interleaved(self):
        options = EvidenceOptions()
        events = [{"start": 100, "end": 1000, "snapshot": 550, "score": 0.8}]
        samples = sample_frames(0, 1500, events, options)
        self.assertEqual(len(samples), 24)
        self.assertEqual([f["phase"] for f in samples[:3]], ["early", "middle", "late"])
        self.assertEqual(len({round(f["timestamp"], 3) for f in samples}), 24)
        self.assertTrue(all(0 <= f["timestamp"] <= 1500 for f in samples))
        tail = [f for f in samples if f["sampling"] == "tail_coverage"]
        self.assertEqual(len(tail), options.tail_coverage_frames)
        self.assertGreaterEqual(min(f["timestamp"] for f in tail), 1320)
        self.assertEqual(max(f["timestamp"] for f in tail), 1500)
        for phase in ("early", "middle", "late"):
            self.assertEqual(sum(f["candidate_phase"] == phase for f in samples), 8)
        self.assertLess(min(f["timestamp"] for f in samples), 150)
        point = sample_frames(100, 100, [], options)
        self.assertEqual(len(point), 1)

    def test_visual_selection_rejects_repeats_and_blur_and_keeps_complementary_views(self):
        panel = Image.new("RGB", (240, 160), "gray")
        draw = ImageDraw.Draw(panel)
        for x in range(0, 240, 8):
            draw.rectangle((x, 20, x+3, 140), fill="white")
        sharp = visual_metrics(panel)
        blurred = visual_metrics(panel.filter(ImageFilter.GaussianBlur(8)))
        self.assertGreater(sharp["focus"], blurred["focus"])
        records = []
        for phase in ("early", "middle", "late"):
            for j, (signature, quality) in enumerate([(b'\x10'*12, 0.8), (b'\x10'*12, 0.8),
                                                       (b'\x50'*12, 0.8), (b'\x90'*12, 0.8),
                                                       (b'\xe0'*12, 0.1)]):
                records.append({"phase": phase, "timestamp": len(records), "tracked_objects": 2,
                                "quality": quality, "_signature": signature})
        selected = select_diverse_frames(records, 12)
        self.assertEqual(len(selected), 9)
        self.assertTrue(all(r["quality"] == 0.8 for r in selected))
        self.assertEqual(len({(r["phase"],r["_signature"]) for r in selected}), 9)
        late_duplicates = [
            {"phase": "late", "timestamp": timestamp, "tracked_objects": 1,
             "quality": 1.0, "_signature": b"same-image"}
            for timestamp in (2400, 3000, 3599)
        ]
        chosen = select_diverse_frames(late_duplicates, 3)
        self.assertEqual([frame["timestamp"] for frame in chosen], [3599])

    def test_candidate_tail_unavailability_is_reported_without_exceeding_budgets(self):
        client = FrigateEvidence("https://proxy.example/service", "key", "test-camera")
        blob = BytesIO()
        Image.new("RGB", (320, 180), "white").save(blob, "JPEG")
        candidate = {"candidate_id": "synthetic-tail", "subject": "test-camera",
                     "started_at": 0, "ended_at": 3600}
        events = [{"start": 0, "end": 1200, "snapshot": 600, "score": 0.9}]

        def get(path, **kwargs):
            timestamp = float(path.split("/recordings/")[1].split("/")[0])
            if timestamp >= 3420:
                raise EvidenceUnavailable("recording_unavailable")
            return blob.getvalue()

        with patch.object(client, "object_events", return_value=events), \
                patch.object(client, "crop_bounds", return_value=None), \
                patch.object(client, "get", side_effect=get):
            result = client.composite(candidate)

        coverage = result["coverage"]
        self.assertEqual(coverage["candidate_interval"], {"start": 0, "end": 3600})
        self.assertEqual(coverage["tail_window"], {"start": 3420, "end": 3600})
        self.assertTrue(coverage["tail_requested_timestamps"])
        self.assertEqual(coverage["tail_missing_timestamps"], coverage["tail_requested_timestamps"])
        self.assertEqual(coverage["tail_selected_timestamps"], [])
        self.assertFalse(coverage["tail_available"])
        self.assertEqual(coverage["unobserved_intervals"][-1][1], 3600)
        self.assertTrue(any("tail coverage is unavailable" in warning for warning in result["warnings"]))
        self.assertEqual(result["sampling"]["planned"], 24)
        self.assertEqual(result["evidence_budget"]["max_source_frames"], 24)
        self.assertEqual(result["evidence_budget"]["max_selected_frames"], 12)
        self.assertLessEqual(len(result["images"]), 3)

    def test_short_or_disjoint_tracks_cannot_collapse_candidate_coverage(self):
        options = EvidenceOptions(post_window_seconds=120)
        for tracks in ([{"start": 100, "end": 100, "snapshot": 100, "score": 1}],
                       [{"start": 100, "end": 101, "snapshot": 100, "score": 1},
                        {"start": 199, "end": 200, "snapshot": 200, "score": 1}]):
            frames = sample_frames(0, 3600, tracks, options)
            self.assertEqual(len(frames), 24)
            self.assertEqual(len({f["timestamp"] for f in frames}), 24)
            records = [{**f, "quality": 1, "_signature": b"same-image"} for f in frames]
            chosen = select_diverse_frames(records, 12, options)
            self.assertLess(chosen[0]["timestamp"], 400)
            self.assertEqual(chosen[-1]["timestamp"], 3720)
            self.assertIn(3600, [f["timestamp"] for f in chosen])
            self.assertEqual({f["candidate_phase"] for f in chosen}, {"early", "middle", "late"})
            # The reserved tail already participates in duplicate screening.
            self.assertEqual(len(chosen), 4)
        self.assertEqual(len(sample_frames(100, 100, tracks[:1], EvidenceOptions())), 1)

    def test_bad_boundary_quality_does_not_force_a_blurred_frame(self):
        records = [{"phase": "late", "timestamp": t, "quality": q,
                    "tracked_objects": 1, "_signature": b"same-image", "sampling": "tail_coverage"}
                   for t, q in [(1, 1), (2, 0.01)]]
        self.assertEqual([r["timestamp"] for r in select_diverse_frames(records, 12)], [1])

    def test_evidence_options_bound_tail_coverage_to_sample_budget(self):
        with self.assertRaises(ValueError):
            EvidenceOptions(sample_frames=6, tail_coverage_frames=3)

    def test_evidence_pack_limits_pages_and_gives_frames_stable_ids(self):
        client = FrigateEvidence("https://proxy.example/service", "key", "test-camera")
        counter = [0]
        def get(path, **kwargs):
            counter[0] += 1
            blob = BytesIO()
            Image.new("RGB", (320, 180), (counter[0]*10, counter[0]*5, counter[0]*3)).save(blob, "JPEG")
            return blob.getvalue()
        candidate = {"candidate_id": "synthetic-meal", "subject": "test-camera", "started_at": 100, "ended_at": 1000}
        with patch.object(client, "object_events", return_value=[]), patch.object(client, "get", side_effect=get):
            result = client.composite(candidate)
        self.assertEqual(result["evidence_version"], "historical-diversity-v1")
        self.assertEqual(len(result["frames"]), 12)
        self.assertEqual(len(result["images"]), 3)
        self.assertEqual([f["frame_id"] for f in result["frames"]], [f"F{i:02d}" for i in range(1,13)])
        self.assertTrue(all(len(page["frame_ids"]) == 4 for page in result["images"]))
        self.assertEqual(result["evidence_budget"]["max_source_frames"], 24)
        self.assertEqual(counter[0], 24)

    def test_deadline_returns_partial_coverage_without_more_fetches(self):
        client = FrigateEvidence("https://proxy.example/service", "key", "test-camera")
        blob = BytesIO();Image.new("RGB", (320, 180), "white").save(blob,"JPEG")
        candidate = {"candidate_id": "synthetic-meal", "subject": "test-camera", "started_at": 100, "ended_at": 1000}
        with patch.object(client, "object_events", return_value=[]), \
                patch.object(client, "get", return_value=blob.getvalue()) as get, \
                patch("evidence.time.monotonic", side_effect=[0,1,2,3,36]):
            result = client.composite(candidate)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(result["sampling"]["attempted"], 3)
        self.assertEqual(len(result["frames"]), 3)
        self.assertTrue(any("budget exhausted" in w for w in result["warnings"]))

    def test_slow_evidence_does_not_block_ingestion_or_start_duplicate_sampling(self):
        state = self.enable()
        candidate = {"meal_id":"synthetic-candidate", "camera":"test-camera"}
        started, release = threading.Event(), threading.Event()
        def slow(_):
            started.set()
            if not release.wait(2): raise AssertionError("Test did not release extraction")
            return {"images":[],"meal_id":candidate["meal_id"]}
        with patch.object(state, "candidate", return_value=candidate), patch.object(state.evidence, "composite", side_effect=slow) as build:
            with ThreadPoolExecutor(1) as pool:
                first = pool.submit(self.client.get, "/evidence/synthetic-candidate", headers=self.evidence_auth)
                try:
                    self.assertTrue(started.wait(1))
                    acquired = state.lock.acquire(blocking=False)
                    self.assertTrue(acquired)
                    if acquired: state.lock.release()
                    pending = self.client.get("/evidence/synthetic-candidate", headers=self.evidence_auth)
                    self.assertEqual(pending.status_code, 202)
                finally:
                    release.set()
                self.assertEqual(first.result().status_code, 200)
            self.assertEqual(self.client.get("/evidence/synthetic-candidate", headers=self.evidence_auth).status_code, 200)
            self.assertEqual(build.call_count, 1)

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
