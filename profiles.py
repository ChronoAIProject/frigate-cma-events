"""Compatibility presets. Domain choices are emitted as declarative policy."""
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from pipeline import Pipeline


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

    def pipeline(self, source=""):
        from dataclasses import asdict
        import json
        from pathlib import Path

        values = {**{name.upper(): value for name, value in asdict(self).items()}, "EVENT_SOURCE": source}
        def expand(value):
            if isinstance(value, dict):
                return {k: expand(v) for k, v in value.items()}
            if isinstance(value, list):
                return [expand(v) for v in value]
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                return values[value[2:-1]]
            return value
        template = Path(__file__).parent / "examples" / "dining" / "pipeline.example.json"
        return Pipeline.parse(expand(json.loads(template.read_text())))
