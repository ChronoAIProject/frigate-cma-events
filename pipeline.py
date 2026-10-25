"""Validated declarative event pipelines. No expressions, imports or executable hooks."""
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
from zoneinfo import ZoneInfo

from evidence import EvidenceOptions


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def path_value(value, path, default=None):
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def field_path(value):
    if not isinstance(value, str) or len(value) > 256 or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9-]*(\.[A-Za-z_][A-Za-z_0-9-]*)*", value):
        raise ValueError("Invalid field path")
    return value


def keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("Unknown or invalid pipeline fields")


def bounded(value, low, high, *, integer=False):
    if not number(value) or not low <= value <= high or (integer and not isinstance(value, int)):
        raise ValueError("Pipeline limit outside supported bounds")


def strings(value, *, nonempty=False):
    if (not isinstance(value, list) or len(value) > 64 or (nonempty and not value)
            or any(not isinstance(v, str) or not v or len(v) > 2048 for v in value)):
        raise ValueError("Expected a bounded string list")


def validate_predicate(predicate, depth=0):
    if not isinstance(predicate, dict) or depth > 6:
        raise ValueError("Invalid or overly nested predicate")
    if not predicate:
        return
    logical = set(predicate) & {"all", "any", "not"}
    if logical:
        if len(predicate) != 1:
            raise ValueError("Logical predicates have exactly one operator")
        op = next(iter(logical))
        items = [predicate[op]] if op == "not" else predicate[op]
        if not isinstance(items, list) or not 1 <= len(items) <= 16:
            raise ValueError("Invalid predicate operands")
        for item in items:
            validate_predicate(item, depth + 1)
        return
    keys(predicate, ("field", "op", "value"))
    if set(predicate) != {"field", "op", "value"}:
        raise ValueError("Predicate needs field/op/value")
    field_path(predicate["field"])
    op, value = predicate["op"], predicate["value"]
    if op not in {"eq", "in", "contains_any", "gte", "lte", "exists"}:
        raise ValueError("Unsupported predicate operator")
    if op in {"in", "contains_any"}:
        strings(value, nonempty=True)
    elif op in {"gte", "lte"} and not number(value):
        raise ValueError("Numeric comparison requires a finite number")
    elif op == "exists" and type(value) is not bool:
        raise ValueError("exists requires a boolean")
    elif op == "eq" and not (value is None or isinstance(value, (str, bool)) or number(value)):
        raise ValueError("eq requires a scalar")


def matches(value, predicate):
    if not predicate:
        return True
    if "all" in predicate:
        return all(matches(value, p) for p in predicate["all"])
    if "any" in predicate:
        return any(matches(value, p) for p in predicate["any"])
    if "not" in predicate:
        return not matches(value, predicate["not"])
    missing = object()
    actual = path_value(value, predicate["field"], missing)
    op, expected = predicate["op"], predicate["value"]
    if op == "exists":
        return (actual is not missing) == expected
    if actual is missing:
        return False
    if op == "eq":
        return type(actual) is type(expected) and actual == expected
    if op == "in":
        return isinstance(actual, str) and actual in expected
    if op == "contains_any":
        return isinstance(actual, list) and any(item in actual for item in expected)
    return number(actual) and (actual >= expected if op == "gte" else actual <= expected)


@dataclass(frozen=True)
class Pipeline:
    spec: dict

    @classmethod
    def parse(cls, document):
        # Roundtrip detaches caller data and rejects non-finite configuration.
        raw = json.dumps(document, allow_nan=False)
        if len(raw.encode()) > 65536:
            raise ValueError("Pipeline configuration exceeds 64 KiB")
        d = json.loads(raw)
        keys(d, ("version", "name", "source", "window", "activity", "requirements", "limits", "output", "evidence"))
        if (type(d.get("version")) is not int or d["version"] != 1 or not isinstance(d.get("name"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", d["name"])):
            raise ValueError("Pipeline requires version 1 and a stable name")
        if "source" not in d:
            raise ValueError("Pipeline requires a source")
        s = d["source"]
        keys(s, ("adapter", "source", "subject", "types", "mode", "fields", "filter", "health"))
        if s.get("adapter") not in {"frigate_reviews", "cloudevents"}:
            raise ValueError("Unsupported source adapter")
        if not isinstance(s.get("source"), str) or len(s["source"]) > 2048:
            raise ValueError("Invalid source URI")
        s.setdefault("subject", "*")
        if not isinstance(s["subject"], str) or not 1 <= len(s["subject"]) <= 256:
            raise ValueError("Invalid subject selector")
        s.setdefault("types", ["frigate.review.new", "frigate.review.update", "frigate.review.end"] if s["adapter"] == "frigate_reviews" else [])
        strings(s["types"], nonempty=True)
        s.setdefault("filter", {})
        validate_predicate(s["filter"])
        s.setdefault("mode", "span" if s["adapter"] == "frigate_reviews" else "point")
        if s["mode"] not in {"point", "span"} or (s["adapter"] == "frigate_reviews" and s["mode"] != "span"):
            raise ValueError("Invalid event mode")
        s.setdefault("fields", {})
        keys(s["fields"], ("id", "subject", "time", "phase", "start", "end", "attributes"))
        for name, path in s["fields"].items():
            if name == "attributes":
                if not isinstance(path, dict) or len(path) > 32:
                    raise ValueError("Invalid attribute projection")
                for attribute, field in path.items():
                    if "." in field_path(attribute):
                        raise ValueError("Projected attribute names cannot be nested")
                    field_path(field)
            else:
                field_path(path)
        if s["mode"] == "span" and s["adapter"] == "cloudevents" and not {"id", "phase", "start", "end"} <= s["fields"].keys():
            raise ValueError("Span events require id/phase/start/end mappings")
        s.setdefault("health", {})
        h = s["health"]
        keys(h, ("mode", "event_type", "require_resynced"))
        h.setdefault("mode", "heartbeat")
        h.setdefault("event_type", "frigate.bridge.heartbeat" if s["adapter"] == "frigate_reviews" else "bridge.heartbeat")
        h.setdefault("require_resynced", True)
        if (h["mode"] not in {"heartbeat", "observed_events"} or type(h["require_resynced"]) is not bool
                or not isinstance(h["event_type"], str) or not h["event_type"] or h["event_type"] in s["types"]):
            raise ValueError("Invalid source health policy")
        if h["mode"] == "observed_events" and (s["adapter"] != "cloudevents" or s["mode"] != "point"):
            raise ValueError("Only point-event summaries may use observed_events health")
        d.setdefault("window", {})
        w = d["window"]
        keys(w, ("merge_seconds", "settle_seconds", "maximum_seconds"))
        for name, default in (("merge_seconds", 60), ("settle_seconds", 10), ("maximum_seconds", 14400)):
            w.setdefault(name, default)
            bounded(w[name], 1, 86400)
        d.setdefault("activity", {"name": "activity"})
        d.setdefault("requirements", [])
        if not isinstance(d["requirements"], list) or len(d["requirements"]) > 16:
            raise ValueError("Too many requirements")
        names = set()
        for condition in [d["activity"], *d["requirements"]]:
            keys(condition, ("name", "match", "minimum_seconds", "minimum_events", "reason"))
            name = condition.get("name", "")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in names:
                raise ValueError("Condition names must be unique")
            names.add(name)
            condition.setdefault("match", {})
            validate_predicate(condition["match"])
            condition.setdefault("minimum_seconds", 0)
            condition.setdefault("minimum_events", 1)
            condition.setdefault("reason", name + "_insufficient")
            bounded(condition["minimum_seconds"], 0, w["maximum_seconds"])
            bounded(condition["minimum_events"], 1, 10000, integer=True)
            if not isinstance(condition["reason"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", condition["reason"]) or condition["reason"] == "candidate":
                raise ValueError("Invalid suppression reason")
        d.setdefault("limits", {})
        limits = d["limits"]
        keys(limits, ("cooldown_seconds", "max_per_day", "timezone"))
        limits.setdefault("cooldown_seconds", 60)
        limits.setdefault("max_per_day", 100)
        limits.setdefault("timezone", "UTC")
        bounded(limits["cooldown_seconds"], 1, 86400)
        bounded(limits["max_per_day"], 1, 10000, integer=True)
        if not isinstance(limits["timezone"], str):
            raise ValueError("Timezone must be an IANA timezone name")
        ZoneInfo(limits["timezone"])
        d.setdefault("output", {})
        out = d["output"]
        keys(out, ("event_type", "schema", "aliases", "id_prefix", "cautions", "budget", "result_statuses", "result_fields", "max_event_ids", "metadata_records"))
        out.setdefault("event_type", "activity.window_candidate")
        out.setdefault("schema", "activity-window-candidate.v1")
        for name in ("event_type", "schema"):
            if not isinstance(out[name], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", out[name]):
                raise ValueError("Invalid output event/schema")
        out.setdefault("id_prefix", "")
        out.setdefault("max_event_ids", 100)
        out.setdefault("metadata_records", 8)
        bounded(out["max_event_ids"], 1, 100, integer=True)
        bounded(out["metadata_records"], 0, 16, integer=True)
        if not isinstance(out["id_prefix"], str) or len(out["id_prefix"]) > 256 or (out["id_prefix"] and s["subject"] == "*"):
            raise ValueError("A compatibility ID prefix requires a fixed subject")
        out.setdefault("aliases", {})
        reserved = {"schema", "candidate_id", "subject", "decision", "event_ids", "started_at", "ended_at", "activity_seconds", "event_count", "conditions", "images", "evidence_budget", "cautions", "test", "validation_mode", "status", "accepted", "result", "records", "metadata_truncated"}
        if not isinstance(out["aliases"], dict) or len(out["aliases"]) > 16:
            raise ValueError("Invalid output aliases")
        for alias, field in out["aliases"].items():
            if "." in field_path(alias) or alias in reserved:
                raise ValueError("Alias cannot overwrite canonical fields")
            field_path(field)
        out.setdefault("cautions", ["Activity is a candidate, not a confirmed real-world outcome."])
        strings(out["cautions"])
        out.setdefault("budget", {"max_model_turns": 1, "max_images": 0, "max_source_frames": 0})
        keys(out["budget"], ("max_model_turns", "max_images", "max_source_frames", "max_selected_frames"))
        for name, value in out["budget"].items():
            bounded(value, 0, {"max_model_turns": 1, "max_images": 3, "max_source_frames": 48, "max_selected_frames": 12}[name], integer=True)
        out.setdefault("result_statuses", ["processed", "pending", "integration_test"])
        out.setdefault("result_fields", ["reference", "uncertainty"])
        strings(out["result_statuses"], nonempty=True)
        strings(out["result_fields"])
        if "status" in out["result_fields"]:
            raise ValueError("status is a reserved result field")
        d.setdefault("evidence", {"adapter": "none"})
        ev = d["evidence"]
        keys(ev, ("adapter", "crop_zone", "labels", "options", "version", "cautions"))
        ev.setdefault("adapter", "none")
        if ev["adapter"] not in {"none", "frigate"} or (ev["adapter"] == "frigate" and (s["adapter"] != "frigate_reviews" or s["subject"] == "*")):
            raise ValueError("Historical evidence requires a fixed Frigate source")
        ev.setdefault("crop_zone", "")
        ev.setdefault("labels", [])
        ev.setdefault("options", {})
        ev.setdefault("version", "historical-diversity-v1")
        ev.setdefault("cautions", [])
        strings(ev["labels"])
        strings(ev["cautions"])
        if not isinstance(ev["crop_zone"], str) or not isinstance(ev["version"], str) or not 1 <= len(ev["version"]) <= 128:
            raise ValueError("Invalid evidence parameters")
        try:
            EvidenceOptions(**ev["options"])
        except TypeError as exc:
            raise ValueError("Unknown evidence options") from exc
        return cls(d)

    @property
    def subject(self):
        return self.spec["source"]["subject"]

    @property
    def maximum_seconds(self):
        return self.spec["window"]["maximum_seconds"]

    @property
    def merge_seconds(self):
        return self.spec["window"]["merge_seconds"]

    @property
    def delivery_grace_seconds(self):
        return self.spec["window"]["settle_seconds"]

    def state_document(self):
        # Media sampling settings may change without invalidating aggregation.
        return {k: deepcopy(v) for k, v in self.spec.items() if k != "evidence"}

    def aliases(self, payload):
        missing = object()
        aliases = {name: path_value(payload, path, missing) for name, path in self.spec["output"]["aliases"].items()}
        return {**payload, **{name: value for name, value in aliases.items() if value is not missing}}
