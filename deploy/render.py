"""Render site parameters to a private chrono-cd spec; never read secret values."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evidence import EvidenceOptions
from pipeline import Pipeline


def render(image, service, environ):
    required = ("CAMERA", "SEATS_ZONE", "TABLE_ZONE", "EVENT_SOURCE")
    document, file = environ.get("PIPELINE_CONFIG_JSON"), environ.get("PIPELINE_CONFIG_FILE")
    if document and file:
        raise ValueError("Provide one pipeline configuration source")
    pipeline = Pipeline.parse(json.loads(Path(file).read_text() if file else document)) if document or file else None
    if pipeline is None and any(not environ.get(key) for key in required):
        raise ValueError("Required parameters: " + ", ".join(required))
    names = required + ("TIMEZONE", "PROVIDER_SLUG", "WORKER_NAME", "MERGE_SECONDS", "DELIVERY_GRACE_SECONDS",
                        "MINIMUM_SECONDS", "MAXIMUM_SECONDS", "COOLDOWN_SECONDS", "MAX_PER_DAY",
                        "HEARTBEAT_SECONDS", "TICK_SECONDS", "CMA_EVENT_URL", "CMA_HMAC_KEY_ID",
                        "CMA_TIMEOUT_SECONDS", "FRIGATE_PROXY_URL", "FRIGATE_URL", "PROCESSING_ENABLED", "EVIDENCE_SERVICE", "WORKER_SERVICE")
    names += tuple("EVIDENCE_" + name.upper() for name in EvidenceOptions.__dataclass_fields__)
    env = {key: environ[key] for key in names if key in environ}
    if pipeline is not None:
        for name in ("CAMERA", "SEATS_ZONE", "TABLE_ZONE", "TIMEZONE", "MERGE_SECONDS", "DELIVERY_GRACE_SECONDS",
                     "MINIMUM_SECONDS", "MAXIMUM_SECONDS", "COOLDOWN_SECONDS", "MAX_PER_DAY", "EVENT_SOURCE"):
            env.pop(name, None)
        env["PIPELINE_CONFIG_JSON"] = json.dumps(pipeline.spec, separators=(",", ":"), ensure_ascii=False)
    env["DATA_DIR"] = "/data"
    service_spec = {
        "image": image, "port": 8080, "health_path": "/healthz", "replicas": 1,
        "routes": ["/"], "env": env,
        "secret_keys": ["PROVIDER_TOKEN", "API_TOKEN", "RELAY_TOKEN", "CMA_HMAC_SECRET"],
        "volumes": [{"name": "event-state", "mount_path": "/data", "type": "pvc", "size": "1Gi"}],
        "resources": {"cpu_request": "50m", "memory_request": "128Mi", "cpu_limit": "500m", "memory_limit": "256Mi"}}
    result = {"services": {service: service_spec}}
    if (environ.get("FRIGATE_PROXY_URL") or environ.get("FRIGATE_URL")) and (pipeline is None or pipeline.spec["evidence"]["adapter"] == "frigate"):
        service_spec["secret_keys"] += ["FRIGATE_API_KEY", "WORKER_TOKEN" if environ.get("WORKER_SERVICE") else "EVIDENCE_TOKEN"]
    elif environ.get("WORKER_SERVICE"):
        service_spec["secret_keys"] += ["WORKER_TOKEN"]
    if environ.get("PERSISTENCE_BACKEND") == "mongodb":
        result["addons"] = {"mongodb": {}}
        service_spec.pop("volumes")
        service_spec["addon_env"] = {"mongodb": {"uri": "MONGODB_URI", "database": "MONGODB_DATABASE"}}
    if environ.get("TARGET_ORG_ID"):
        result["target_org_id"] = environ["TARGET_ORG_ID"]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Refuse to put installation-specific settings in the published checkout.
    if Path(__file__).resolve().parents[1] in args.output.resolve().parents:
        parser.error("Write installation specs outside the source checkout")
    spec = render(args.image, args.service, os.environ)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(json.dumps(spec, indent=2) + "\n")
    args.output.chmod(0o600)
