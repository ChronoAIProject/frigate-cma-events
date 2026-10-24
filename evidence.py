"""Bounded historical evidence. No arbitrary URLs, shell, or model calls."""
import base64
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


class FrigateEvidence:
    def __init__(self, proxy_url, key, camera):
        url = urlsplit(proxy_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("FRIGATE_PROXY_URL must be an HTTPS service proxy URL")
        self.url, self.key, self.camera = proxy_url.rstrip("/"), key, camera
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

    def composite(self, candidate):
        if candidate.get("camera") != self.camera:
            raise EvidenceUnavailable("camera_mismatch")
        start, end = candidate["started_at"], candidate["ended_at"]
        if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (start, end)) or end < start:
            raise EvidenceUnavailable("invalid_interval")
        # Always historical: never substitute today's live frame for old activity.
        times = [max(start, end - 45), max(start, end - 15), max(start, end - 2)]
        if candidate.get("test"):
            times = times[-1:]
        frames, panels, missing = [], [], []
        for timestamp in dict.fromkeys(times):
            path = "api/" + quote(self.camera, safe="") + f"/recordings/{timestamp:.3f}/snapshot.jpg?height=540"
            try:
                raw = self.get(path)
                with Image.open(BytesIO(raw)) as source:
                    if source.width * source.height > 8_000_000:
                        raise EvidenceUnavailable("image_dimensions_limit")
                    panel = source.convert("RGB")
                    panel.thumbnail((960, 540))
                frames.append(timestamp)
                panels.append(panel)
            except (EvidenceUnavailable, UnidentifiedImageError, OSError, Image.DecompressionBombError):
                missing.append(timestamp)
        if not panels:
            raise EvidenceUnavailable("historical_frames_unavailable")
        width = max(p.width for p in panels)
        output = Image.new("RGB", (width, sum(p.height + 28 for p in panels)), "#111111")
        draw, y = ImageDraw.Draw(output), 0
        for panel, timestamp in zip(panels, frames, strict=True):
            draw.text((8, y + 5), datetime.fromtimestamp(timestamp, timezone.utc).isoformat(), fill="white")
            output.paste(panel, (0, y + 28))
            y += panel.height + 28
        data = BytesIO()
        output.save(data, format="JPEG", quality=80)
        if data.tell() > 2 * 1024 * 1024:
            raise EvidenceUnavailable("composite_size_limit")
        return {"meal_id": candidate["meal_id"], "camera": self.camera,
                "mime_type": "image/jpeg", "image_base64": base64.b64encode(data.getvalue()).decode(),
                "frame_timestamps": frames, "missing_timestamps": missing,
                "test": bool(candidate.get("test")),
                "cautions": ["Activity is not proof of a meal; removed food is not proof it was eaten."]}
