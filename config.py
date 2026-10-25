from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path

from aggregation import Rules
from evidence import EvidenceOptions
from pipeline import Pipeline


def secret(name):
    file = os.environ.get(name + "_FILE")
    return Path(file).read_text().strip() if file else os.environ.get(name, "").strip()


@dataclass(frozen=True)
class Config:
    rules: Rules | Pipeline
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
    frigate_proxy_url: str = ""
    frigate_api_key: str = ""
    evidence_token: str = ""
    evidence_service: str = ""
    evidence_options: EvidenceOptions = EvidenceOptions()
    frigate_direct: bool = False
    processing_enabled: bool = True

    @property
    def pipeline(self):
        return self.rules.pipeline(self.event_source) if isinstance(self.rules, Rules) else self.rules

    @property
    def legacy_rules_document(self):
        return asdict(self.rules) if isinstance(self.rules, Rules) else None

    @classmethod
    def from_env(cls):
        document = os.environ.get("PIPELINE_CONFIG_JSON")
        file = os.environ.get("PIPELINE_CONFIG_FILE")
        if document and file:
            raise ValueError("Use PIPELINE_CONFIG_JSON or PIPELINE_CONFIG_FILE, not both")
        if document or file:
            rule = Pipeline.parse(json.loads(Path(file).read_text() if file else document))
            source = rule.spec["source"]["source"]
            if not source or os.environ.get("EVENT_SOURCE", source) != source:
                raise ValueError("Pipeline source must be explicit and agree with EVENT_SOURCE")
            pipeline = rule
        else:
            required = ("CAMERA", "SEATS_ZONE", "TABLE_ZONE", "EVENT_SOURCE")
            if any(not os.environ.get(k) for k in required):
                raise ValueError("Provide PIPELINE_CONFIG_JSON/FILE or legacy deployment variables: " + ", ".join(required))
            raw = {"camera": os.environ["CAMERA"], "seats_zone": os.environ["SEATS_ZONE"], "table_zone": os.environ["TABLE_ZONE"]}
            for field in fields(Rules):
                if field.name in raw:
                    continue
                value = os.environ.get(field.name.upper())
                if value is not None:
                    raw[field.name] = value if field.name == "timezone" else int(value)
            rule, source = Rules(**raw), os.environ["EVENT_SOURCE"]
            pipeline = rule.pipeline(source)
        evidence_options = dict(pipeline.spec["evidence"]["options"])
        for field in fields(EvidenceOptions):
            value = os.environ.get("EVIDENCE_" + field.name.upper())
            if value is not None:
                evidence_options[field.name] = int(value) if isinstance(getattr(EvidenceOptions(), field.name), int) else float(value)
        direct_url = os.environ.get("FRIGATE_URL", "").strip()
        if direct_url and os.environ.get("FRIGATE_PROXY_URL", "").strip():
            raise ValueError("Use FRIGATE_URL or FRIGATE_PROXY_URL, not both")
        processing = os.environ.get("PROCESSING_ENABLED", "true")
        if processing not in {"true", "false"}:
            raise ValueError("PROCESSING_ENABLED must be true or false")
        config = cls(
            rules=rule, data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            provider_token=secret("PROVIDER_TOKEN"), api_token=secret("API_TOKEN"),
            event_source=source,
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
            frigate_proxy_url=direct_url or os.environ.get("FRIGATE_PROXY_URL", "").strip(),
            frigate_direct=bool(direct_url), processing_enabled=processing == "true",
            frigate_api_key=secret("FRIGATE_API_KEY"),
            evidence_token=secret("WORKER_TOKEN") or secret("EVIDENCE_TOKEN"),
            evidence_service=(os.environ.get("WORKER_SERVICE") or os.environ.get("EVIDENCE_SERVICE", "")).strip(),
            evidence_options=EvidenceOptions(**evidence_options),
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
        if config.frigate_proxy_url and pipeline.spec["evidence"]["adapter"] == "frigate":
            if ((not config.frigate_direct and not config.frigate_api_key)
                    or len(config.evidence_token) < 32 or not config.evidence_service):
                raise ValueError("Evidence requires FRIGATE_API_KEY, EVIDENCE_TOKEN and EVIDENCE_SERVICE")
        if config.evidence_token and (len(config.evidence_token) < 32 or config.evidence_token in {config.api_token, config.provider_token, config.relay_token}):
            raise ValueError("Use a separate worker/evidence token of at least 32 characters")
        return config
