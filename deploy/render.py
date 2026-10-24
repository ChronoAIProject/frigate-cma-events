"""Render site parameters to a private chrono-cd spec; never read secret values."""
import argparse
import json
import os
from pathlib import Path


def render(image, service, environ):
    required = ("CAMERA", "SEATS_ZONE", "TABLE_ZONE", "EVENT_SOURCE")
    if any(not environ.get(key) for key in required):
        raise ValueError("Required parameters: " + ", ".join(required))
    names = required + ("TIMEZONE", "PROVIDER_SLUG", "MERGE_SECONDS", "DELIVERY_GRACE_SECONDS",
                        "MINIMUM_SECONDS", "MAXIMUM_SECONDS", "COOLDOWN_SECONDS", "MAX_PER_DAY",
                        "HEARTBEAT_SECONDS", "TICK_SECONDS")
    env = {key: environ[key] for key in names if key in environ}
    env["DATA_DIR"] = "/data"
    service_spec = {
        "image": image, "port": 8080, "health_path": "/healthz", "replicas": 1,
        "routes": ["/"], "env": env, "secret_keys": ["PROVIDER_TOKEN", "API_TOKEN", "RELAY_TOKEN"],
        "volumes": [{"name": "event-state", "mount_path": "/data", "type": "pvc", "size": "1Gi"}],
        "resources": {"cpu_request": "50m", "memory_request": "128Mi", "cpu_limit": "500m", "memory_limit": "256Mi"}}
    result = {"services": {service: service_spec}}
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
