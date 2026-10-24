from dataclasses import dataclass, fields
import os
from pathlib import Path

from aggregation import Rules


def secret(name):
    file = os.environ.get(name + "_FILE")
    return Path(file).read_text().strip() if file else os.environ.get(name, "").strip()


@dataclass(frozen=True)
class Config:
    rules: Rules
    data_dir: Path
    provider_token: str
    api_token: str
    event_source: str
    provider_slug: str = "frigate_rules"
    heartbeat_seconds: int = 90
    tick_seconds: float = 5

    @classmethod
    def from_env(cls):
        required = ("CAMERA", "SEATS_ZONE", "TABLE_ZONE", "EVENT_SOURCE")
        if any(not os.environ.get(k) for k in required):
            raise ValueError("Required deployment variables: " + ", ".join(required))
        raw = {"camera": os.environ["CAMERA"], "seats_zone": os.environ["SEATS_ZONE"],
               "table_zone": os.environ["TABLE_ZONE"]}
        for field in fields(Rules):
            if field.name in raw:
                continue
            value = os.environ.get(field.name.upper())
            if value is not None:
                raw[field.name] = value if field.name == "timezone" else int(value)
        config = cls(Rules(**raw), Path(os.environ.get("DATA_DIR", "/data")),
                     secret("PROVIDER_TOKEN"), secret("API_TOKEN"), os.environ["EVENT_SOURCE"],
                     os.environ.get("PROVIDER_SLUG", "frigate_rules"),
                     int(os.environ.get("HEARTBEAT_SECONDS", "90")),
                     float(os.environ.get("TICK_SECONDS", "5")))
        if len(config.provider_token) < 32 or len(config.api_token) < 32:
            raise ValueError("PROVIDER_TOKEN and API_TOKEN must each contain at least 32 characters")
        if config.provider_token == config.api_token:
            raise ValueError("Use different provider and operator tokens")
        if not 1 <= config.tick_seconds <= 60 or config.heartbeat_seconds < config.tick_seconds * 2:
            raise ValueError("Invalid timer configuration")
        return config
