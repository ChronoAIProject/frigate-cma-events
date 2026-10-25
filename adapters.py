"""Protocol normalization; adapters do not decide business outcomes."""
from copy import deepcopy
from datetime import datetime
import json
import uuid

from pipeline import matches, number, path_value


def timestamp(value):
    if number(value):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.timestamp()
    raise ValueError("Timestamp must be finite epoch seconds or timezone-aware ISO8601")


class Adapter:
    def __init__(self, pipeline):
        self.policy = pipeline.spec["source"]

    def normalize(self, event):
        cfg = self.policy
        if event.get("type") not in cfg["types"]:
            raise ValueError("Unsupported source event type")
        if not matches(event, cfg["filter"]):
            return None
        if cfg["adapter"] == "frigate_reviews":
            message = event.get("data", {})
            phase = event["type"].removeprefix("frigate.review.")
            if message.get("type") != phase or phase not in {"new", "update", "end"}:
                raise ValueError("Frigate lifecycle disagrees with envelope")
            row = message.get("after", {})
            subject = row.get("camera")
            if subject != event.get("subject"):
                raise ValueError("Frigate camera disagrees with subject")
            attrs = row.get("data", {})
            if not isinstance(attrs, dict):
                raise ValueError("Invalid review attributes")
            for name in ("objects", "zones", "detections"):
                values = attrs.get(name, [])
                if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
                    raise ValueError("Invalid review attribute values")
            normalized = {"id": row.get("id"), "camera": subject, "severity": row.get("severity"),
                          "data": {k: list(v) for k, v in attrs.items() if k in {"objects", "zones", "detections"}}}
            start, end, identity = row.get("start_time"), row.get("end_time"), row.get("id")
        else:
            fields = cfg["fields"]
            subject = path_value(event, fields.get("subject", "subject"))
            attrs = {name: deepcopy(path_value(event, field)) for name, field in fields.get("attributes", {}).items()}
            if len(json.dumps(attrs, allow_nan=False).encode()) > 8192:
                raise ValueError("Projected attributes exceed 8 KiB")
            if cfg["mode"] == "point":
                phase, identity = "end", event["id"]
                start = end = timestamp(path_value(event, fields.get("time", "time")))
            else:
                phase = path_value(event, fields["phase"])
                identity = path_value(event, fields["id"])
                start = timestamp(path_value(event, fields["start"]))
                end = path_value(event, fields["end"])
                end = None if end is None else timestamp(end)
            if not isinstance(identity, str) or not identity:
                raise ValueError("Missing source record identity")
            # One producer's ID cannot collide with another subject's record.
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([event["source"], subject, identity])))
            normalized = {"id": identity, "subject": subject, "attributes": attrs, "source_event_ids": [event["id"]]}
        if not isinstance(subject, str) or not 1 <= len(subject) <= 256:
            raise ValueError("Invalid normalized subject")
        if cfg["subject"] != "*" and subject != cfg["subject"]:
            return None
        return {"id": identity, "subject": subject, "phase": phase, "start": start, "end": end, "data": normalized}

    def subject(self, stored):
        return stored.get("camera") if self.policy["adapter"] == "frigate_reviews" else stored["subject"]

    def attributes(self, stored):
        if self.policy["adapter"] != "frigate_reviews":
            return stored["attributes"]
        return {"severity": stored.get("severity"), "labels": stored["data"].get("objects", []),
                "zones": stored["data"].get("zones", []), "detections": stored["data"].get("detections", [])}

    def merge(self, previous, current):
        result = deepcopy(current)
        if self.policy["adapter"] == "frigate_reviews":
            # Preserve the existing on-disk representation for reversible upgrades.
            for name in ("objects", "zones", "detections"):
                result["data"][name] = sorted(set(previous["data"].get(name, [])) | set(result["data"].get(name, [])))
            if previous.get("severity") == "alert":
                result["severity"] = "alert"
        else:
            result["source_event_ids"] = sorted(set(previous.get("source_event_ids", [])) | set(result["source_event_ids"]))[:16]
            for name, value in previous["attributes"].items():
                new = result["attributes"].get(name)
                if isinstance(value, list) and isinstance(new, list) and all(isinstance(v, str) for v in [*value, *new]):
                    result["attributes"][name] = sorted(set(value) | set(new))
        return result


def wrap_review(message, source):
    """Stable CloudEvent envelope for Frigate MQTT/REST repair."""
    body = json.dumps(message, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {"specversion": "1.0", "source": source,
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, source + "\n" + body)),
            "type": "frigate.review." + message["type"], "subject": message["after"]["camera"],
            "datacontenttype": "application/json", "data": message}
