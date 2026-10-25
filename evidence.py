"""Bounded historical evidence. No arbitrary URLs, shell, or model calls."""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
import math
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, ImageDraw, UnidentifiedImageError


class EvidenceUnavailable(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@dataclass(frozen=True)
class EvidenceOptions:
    crop_padding: float = 0.04
    max_width: int = 1280
    max_height: int = 720
    jpeg_quality: int = 92

    def __post_init__(self):
        if not 0 <= self.crop_padding <= 0.15:
            raise ValueError("EVIDENCE_CROP_PADDING must be between 0 and 0.15")
        if not 320 <= self.max_width <= 1600 or not 180 <= self.max_height <= 900:
            raise ValueError("Evidence panel dimensions exceed supported bounds")
        if not 60 <= self.jpeg_quality <= 95:
            raise ValueError("EVIDENCE_JPEG_QUALITY must be between 60 and 95")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def select_frames(start, end, events):
    """Cover early/middle/late tableware activity, preferring concurrent tracks.

    Snapshot times are useful anchors, not proof of food. A long-lived track can
    also supply phase midpoints. Empty phases stay empty; clearing is not a meal.
    """
    if not events:
        return [{"timestamp": start + (end - start) * f, "phase": phase,
                 "selection": "interval_fallback", "tracked_objects": 0}
                for f, phase in zip((0.2, 0.5, 0.8), ("early", "middle", "late"), strict=True)]
    first, last = min(e["start"] for e in events), max(e["end"] for e in events)
    selected, used = [], set()
    for i, phase in enumerate(("early", "middle", "late")):
        lower, upper = first + (last - first) * i / 3, first + (last - first) * (i + 1) / 3
        center = (lower + upper) / 2
        options = {}
        for event in events:
            left, right = max(lower, event["start"]), min(upper, event["end"])
            if left > right:
                continue
            options.setdefault((left + right) / 2, False)
            t = event["snapshot"]
            if lower <= t <= upper:
                options[t] = True
        ranked = []
        for timestamp, snapshot in options.items():
            if round(timestamp, 3) in used:
                continue
            active = [e for e in events if e["start"] <= timestamp <= e["end"]]
            rank = (len(active), sum(e["score"] for e in active), snapshot, -abs(timestamp - center), timestamp)
            ranked.append((rank, timestamp))
        if ranked:
            rank, timestamp = max(ranked)
            used.add(round(timestamp, 3))
            selected.append({"timestamp": timestamp, "phase": phase,
                             "selection": "tableware_tracks", "tracked_objects": rank[0]})
    return selected


class FrigateEvidence:
    def __init__(self, proxy_url, key, camera, table_zone="", options=None):
        url = urlsplit(proxy_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("FRIGATE_PROXY_URL must be an HTTPS service proxy URL")
        self.url, self.key, self.camera = proxy_url.rstrip("/"), key, camera
        self.table_zone, self.options = table_zone, options or EvidenceOptions()
        self.opener = build_opener(NoRedirect)

    def get(self, path, limit=2 * 1024 * 1024):
        request = Request(self.url + "/" + path, headers={
            "Authorization": "Bearer " + self.key, "User-Agent": "frigate-cma-events/0.2"})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise EvidenceUnavailable("upstream_size_limit")
                return raw
        except (OSError, ValueError) as exc:
            raise EvidenceUnavailable("upstream_unavailable") from exc

    def reviews(self, after):
        # Fail closed if this bounded response could have truncated the window.
        query = urlencode({"cameras": self.camera, "after": after, "limit": 1000})
        try:
            rows = json.loads(self.get("api/review?" + query))
            if not isinstance(rows, list) or len(rows) >= 1000:
                raise ValueError()
            if any(not isinstance(r, dict) or r.get("camera") != self.camera for r in rows):
                raise ValueError()
            return rows
        except (ValueError, TypeError) as exc:
            raise EvidenceUnavailable("invalid_review_response") from exc

    def tableware_events(self, start, end):
        # Include overlapping tracks which started before the activity window.
        query = {"cameras": self.camera, "labels": "plate,bowl,cup,bottle",
                 "after": start - max(end - start, 60), "before": end + 0.001,
                 "limit": 1000, "include_thumbnails": 0}
        if self.table_zone:
            query["zones"] = self.table_zone
        try:
            rows = json.loads(self.get("api/events?" + urlencode(query)))
            if not isinstance(rows, list) or len(rows) >= 1000:
                raise ValueError()
            events = []
            for row in rows:
                if (not isinstance(row, dict) or row.get("camera") != self.camera
                        or row.get("label") not in {"plate", "bowl", "cup", "bottle"}
                        or row.get("false_positive") is True
                        or (self.table_zone and self.table_zone not in row.get("zones", []))):
                    continue
                left, right = row.get("start_time"), row.get("end_time")
                right = end if right is None else right
                if not finite(left) or not finite(right) or right < left or right < start or left > end:
                    continue
                left, right = max(left, start), min(right, end)
                data = row.get("data") or {}
                timestamp, score = data.get("snapshot_frame_time"), data.get("top_score")
                if not finite(timestamp) or not left <= timestamp <= right:
                    timestamp = (left + right) / 2
                events.append({"start": left, "end": right, "snapshot": timestamp,
                               "score": score if finite(score) and 0 <= score <= 1 else 0})
            return events
        except (ValueError, TypeError, AttributeError) as exc:
            raise EvidenceUnavailable("invalid_tableware_metadata") from exc

    def table_crop(self):
        """Use the deployment's existing normalized Frigate table-zone geometry."""
        if not self.table_zone:
            return None
        try:
            config = json.loads(self.get("api/config"))
            coords = config["cameras"][self.camera]["zones"][self.table_zone]["coordinates"]
            values = [float(v) for v in coords.split(",")] if isinstance(coords, str) else coords
            if len(values) < 6 or len(values) % 2 or any(not finite(v) or not 0 <= v <= 1 for v in values):
                raise ValueError()
            x, y = values[::2], values[1::2]
            if min(x) >= max(x) or min(y) >= max(y):
                raise ValueError()
            pad = self.options.crop_padding
            bounds = [max(0, min(x) - pad), max(0, min(y) - pad),
                      min(1, max(x) + pad), min(1, max(y) + pad)]
            if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                raise ValueError()
            return bounds
        except (ValueError, KeyError, TypeError) as exc:
            raise EvidenceUnavailable("table_zone_unavailable") from exc

    def composite(self, candidate):
        if candidate.get("camera") != self.camera:
            raise EvidenceUnavailable("camera_mismatch")
        start, end = candidate["started_at"], candidate["ended_at"]
        if not all(finite(t) for t in (start, end)) or end < start:
            raise EvidenceUnavailable("invalid_interval")
        warnings = []
        if candidate.get("test"):
            selections = [{"timestamp": max(start, end - 2), "phase": "validation",
                           "selection": "integration_test", "tracked_objects": 0}]
        else:
            try:
                events = self.tableware_events(start, end)
            except EvidenceUnavailable as exc:
                events = []
                warnings.append(str(exc))
            selections = select_frames(start, end, events)
            if not events:
                warnings.append("No usable tableware tracks; evenly spaced activity samples only.")
        try:
            crop = self.table_crop()
        except EvidenceUnavailable as exc:
            crop = None
            warnings.append(str(exc))
        if crop is None:
            warnings.append("Table crop unavailable; full-scene evidence has lower table detail.")
        frames, panels, missing, annotations, seen = [], [], [], [], set()
        for selection in selections:
            timestamp = selection["timestamp"]
            if round(timestamp, 3) in seen:
                continue
            seen.add(round(timestamp, 3))
            # Fetch native resolution first. Resizing the whole room before
            # cropping destroys precisely the small dish details we need.
            path = "api/" + quote(self.camera, safe="") + f"/recordings/{timestamp:.3f}/snapshot.jpg"
            try:
                raw = self.get(path, limit=6 * 1024 * 1024)
                with Image.open(BytesIO(raw)) as source:
                    if source.width * source.height > 16_000_000:
                        raise EvidenceUnavailable("image_dimensions_limit")
                    panel = source.convert("RGB")
                    original_size = list(panel.size)
                    box = ([math.floor(crop[0] * panel.width), math.floor(crop[1] * panel.height),
                            math.ceil(crop[2] * panel.width), math.ceil(crop[3] * panel.height)] if crop else None)
                    if box:
                        panel = panel.crop(box)
                    panel.thumbnail((self.options.max_width, self.options.max_height), Image.Resampling.LANCZOS)
                frames.append(timestamp)
                panels.append(panel)
                annotations.append({**selection, "source_size": original_size, "crop_pixels": box,
                                    "panel_size": list(panel.size)})
            except (EvidenceUnavailable, UnidentifiedImageError, OSError, Image.DecompressionBombError):
                missing.append(timestamp)
        if not panels:
            raise EvidenceUnavailable("historical_frames_unavailable")
        width = max(p.width for p in panels)
        output = Image.new("RGB", (width, sum(p.height + 28 for p in panels)), "#111111")
        draw, y = ImageDraw.Draw(output), 0
        for panel, annotation in zip(panels, annotations, strict=True):
            label = annotation["phase"] + " | " + datetime.fromtimestamp(annotation["timestamp"], timezone.utc).isoformat()
            draw.text((8, y + 5), label, fill="white")
            output.paste(panel, (0, y + 28))
            y += panel.height + 28
        data = BytesIO()
        output.save(data, format="JPEG", quality=self.options.jpeg_quality)
        if data.tell() > 2 * 1024 * 1024:
            raise EvidenceUnavailable("composite_size_limit")
        return {"meal_id": candidate["meal_id"], "camera": self.camera,
                "mime_type": "image/jpeg", "image_base64": base64.b64encode(data.getvalue()).decode(),
                "frame_timestamps": frames, "missing_timestamps": missing,
                "evidence_version": "tableware-phases-v2", "frames": annotations,
                "crop_normalized": crop, "warnings": warnings,
                "test": bool(candidate.get("test")),
                "cautions": ["Activity is not proof of a meal; removed food is not proof it was eaten.",
                             "Early/middle/late describe tableware activity, not confirmed meal phases.",
                             "The late panel can precede meal end; missing dishes do not establish leftovers."]}
