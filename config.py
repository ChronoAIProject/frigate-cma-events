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
    relay_token: str = ""
    provider_slug: str = "frigate_rules"
    worker_name: str = "Frigate rule consumer"
    heartbeat_seconds: int = 90
    tick_seconds: float = 5
    mongo_uri: str = ""
    mongo_database: str = "frigate_cma_events"
    cma_event_url: str = ""
    cma_hmac_key_id: str = ""
    cma_hmac_secret: str = ""
    cma_timeout_seconds: float = 8

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
        config = cls(
            rules=Rules(**raw), data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            provider_token=secret("PROVIDER_TOKEN"), api_token=secret("API_TOKEN"),
            event_source=os.environ["EVENT_SOURCE"],
            relay_token=secret("RELAY_TOKEN") or secret("API_TOKEN"),
            provider_slug=os.environ.get("PROVIDER_SLUG", "frigate_rules"),
            worker_name=os.environ.get("WORKER_NAME", "Frigate rule consumer").strip(),
            heartbeat_seconds=int(os.environ.get("HEARTBEAT_SECONDS", "90")),
            tick_seconds=float(os.environ.get("TICK_SECONDS", "5")),
            mongo_uri=os.environ.get("MONGODB_URI", "").strip(),
            mongo_database=os.environ.get("MONGODB_DATABASE", "frigate_cma_events").strip()
            or "frigate_cma_events",
            cma_event_url=os.environ.get("CMA_EVENT_URL", "").strip(),
            cma_hmac_key_id=os.environ.get("CMA_HMAC_KEY_ID", "").strip(),
            cma_hmac_secret=secret("CMA_HMAC_SECRET"),
            cma_timeout_seconds=float(os.environ.get("CMA_TIMEOUT_SECONDS", "8")),
        )
        if len(config.provider_token) < 32 or len(config.api_token) < 32:
            raise ValueError("PROVIDER_TOKEN and API_TOKEN must each contain at least 32 characters")
        if len(config.relay_token) < 32:
            raise ValueError("RELAY_TOKEN (or API_TOKEN fallback) must contain at least 32 characters")
        if config.provider_token == config.api_token:
            raise ValueError("Use different provider and operator tokens")
        if not 1 <= len(config.worker_name) <= 128:
            raise ValueError("WORKER_NAME must contain between 1 and 128 characters")
        if not 1 <= config.tick_seconds <= 60 or config.heartbeat_seconds < config.tick_seconds * 2:
            raise ValueError("Invalid timer configuration")
        if config.cma_event_url and (not config.cma_hmac_key_id or len(config.cma_hmac_secret) < 16):
            raise ValueError("CMA_HMAC_KEY_ID and CMA_HMAC_SECRET are required when CMA_EVENT_URL is set")
        if not 0.5 <= config.cma_timeout_seconds <= 30:
            raise ValueError("CMA_TIMEOUT_SECONDS must be between 0.5 and 30")
        return config
