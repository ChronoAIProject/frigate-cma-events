"""Bounded historical evidence. No arbitrary URLs, shell, or model calls."""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
import math
import time
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat, UnidentifiedImageError


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
    sample_frames: int = 24
    selected_frames: int = 12
    burst_seconds: float = 12
    sampling_seconds: float = 35
    density_weight: float = 0.25
    quality_weight: float = 0.35
    novelty_weight: float = 0.4
    duplicate_threshold: float = 0.018
    minimum_quality_ratio: float = 0.6
    novelty_scale: float = 0.12
    tail_coverage_frames: int = 2
    tail_coverage_seconds: float = 180

    def __post_init__(self):
        if any(not finite(getattr(self, name)) for name in self.__dataclass_fields__):
            raise ValueError("Evidence options require finite numeric values")
        if any(type(getattr(self, name)) is not int for name in (
                "max_width", "max_height", "jpeg_quality", "sample_frames", "selected_frames", "tail_coverage_frames")):
            raise ValueError("Evidence dimensions, quality and frame counts require integers")
        if not 0 <= self.crop_padding <= 0.15:
            raise ValueError("EVIDENCE_CROP_PADDING must be between 0 and 0.15")
        if not 320 <= self.max_width <= 1600 or not 180 <= self.max_height <= 900:
            raise ValueError("Evidence panel dimensions exceed supported bounds")
        if not 60 <= self.jpeg_quality <= 95:
            raise ValueError("EVIDENCE_JPEG_QUALITY must be between 60 and 95")
        if not 6 <= self.sample_frames <= 48 or not 3 <= self.selected_frames <= min(12, self.sample_frames):
            raise ValueError("Evidence requires 6..48 samples and 3..12 selected frames, not more than samples")
        if not 1 <= self.tail_coverage_frames <= self.sample_frames // 3 or not 1 <= self.tail_coverage_seconds <= 3600:
            raise ValueError("Tail coverage must fit the bounded sample budget and a 1..3600 second window")
        if not 2 <= self.burst_seconds <= 60 or not 5 <= self.sampling_seconds <= 40:
            raise ValueError("Invalid evidence burst or sampling time budget")
        if any(not finite(v) or not 0 <= v <= 1 for v in (self.density_weight, self.quality_weight,
                self.novelty_weight, self.duplicate_threshold, self.minimum_quality_ratio)):
            raise ValueError("Invalid evidence selection weights or thresholds")
        if self.density_weight + self.quality_weight + self.novelty_weight <= 0 or not 0 < self.novelty_scale <= 1:
            raise ValueError("Selection needs a positive weight and novelty scale")

    def budget(self):
        return {"max_model_turns": 1, "max_images": math.ceil(self.selected_frames / 4),
                "max_source_frames": self.sample_frames, "max_selected_frames": self.selected_frames}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def select_frames(start, end, events):
    """Cover early/middle/late object activity, preferring concurrent tracks.

    Snapshot times are useful anchors, not semantic conclusions. A long-lived
    track can also supply phase midpoints. Empty phases stay empty.
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
                             "selection": "object_tracks", "tracked_objects": rank[0]})
    return selected


def sample_frames(start, end, events, options):
    """Bound candidate-interval coverage while preserving object-activity anchors.

    Two configurable samples are reserved for the candidate tail, independently
    of object-track anchors. Sampling remains bounded by the existing frame budget.
    """
    anchors = {f["phase"]: f["timestamp"] for f in select_frames(start, end, events)}
    activity_start = min(e["start"] for e in events) if events else start
    activity_end = max(e["end"] for e in events) if events else end
    duration = end - start
    tail_start = max(start, end - options.tail_coverage_seconds)
    pools, seen = [], set()
    for i, phase in enumerate(("early", "middle", "late")):
        lo, hi = start + duration * i / 3, start + duration * (i + 1) / 3
        activity_lo = activity_start + (activity_end - activity_start) * i / 3
        activity_hi = activity_start + (activity_end - activity_start) * (i + 1) / 3
        anchor = anchors.get(phase, (activity_lo + activity_hi) / 2)
        anchor = min(hi, max(lo, anchor))
        quota = options.sample_frames // 3 + (i < options.sample_frames % 3)
        tail_count = min(options.tail_coverage_frames, quota) if phase == "late" else 0
        tail_times = []
        if tail_count:
            tail_times = [
                (tail_start + (end - tail_start) * (j / (tail_count - 1) if tail_count > 1 else 1.0),
                 "tail_coverage")
                for j in range(tail_count)
            ]
        remaining = quota - tail_count
        spread = min(remaining, max(2, remaining - 5)) if remaining else 0
        burst_count = remaining - spread
        bursts = [(anchor + k * options.burst_seconds, "anchor_burst")
                  for k in (0, -1, 1, -2, 2)][:burst_count]
        broad = [(lo + (hi - lo) * (j + 0.5) / spread, "interval_coverage")
                 for j in range(spread)] if spread else []
        # Tail observations are reserved first so activity bursts cannot consume
        # the late-phase quota before the candidate boundary is sampled.
        times = tail_times + bursts + broad
        # Fill unused slots when a burst or interval point coincides with another
        # sample; all replacements remain inside the candidate interval.
        times += [(lo + (hi - lo) * (j + 0.5) / max(1, quota * 2), "interval_coverage")
                  for j in range(quota * 2)]
        pool = []
        for timestamp, source in times:
            if len(pool) >= quota:
                break
            timestamp = min(end, max(start, timestamp)) if source == "tail_coverage" else min(hi, max(lo, timestamp))
            key = round(timestamp, 3)
            if key in seen:
                continue
            seen.add(key)
            active = [e for e in events if e["start"] <= timestamp <= e["end"]]
            pool.append({"timestamp": timestamp, "phase": phase, "sampling": source,
                         "selection": "object_tracks" if events else "interval_fallback",
                         "tracked_objects": len(active)})
        pools.append(pool)
    return [pool[j] for j in range(max(map(len, pools), default=0)) for pool in pools if j < len(pool)]

def visual_metrics(panel, polygon=None):
    """Cheap image-quality/novelty heuristics, not semantic or occlusion detection."""
    small = panel.resize((48, 32), Image.Resampling.BILINEAR)
    mask = Image.new("L", small.size, 0 if polygon else 255)
    if polygon:
        ImageDraw.Draw(mask).polygon([(x * small.width, y * small.height) for x, y in polygon], fill=255)
    small.paste((128, 128, 128), mask=ImageChops.invert(mask))
    gray = small.convert("L")
    contrast = ImageStat.Stat(gray, mask).stddev[0]
    focus = ImageStat.Stat(ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(1))), mask).mean[0]
    hist = gray.histogram(mask=mask)
    exposed = (sum(hist[:8]) + sum(hist[248:])) / max(1, sum(hist))
    quality = 0.5 * min(1, focus / 12) + 0.2 * min(1, contrast / 64) + 0.3 * (1 - exposed)
    return {"quality": round(quality, 4), "focus": round(focus, 3),
            "clipped_exposure": round(exposed, 4), "_signature": small.tobytes()}


def visual_distance(left, right):
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / (255 * len(left))


def select_diverse_frames(records, limit, options=None):
    """Keep quality and complementary views within every activity phase."""
    options = options or EvidenceOptions()
    chosen = []
    for i, phase in enumerate(("early", "middle", "late")):
        pool = [r for r in records if r["phase"] == phase]
        quota = limit // 3 + (i < limit % 3)
        picked = []
        peak = max((r["tracked_objects"] for r in pool), default=1) or 1
        floor = max((r["quality"] for r in pool), default=0) * options.minimum_quality_ratio
        if phase == "late" and quota:
            tail = [r for r in pool if r.get("sampling") == "tail_coverage" and r["quality"] >= floor]
            if tail:
                latest_tail = max(tail, key=lambda r: (r["timestamp"], r["quality"]))
                latest_tail["visual_novelty"] = None
                picked.append(latest_tail)
                pool = [r for r in pool if r is not latest_tail]
        while pool and len(picked) < quota:
            ranked = []
            for record in pool:
                if record["quality"] < floor:
                    continue
                novelty = min((visual_distance(record["_signature"], p["_signature"]) for p in picked), default=options.novelty_scale)
                if picked and novelty < options.duplicate_threshold:
                    continue
                rank = (options.density_weight * record["tracked_objects"] / peak + options.quality_weight * record["quality"]
                        + options.novelty_weight * min(1, novelty / options.novelty_scale), record["quality"], record["timestamp"])
                ranked.append((rank, record, novelty))
            if not ranked:
                break
            _, best, novelty = max(ranked, key=lambda item: item[0])
            best["visual_novelty"] = round(novelty, 4) if picked else None
            picked.append(best)
            pool = [r for r in pool if r is not best]
        chosen.extend(picked)
    return sorted(chosen, key=lambda r: r["timestamp"])


def unobserved_intervals(start, end, observed_timestamps):
    """Return intervals between available observations and candidate boundaries."""
    points = [start, *sorted({t for t in observed_timestamps if start < t < end}), end]
    return [[left, right] for left, right in zip(points, points[1:]) if right > left]


class FrigateEvidence:
    def __init__(self, proxy_url, key, camera, crop_zone="", options=None, *, labels=(),
                 version="historical-diversity-v1", cautions=(), aliases=None, allow_http=False):
        url = urlsplit(proxy_url)
        if (url.scheme not in ({"http", "https"} if allow_http else {"https"}) or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("FRIGATE_PROXY_URL must be an HTTPS service proxy URL")
        self.url, self.key, self.camera = proxy_url.rstrip("/"), key, camera
        self.crop_zone, self.options = crop_zone, options or EvidenceOptions()
        self.roi_polygon = None
        self.labels, self.version, self.cautions = tuple(labels), version, list(cautions)
        self.aliases = aliases or {}
        self.opener = build_opener(NoRedirect)

    def get(self, path, limit=2 * 1024 * 1024, timeout=15):
        headers = {"User-Agent": "frigate-cma-events/0.2"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = Request(self.url + "/" + path, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
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

    def object_events(self, start, end):
        if not self.labels:
            return []
        # Include overlapping tracks which started before the activity window.
        query = {"cameras": self.camera, "labels": ",".join(self.labels),
                 "after": start - max(end - start, 60), "before": end + 0.001,
                 "limit": 1000, "include_thumbnails": 0}
        if self.crop_zone:
            query["zones"] = self.crop_zone
        try:
            rows = json.loads(self.get("api/events?" + urlencode(query)))
            if not isinstance(rows, list) or len(rows) >= 1000:
                raise ValueError()
            events = []
            for row in rows:
                if (not isinstance(row, dict) or row.get("camera") != self.camera
                        or row.get("label") not in self.labels
                        or row.get("false_positive") is True
                        or (self.crop_zone and self.crop_zone not in row.get("zones", []))):
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
            raise EvidenceUnavailable("invalid_object_metadata") from exc

    def crop_bounds(self):
        """Use the deployment's normalized Frigate region geometry."""
        self.roi_polygon = None
        if not self.crop_zone:
            return None
        try:
            config = json.loads(self.get("api/config"))
            coords = config["cameras"][self.camera]["zones"][self.crop_zone]["coordinates"]
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
            self.roi_polygon = list(zip(x, y, strict=True))
            return bounds
        except (ValueError, KeyError, TypeError) as exc:
            raise EvidenceUnavailable("crop_zone_unavailable") from exc

    def composite(self, candidate):
        if candidate.get("subject") != self.camera:
            raise EvidenceUnavailable("camera_mismatch")
        start, end = candidate["started_at"], candidate["ended_at"]
        if not all(finite(t) for t in (start, end)) or end < start:
            raise EvidenceUnavailable("invalid_interval")
        deadline = time.monotonic() + self.options.sampling_seconds
        warnings = []
        if candidate.get("test"):
            selections = [{"timestamp": max(start, end - 2), "phase": "validation",
                           "selection": "integration_test", "tracked_objects": 0}]
        else:
            try:
                events = self.object_events(start, end)
            except EvidenceUnavailable as exc:
                events = []
                warnings.append(str(exc))
            selections = sample_frames(start, end, events, self.options)
            if not events:
                warnings.append("No usable configured object tracks; interval and burst samples only.")
        try:
            crop = self.crop_bounds()
        except EvidenceUnavailable as exc:
            crop = None
            warnings.append(str(exc))
        if crop is None:
            warnings.append("Region crop unavailable; full-scene evidence has lower region detail.")
        records, missing, attempted, retained_bytes = [], [], 0, 0
        for selection in selections:
            timestamp = selection["timestamp"]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or retained_bytes >= 24 * 1024 * 1024:
                warnings.append("Candidate sampling budget exhausted; partial coverage only.")
                break
            # Fetch native resolution first. Resizing the whole room before
            # cropping destroys small region details.
            path = "api/" + quote(self.camera, safe="") + f"/recordings/{timestamp:.3f}/snapshot.jpg"
            attempted += 1
            try:
                raw = self.get(path, limit=6 * 1024 * 1024, timeout=min(6, remaining))
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
                polygon = ([(max(0, min(1, (x - crop[0]) / (crop[2] - crop[0]))),
                             max(0, min(1, (y - crop[1]) / (crop[3] - crop[1]))))
                            for x, y in self.roi_polygon] if crop and self.roi_polygon else None)
                encoded = BytesIO()
                panel.save(encoded, format="JPEG", quality=self.options.jpeg_quality)
                jpeg = encoded.getvalue()
                retained_bytes += len(jpeg)
                records.append({**selection, "source_size": original_size, "crop_pixels": box,
                                "panel_size": list(panel.size), "scoring_region": "roi_polygon" if polygon else "crop",
                                **visual_metrics(panel, polygon), "_jpeg": jpeg})
            except (EvidenceUnavailable, UnidentifiedImageError, OSError, Image.DecompressionBombError):
                missing.append(timestamp)
        if not records:
            raise EvidenceUnavailable("historical_frames_unavailable")
        if missing:
            warnings.append("Some candidate historical frames are unavailable; coverage is incomplete.")
        tail_requested = sorted({s["timestamp"] for s in selections if s.get("sampling") == "tail_coverage"})
        tail_available = sorted({r["timestamp"] for r in records if r.get("sampling") == "tail_coverage"})
        tail_available_keys = {round(t, 3) for t in tail_available}
        tail_missing = [t for t in tail_requested if round(t, 3) not in tail_available_keys]
        selected = records if candidate.get("test") else select_diverse_frames(records, self.options.selected_frames, self.options)
        tail_selected = sorted({r["timestamp"] for r in selected if r.get("sampling") == "tail_coverage"})
        if not candidate.get("test") and tail_requested and not tail_selected:
            warnings.append("No usable candidate-tail frame was selected; tail coverage is unavailable.")
        annotations, images = [], []
        for offset in range(0, len(selected), 4):
            page = selected[offset:offset + 4]
            columns = min(2, len(page))
            cell_w, cell_h = max(r["panel_size"][0] for r in page), max(r["panel_size"][1] for r in page) + 28
            output = Image.new("RGB", (columns * cell_w, math.ceil(len(page) / columns) * cell_h), "#111111")
            draw = ImageDraw.Draw(output)
            frame_ids = []
            for j, record in enumerate(page):
                frame_id = f"F{offset + j + 1:02d}"
                frame_ids.append(frame_id)
                x, y = (j % columns) * cell_w, (j // columns) * cell_h
                label = frame_id + " " + record["phase"] + " | " + datetime.fromtimestamp(record["timestamp"], timezone.utc).isoformat(timespec="milliseconds")
                draw.text((x + 6, y + 5), label, fill="white")
                with Image.open(BytesIO(record["_jpeg"])) as panel:
                    output.paste(panel, (x, y + 28))
                annotations.append({**{k: v for k, v in record.items() if not k.startswith("_")},
                                    "frame_id": frame_id, "page": len(images) + 1, "row": j // columns, "column": j % columns})
            data = BytesIO()
            output.save(data, format="JPEG", quality=self.options.jpeg_quality)
            if data.tell() > 2 * 1024 * 1024:
                raise EvidenceUnavailable("composite_size_limit")
            images.append({"page": len(images) + 1, "frame_ids": frame_ids, "size": list(output.size),
                           "mime_type": "image/jpeg", "image_base64": base64.b64encode(data.getvalue()).decode()})
        budget = ({"max_model_turns": 1, "max_images": 1, "max_source_frames": 1, "max_selected_frames": 1}
                  if candidate.get("test") else self.options.budget())
        identity = candidate["candidate_id"]
        return {"candidate_id": identity, "subject": self.camera,
                **{name: identity if path == "candidate_id" else self.camera
                   for name, path in self.aliases.items() if path in {"candidate_id", "subject"}},
                "images": images, "frame_timestamps": [r["timestamp"] for r in selected], "missing_timestamps": missing,
                "evidence_version": self.version, "frames": annotations, "evidence_budget": budget,
                "sampling": {"planned": len(selections), "attempted": attempted, "available": len(records),
                             "selected": len(selected), "discarded": len(records) - len(selected)},
                "coverage": {"candidate_interval": {"start": start, "end": end},
                             "available_timestamps": sorted({r["timestamp"] for r in records}),
                             "selected_timestamps": sorted({r["timestamp"] for r in selected}),
                             "missing_timestamps": sorted(set(missing)),
                             "unobserved_intervals": unobserved_intervals(
                                 start, end, [r["timestamp"] for r in selected]),
                             "tail_window": {"start": max(start, end - self.options.tail_coverage_seconds), "end": end},
                             "tail_requested_timestamps": tail_requested,
                             "tail_available_timestamps": tail_available,
                             "tail_selected_timestamps": tail_selected,
                             "tail_missing_timestamps": tail_missing,
                             "tail_available": bool(tail_selected) if not candidate.get("test") else None},
                "crop_normalized": crop, "warnings": warnings,
                "test": bool(candidate.get("test")),
                "cautions": ["Activity phases do not establish a real-world outcome.",
                             "Track counts, quality and visual novelty are heuristics, not semantic recognition.",
                             *self.cautions]}
