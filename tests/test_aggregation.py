from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from aggregation import Aggregator, Rules, wrap_review


def settings(**kwargs):
    return Rules(camera="test-camera", seats_zone="test-seats", table_zone="test-table", **kwargs)


def review(identity, start, end, tableware=True, camera="test-camera"):
    return {"id": identity, "camera": camera, "start_time": start, "end_time": end,
            "severity": "alert", "data": {
                "objects": ["person", "bowl"] if tableware else ["person"],
                "zones": ["test-seats", "test-table"] if tableware else ["test-seats"],
                "detections": [identity + "-person"]}}


def event(row, phase="end"):
    return wrap_review({"type": phase, "before": deepcopy(row), "after": deepcopy(row)}, "urn:test:frigate")


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.engine = Aggregator(":memory:", settings())

    def tearDown(self):
        self.engine.close()

    def add(self, row, phase="end", received=None):
        received = received if received is not None else (row["end_time"] or row["start_time"])
        return self.engine.ingest(event(row, phase), received)

    def test_three_close_reviews_become_one_metadata_only_candidate(self):
        # Entirely synthetic review intervals.
        for row in [review("brief-before", 0, 30, False),
                    review("long", 120, 1020), review("brief-after", 1140, 1200, False)]:
            self.add(row)
        decisions = self.engine.flush(1501, source_healthy=True)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "candidate")
        self.assertEqual(decisions[0]["review_count"], 3)
        self.assertEqual(decisions[0]["images"], [])
        self.assertEqual(decisions[0]["evidence_budget"], {
            "max_model_turns": 1, "max_images": 1, "max_source_frames": 3})

    def test_open_review_cannot_be_finished_by_silence(self):
        self.add(review("open", 0, None), "new", received=0)
        self.assertEqual(self.engine.flush(10_000, source_healthy=True), [])
        self.add(review("open", 0, 1000))
        self.assertEqual(self.engine.flush(1301, source_healthy=True)[0]["decision"], "candidate")

    def test_quiet_window_is_not_a_message_count(self):
        self.add(review("a", 0, 1000))
        self.assertEqual(self.engine.flush(1299, source_healthy=True), [])
        self.add(review("b", 1200, 1400, False))
        self.assertEqual(self.engine.flush(1650, source_healthy=True), [])
        self.assertEqual(self.engine.flush(1701, source_healthy=True)[0]["review_count"], 2)

    def test_duplicate_updates_do_not_count_as_more_activity(self):
        row = review("same", 0, 1000)
        incoming = event(row)
        self.assertEqual(self.engine.ingest(incoming, 1000), "stored")
        self.assertEqual(self.engine.ingest(incoming, 1010), "duplicate")
        decision = self.engine.flush(1301, source_healthy=True)[0]
        self.assertEqual(decision["review_count"], 1)
        self.assertEqual(decision["alert_interval_seconds"], 1000)

    def test_identity_cannot_be_reused_with_different_content(self):
        incoming = event(review("a", 0, 1000))
        self.engine.ingest(incoming, 1000)
        changed = deepcopy(incoming)
        changed["data"]["after"]["data"]["objects"].append("plate")
        with self.assertRaises(ValueError):
            self.engine.ingest(changed, 1010)

    def test_late_new_event_does_not_reopen_an_ended_review(self):
        self.add(review("a", 0, 1000))
        self.add(review("a", 0, None), "new", received=1100)
        decision = self.engine.flush(1401, source_healthy=True)[0]
        self.assertEqual(decision["ended_at"], 1000)

    def test_late_merge_does_not_mutate_the_producer_event(self):
        self.add(review("a", 0, None), "new", received=0)
        incoming = event(review("a", 0, 1000, tableware=False))
        original = deepcopy(incoming)
        self.engine.ingest(incoming, 1000)
        self.assertEqual(incoming, original)
        self.assertEqual(self.engine.ingest(incoming, 1010), "duplicate")

    def test_short_visits_and_missing_tableware_are_suppressed(self):
        self.add(review("walk-past", 0, 20))
        self.add(review("sit-without-dishes", 2000, 3000, False))
        decisions = self.engine.flush(3400, source_healthy=True)
        self.assertEqual([d["decision"] for d in decisions], ["short_or_non_dining_activity", "no_tableware_evidence"])

    def test_lost_source_health_is_not_evidence_of_absence(self):
        self.add(review("a", 0, 1000))
        self.assertEqual(self.engine.flush(1301), [])
        self.assertEqual(self.engine.flush(1400, source_healthy=False), [])
        self.assertEqual(len(self.engine.flush(1500, source_healthy=True)), 1)

    def test_other_cameras_are_ignored(self):
        self.assertEqual(self.add(review("study", 0, 1000, camera="study")), "other_camera")
        self.assertEqual(self.engine.flush(1400, source_healthy=True), [])

    def test_overlapping_alerts_are_counted_once(self):
        self.add(review("a", 0, 600))
        self.add(review("b", 100, 700))
        self.assertEqual(self.engine.flush(1001, source_healthy=True)[0]["alert_interval_seconds"], 700)

    def test_new_receipt_grace_is_required_for_delayed_events(self):
        self.add(review("a", 0, 1000), received=2000)
        self.assertEqual(self.engine.flush(2030, source_healthy=True), [])
        self.assertEqual(len(self.engine.flush(2061, source_healthy=True)), 1)

    def test_cooldown_and_daily_candidate_cap(self):
        self.add(review("a", 0, 500))
        self.assertEqual(self.engine.flush(801, source_healthy=True)[0]["decision"], "candidate")
        self.add(review("b", 1000, 1500))
        self.assertEqual(self.engine.flush(1801, source_healthy=True)[0]["decision"], "cooldown")
        self.engine.close()
        self.engine = Aggregator(":memory:", replace(settings(), cooldown_seconds=1, max_per_day=2))
        for index in range(3):
            start = index * 4000
            self.add(review(str(index), start, start + 600))
            decision = self.engine.flush(start + 901, source_healthy=True)[0]
            self.assertEqual(decision["decision"], "candidate" if index < 2 else "daily_limit")

    def test_restart_and_late_updates_do_not_repeat_a_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aggregation.sqlite3"
            first = Aggregator(path, settings())
            incoming = event(review("a", 0, 1000))
            first.ingest(incoming, 1000)
            decision = first.flush(1301, source_healthy=True)[0]
            first.close()
            second = Aggregator(path, settings())
            try:
                self.assertEqual(second.ingest(incoming, 1500), "duplicate")
                self.assertEqual(second.ingest(event(review("a", 0, None), "update"), 1500), "already_finalized")
                self.assertEqual(second.flush(2000, source_healthy=True), [])
                stored = second.db.execute("SELECT id FROM decisions").fetchone()[0]
                self.assertEqual(stored, decision["meal_id"])
            finally:
                second.close()

    def test_mismatched_type_and_nonfinite_time_are_rejected(self):
        bad = event(review("a", 0, 1000))
        bad["type"] = "frigate.review.update"
        with self.assertRaises(ValueError):
            self.engine.ingest(bad, 1000)
        with self.assertRaises(ValueError):
            self.add(review("nan", float("nan"), 1000))


if __name__ == "__main__":
    unittest.main()
