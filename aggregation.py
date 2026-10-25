"""Deterministic policy-driven event windows for a CMA-compatible consumer.

This module makes no HTTP requests, reads no images, and calls no models.
HTTP admission and CMA lifecycle endpoints live in service.py.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from adapters import Adapter, wrap_review
from pipeline import Pipeline, matches, number as _number
from profiles import Rules  # Compatibility import for existing callers.


class Aggregator:
    def __init__(self, database, rules):
        self.rules = rules
        self.policy = rules.pipeline() if isinstance(rules, Rules) else rules
        self.adapter = Adapter(self.policy)
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
        if (event.get("specversion") != "1.0" or not isinstance(event.get("source"), str)
                or not event["source"] or not isinstance(event.get("id"), str) or not 1 <= len(event["id"]) <= 512):
            raise ValueError("Invalid CloudEvent identity")
        source = self.policy.spec["source"]["source"]
        if source and event["source"] != source:
            raise ValueError("Event source does not match pipeline")
        normalized = self.adapter.normalize(event)
        if normalized is None:
            return "other_camera" if isinstance(self.rules, Rules) else "filtered"
        phase, start, end = normalized["phase"], normalized["start"], normalized["end"]
        if not _number(start) or not _number(received_at) or (end is not None and not _number(end)):
            raise ValueError("Invalid timestamp")
        if start > received_at + 60 or (end is not None and (end < start or end > received_at + 60)):
            raise ValueError("Invalid event time range")
        if phase not in {"new", "update", "end"} or (phase == "end") != (end is not None):
            raise ValueError("An end event needs an explicit end_time")
        identity, data = normalized["id"], normalized["data"]
        if not isinstance(identity, str) or not identity or len(identity) > 512:
            raise ValueError("Missing record id")
        digest = hashlib.sha256(json.dumps(event, sort_keys=True, allow_nan=False).encode()).hexdigest()
        key = json.dumps([event["source"], event["id"]])
        with self.transaction():
            receipt = self.db.execute("SELECT digest FROM receipts WHERE event_key=?", (key,)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise ValueError("An event identity was reused with different content")
                return "duplicate"
            self.db.execute("INSERT INTO receipts VALUES(?,?)", (key, digest))
            old = self.db.execute("SELECT * FROM reviews WHERE id=?", (identity,)).fetchone()
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
                data = self.adapter.merge(previous, data)
            self.db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?,0)",
                            (identity, start, end, received_at, json.dumps(data)))
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
        cfg = self.policy
        rows = list(self.db.execute("SELECT * FROM reviews WHERE consumed=0 ORDER BY start,id"))
        partitions = {}
        for row in rows:
            subject = self.adapter.subject(json.loads(row["data"]))
            groups = partitions.setdefault(subject, [])
            if groups and (groups[-1]["end"] is None or row["start"] - groups[-1]["end"] <= cfg.merge_seconds):
                group = groups[-1]
                group["rows"].append(row)
                group["end"] = None if group["end"] is None or row["end"] is None else max(group["end"], row["end"])
            else:
                groups.append({"rows": [row], "start": row["start"], "end": row["end"], "subject": subject})
        finalized = []
        for group in [g for groups in partitions.values() for g in groups]:
            if (group["end"] is None or now - group["end"] < cfg.merge_seconds
                    or now - max(r["received"] for r in group["rows"]) < cfg.delivery_grace_seconds):
                continue
            finalized.append(self._decide(group, now))
        return finalized

    def _decide(self, group, now):
        cfg = self.policy.spec
        attrs = [self.adapter.attributes(json.loads(row["data"])) for row in group["rows"]]
        conditions = {}
        for condition in [cfg["activity"], *cfg["requirements"]]:
            selected = [row for row, values in zip(group["rows"], attrs, strict=True) if matches(values, condition["match"])]
            seconds = _union_duration([(row["start"], row["end"]) for row in selected])
            conditions[condition["name"]] = {"matched": len(selected) >= condition["minimum_events"] and seconds >= condition["minimum_seconds"],
                                              "seconds": round(seconds, 2), "events": len(selected)}
        activity = conditions[cfg["activity"]["name"]]
        reason = "candidate"
        day = datetime.fromtimestamp(now, ZoneInfo(cfg["limits"]["timezone"])).date().isoformat()
        subject_alias = next((k for k,v in cfg["output"]["aliases"].items() if v == "subject"), "subject")
        scope = ("$." + subject_alias, group["subject"])
        where = "reason='candidate' AND COALESCE(json_extract(payload,'$.subject'),json_extract(payload,?))=?"
        last = self.db.execute("SELECT max(at) FROM decisions WHERE " + where, scope).fetchone()[0]
        count = self.db.execute("SELECT count(*) FROM decisions WHERE " + where + " AND day=?", (*scope,day)).fetchone()[0]
        if not activity["matched"]:
            reason = cfg["activity"]["reason"]
        elif group["end"] - group["start"] > cfg["window"]["maximum_seconds"]:
            reason = "activity_too_long"
        elif any(not conditions[c["name"]]["matched"] for c in cfg["requirements"]):
            reason = next(c["reason"] for c in cfg["requirements"] if not conditions[c["name"]]["matched"])
        elif last is not None and now - last < cfg["limits"]["cooldown_seconds"]:
            reason = "cooldown"
        elif count >= cfg["limits"]["max_per_day"]:
            reason = "daily_limit"
        ids = [r["id"] for r in group["rows"]]
        prefix = cfg["output"]["id_prefix"] or cfg["name"] + "/" + cfg["source"]["source"] + "/" + group["subject"]
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, prefix + "/" + "/".join(sorted(ids))))
        sample_count = min(len(ids), cfg["output"]["metadata_records"])
        samples, exported_bytes = [], 0
        indexes = sorted({round(i * (len(ids)-1) / max(1, sample_count-1)) for i in range(sample_count)})
        for index in indexes:
            row = group["rows"][index]
            stored = json.loads(row["data"])
            sample = {"record_id": row["id"], "source_event_ids": stored.get("source_event_ids", []),
                      "started_at": row["start"], "ended_at": row["end"], "attributes": attrs[index]}
            size = len(json.dumps(sample, allow_nan=False).encode())
            if exported_bytes + size > 65536:
                break
            samples.append(sample)
            exported_bytes += size
        payload = {"schema": cfg["output"]["schema"], "candidate_id": identity,
                   "subject": group["subject"], "decision": reason, "event_ids": ids[:cfg["output"]["max_event_ids"]],
                   "started_at": group["start"], "ended_at": group["end"],
                   "activity_seconds": activity["seconds"], "conditions": conditions,
                   "event_count": len(ids), "records": samples, "metadata_truncated": len(samples) < len(ids),
                   "images": [], "evidence_budget": cfg["output"]["budget"],
                   "cautions": list(cfg["output"]["cautions"])}
        if cfg["source"]["health"]["mode"] == "observed_events":
            payload["cautions"].append("Only received point events are summarized; source continuity is unverified.")
        payload = self.policy.aliases(payload)
        with self.transaction():
            self.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?)", (identity, now, day, reason, json.dumps(payload)))
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


def replay_events(events, pipeline, now=None):
    """Isolated replay of generic CloudEvents; no delivery or persistent writes."""
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise ValueError("Replay requires 1..100 events")
    engine = Aggregator(":memory:", pipeline)
    observed = []
    try:
        for event in events:
            normalized = engine.adapter.normalize(event)
            if normalized is None:
                continue
            received = normalized["end"] if normalized["end"] is not None else normalized["start"]
            engine.ingest(event, received)
            observed.append(received)
        if not observed:
            return []
        clock = max(observed) + max(pipeline.merge_seconds, pipeline.delivery_grace_seconds) + 1 if now is None else now
        return engine.flush(clock, source_healthy=True)
    finally:
        engine.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Offline review-event aggregation replay; no event delivery")
    parser.add_argument("reviews_json")
    parser.add_argument("--pipeline", type=Path)
    parser.add_argument("--camera")
    parser.add_argument("--seats-zone")
    parser.add_argument("--table-zone")
    args = parser.parse_args()
    if args.pipeline:
        result = replay_events(json.loads(Path(args.reviews_json).read_text()), Pipeline.parse(json.loads(args.pipeline.read_text())))
    else:
        if not all((args.camera, args.seats_zone, args.table_zone)):
            parser.error("Provide --pipeline or all legacy camera/zone arguments")
        result = replay(args.reviews_json, Rules(camera=args.camera, seats_zone=args.seats_zone, table_zone=args.table_zone))
    print(json.dumps(result, ensure_ascii=False, indent=2))
