"""Deterministic review-event aggregation for a CMA-compatible consumer.

This module makes no HTTP requests, reads no images, and calls no models.
HTTP admission and CMA lifecycle endpoints live in service.py.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Rules:
    camera: str
    seats_zone: str
    table_zone: str
    merge_seconds: int = 300
    delivery_grace_seconds: int = 60
    minimum_seconds: int = 480
    maximum_seconds: int = 14400
    cooldown_seconds: int = 3600
    max_per_day: int = 3
    timezone: str = "UTC"

    def __post_init__(self):
        ZoneInfo(self.timezone)
        if not (0 < self.minimum_seconds < self.maximum_seconds):
            raise ValueError("Invalid duration limits")
        if min(self.merge_seconds, self.delivery_grace_seconds, self.cooldown_seconds, self.max_per_day) <= 0:
            raise ValueError("Limits must be positive")


def wrap_review(message, source):
    """Translate a frigate/reviews MQTT payload into a stable CloudEvent.

    Same payload on redelivery => same event id. Distinct updates of one review
    keep distinct ids, while sharing the camera subject for CMA partitioning.
    The relay must persist this exact envelope for ambiguous-send recovery.
    """
    body = json.dumps(message, sort_keys=True, separators=(",", ":"), allow_nan=False)
    review = message["after"]
    return {"specversion": "1.0", "source": source,
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, source + "\n" + body)),
            "type": "frigate.review." + message["type"],
            "subject": review["camera"], "datacontenttype": "application/json", "data": message}


class Aggregator:
    def __init__(self, database, rules):
        self.rules = rules
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS receipts (
                event_key TEXT PRIMARY KEY, digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY, start REAL NOT NULL, end REAL,
                received REAL NOT NULL, data TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, at REAL NOT NULL, day TEXT NOT NULL,
                reason TEXT NOT NULL, payload TEXT NOT NULL
            );
        """)

    def ingest(self, event, received_at):
        if event.get("specversion") != "1.0" or not event.get("source") or not event.get("id"):
            raise ValueError("Invalid CloudEvent identity")
        phase = event.get("type", "").removeprefix("frigate.review.")
        if phase not in {"new", "update", "end"}:
            raise ValueError("Only Frigate review lifecycle events are accepted")
        message = event.get("data", {})
        if message.get("type") != phase:
            raise ValueError("CloudEvent and Frigate event types disagree")
        review = message.get("after", {})
        if review.get("camera") != self.rules.camera or event.get("subject") != self.rules.camera:
            return "other_camera"
        start, end = review.get("start_time"), review.get("end_time")
        if not _number(start) or not _number(received_at) or (end is not None and not _number(end)):
            raise ValueError("Invalid timestamp")
        if start > received_at + 60 or (end is not None and (end < start or end > received_at + 60)):
            raise ValueError("Invalid event time range")
        if (phase == "end") != (end is not None):
            raise ValueError("An end event needs an explicit end_time")
        if not isinstance(review.get("id"), str) or not review["id"]:
            raise ValueError("Missing review id")
        data = review.get("data", {})
        if not isinstance(data, dict) or not all(isinstance(data.get(k, []), list) for k in ("objects", "zones", "detections")):
            raise ValueError("Invalid review data")
        if not all(isinstance(x, str) for k in ("objects", "zones", "detections") for x in data.get(k, [])):
            raise ValueError("Invalid review data values")
        data = dict(data)
        severity = review.get("severity")
        digest = hashlib.sha256(json.dumps(event, sort_keys=True, allow_nan=False).encode()).hexdigest()
        key = json.dumps([event["source"], event["id"]])
        with self.transaction():
            receipt = self.db.execute("SELECT digest FROM receipts WHERE event_key=?", (key,)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise ValueError("An event identity was reused with different content")
                return "duplicate"
            self.db.execute("INSERT INTO receipts VALUES(?,?)", (key, digest))
            old = self.db.execute("SELECT * FROM reviews WHERE id=?", (review["id"],)).fetchone()
            if old:
                if start != old["start"]:
                    raise ValueError("Review start_time changed")
                if old["consumed"]:
                    return "already_finalized"
                previous = json.loads(old["data"])
                # Late new/update events cannot reopen a review already ended.
                if phase == "end" and old["end"] is not None and end != old["end"]:
                    raise ValueError("Review end_time changed")
                end = old["end"] if old["end"] is not None else end
                for field in ("objects", "zones", "detections"):
                    data[field] = sorted(set(previous["data"].get(field, [])) | set(data.get(field, [])))
                if previous.get("severity") == "alert":
                    severity = "alert"
            normalized = {"id": review["id"], "camera": review["camera"], "severity": severity, "data": data}
            self.db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?,0)",
                            (review["id"], start, end, received_at, json.dumps(normalized)))
        return "stored"

    def flush(self, now, *, source_healthy=False):
        """Finalize closed, quiet groups only after an affirmative source-health check.

        Missing events are not proof of absence. The live adapter must require a
        fresh bridge heartbeat and successful resynchronization after reconnect.
        """
        if not source_healthy:
            return []
        if not _number(now):
            raise ValueError("Invalid flush time")
        cfg = self.rules
        rows = list(self.db.execute("SELECT * FROM reviews WHERE consumed=0 ORDER BY start,id"))
        groups = []
        for row in rows:
            if groups and (groups[-1]["end"] is None or row["start"] - groups[-1]["end"] <= cfg.merge_seconds):
                group = groups[-1]
                group["rows"].append(row)
                group["end"] = None if group["end"] is None or row["end"] is None else max(group["end"], row["end"])
            else:
                groups.append({"rows": [row], "start": row["start"], "end": row["end"]})
        finalized = []
        for group in groups:
            if (group["end"] is None or now - group["end"] < cfg.merge_seconds
                    or now - max(r["received"] for r in group["rows"]) < cfg.delivery_grace_seconds):
                continue
            finalized.append(self._decide(group, now))
        return finalized

    def _decide(self, group, now):
        cfg = self.rules
        reviews = [json.loads(row["data"]) for row in group["rows"]]
        seated = [(row["start"], row["end"]) for row, review in zip(group["rows"], reviews, strict=True)
                  if review["severity"] == "alert" and cfg.seats_zone in review["data"].get("zones", [])
                  and "person" in review["data"].get("objects", [])]
        duration = _union_duration(seated)
        tableware = any(cfg.table_zone in r["data"].get("zones", [])
                        and {"plate", "bowl", "cup", "bottle"}.intersection(r["data"].get("objects", []))
                        for r in reviews)
        reason = "candidate"
        day = datetime.fromtimestamp(now, ZoneInfo(cfg.timezone)).date().isoformat()
        last = self.db.execute("SELECT max(at) FROM decisions WHERE reason='candidate'").fetchone()[0]
        count = self.db.execute("SELECT count(*) FROM decisions WHERE reason='candidate' AND day=?", (day,)).fetchone()[0]
        if duration < cfg.minimum_seconds:
            reason = "short_or_non_dining_activity"
        elif group["end"] - group["start"] > cfg.maximum_seconds:
            reason = "activity_too_long"
        elif not tableware:
            reason = "no_tableware_evidence"
        elif last is not None and now - last < cfg.cooldown_seconds:
            reason = "cooldown"
        elif count >= cfg.max_per_day:
            reason = "daily_limit"
        ids = [r["id"] for r in reviews]
        meal_id = str(uuid.uuid5(uuid.NAMESPACE_URL, cfg.camera + "/" + "/".join(sorted(ids))))
        payload = {"schema": "dining-review-candidate.v1", "meal_id": meal_id,
                   "camera": cfg.camera, "decision": reason, "review_ids": ids,
                   "started_at": group["start"], "ended_at": group["end"],
                   "alert_interval_seconds": round(duration, 2), "tableware_seen": tableware,
                   "review_count": len(ids), "images": [],
                   "evidence_budget": {"max_model_turns": 1, "max_images": 1, "max_source_frames": 3},
                   "cautions": ["Candidate activity, not a confirmed meal.",
                                "Review intervals and zones do not measure continuous seat occupancy.",
                                "No image was viewed. Food, leftovers and clearing are unverified."]}
        with self.transaction():
            self.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?)", (meal_id, now, day, reason, json.dumps(payload)))
            self.db.executemany("UPDATE reviews SET consumed=1 WHERE id=?", [(i,) for i in ids])
        return payload

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield
        else:
            with self.db:
                yield

    def close(self):
        self.db.close()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _union_duration(intervals):
    total, end = 0.0, None
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end if end is not None else start))
        end = max(end, stop) if end is not None else stop
    return total


def replay(path, rules):
    """Offline replay of closed Frigate REST reviews; never contacts CMA/NyxBot."""
    reviews = json.loads(Path(path).read_text())
    engine = Aggregator(":memory:", rules)
    closed = [r for r in reviews if r.get("end_time") is not None and r.get("camera") == rules.camera]
    if not closed:
        return []
    try:
        for review in sorted(closed, key=lambda r: r["end_time"]):
            engine.ingest(wrap_review({"type": "end", "before": review, "after": review}, "urn:frigate:replay"), review["end_time"])
        now = max(r["end_time"] for r in closed) + max(rules.merge_seconds, rules.delivery_grace_seconds) + 1
        return engine.flush(now, source_healthy=True)
    finally:
        engine.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Offline review-event aggregation replay; no event delivery")
    parser.add_argument("reviews_json")
    parser.add_argument("--camera", required=True)
    parser.add_argument("--seats-zone", required=True)
    parser.add_argument("--table-zone", required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args.reviews_json, Rules(camera=args.camera, seats_zone=args.seats_zone, table_zone=args.table_zone)), ensure_ascii=False, indent=2))
