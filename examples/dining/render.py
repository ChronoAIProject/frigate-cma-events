"""Render non-secret example parameters into a private installation bundle."""
import argparse
from dataclasses import fields
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from deploy.render import render as render_app
from profiles import Rules
from evidence import EvidenceOptions

REQUIRED = (
    "APP_ORIGIN", "CAMERA", "SEATS_ZONE", "TABLE_ZONE", "EVENT_SOURCE",
    "FRIGATE_PROXY_URL", "EVIDENCE_SERVICE", "SHEET_ID", "SHEET_CONNECTION",
    "SHEET_TAB", "CMA_OWNER_ID",
)
DEFAULTS = {
    "APP_SERVICE": "dining-api", "TIMEZONE": "UTC", "WORKER_NAME": "DiningAnalysisWorker",
    "MODEL_SERVICE": "chrono-llm-public", "MODEL_NAME": "gpt-6.1-sol",
    "MODEL_THINKING_EFFORT": "medium", "SHEETS_SERVICE": "api-google-workspace",
    "CMA_HMAC_KEY_ID": "dining-v1", "MQTT_HOST": "core-mosquitto", "MQTT_PORT": "1883",
    "MQTT_USERNAME": "example-relay", "MQTT_TLS": "false",
}
OPTIONAL = ("TARGET_ORG_ID", "PERSISTENCE_BACKEND", "CMA_EVENT_URL", "MINIMUM_SECONDS",
            "MAXIMUM_SECONDS", "MERGE_SECONDS", "DELIVERY_GRACE_SECONDS", "COOLDOWN_SECONDS",
            "MAX_PER_DAY", "HEARTBEAT_SECONDS", "TICK_SECONDS", "CMA_TIMEOUT_SECONDS", "SHEET_COLUMNS_JSON")
OPTIONAL += tuple("EVIDENCE_" + field.name.upper() for field in fields(EvidenceOptions))


def expand(value, parameters):
    if isinstance(value, dict):
        return {k: expand(v, parameters) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, parameters) for v in value]
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Z_]+)\}", lambda m: str(parameters[m[1]]), value)
    return value


def bundle(image, environ):
    if any(not environ.get(k) for k in REQUIRED):
        raise ValueError("Required parameters: " + ", ".join(REQUIRED))
    # Deliberate allowlist: secret env values never enter templates or app env.
    params = {**DEFAULTS, **{k: environ[k] for k in (*REQUIRED, *DEFAULTS, *OPTIONAL) if k in environ}}
    origin = urlsplit(params["APP_ORIGIN"])
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path not in ("", "/") or origin.query or origin.fragment):
        raise ValueError("APP_ORIGIN must be an HTTPS origin without credentials or a path")
    params["APP_ORIGIN"] = params["APP_ORIGIN"].rstrip("/")
    if params["MQTT_TLS"] not in {"true", "false"}:
        raise ValueError("MQTT_TLS must be true or false")
    port = int(params["MQTT_PORT"])
    if not 1 <= port <= 65535:
        raise ValueError("Invalid MQTT_PORT")
    schema = json.loads((HERE / "sheet-schema.json").read_text())
    if params.get("SHEET_COLUMNS_JSON"):
        columns = json.loads(params["SHEET_COLUMNS_JSON"])
        if not isinstance(columns, list) or len(columns) != 10 or any(not isinstance(c, str) or not c for c in columns):
            raise ValueError("SHEET_COLUMNS_JSON must contain ten nonempty column names in schema order")
        schema["columns"] = columns
    base = "v4/spreadsheets/" + quote(params["SHEET_ID"], safe="") + "/values/"
    tab = "'" + params["SHEET_TAB"].replace("'", "''") + "'!"
    cfg = {"spreadsheet_id": params["SHEET_ID"], "tab": params["SHEET_TAB"],
           "sheet_service": params["SHEETS_SERVICE"], "sheet_connection": params["SHEET_CONNECTION"],
           "evidence_service": params["EVIDENCE_SERVICE"], "timezone": params["TIMEZONE"],
           "columns": schema["columns"], "values_base": base,
           "headers_path": base + quote(tab + "A1:J1", safe=""),
           "ids_path": base + quote(tab + "B:B", safe=""),
           "read_path": base + quote(tab + "A:J", safe=""),
           "append_path": base + quote(tab + "A:J", safe="") + ":append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"}
    prompt = (HERE / "worker-prompt.md").read_text() + "\n\nPrivate deployment configuration:\n" + json.dumps(cfg, ensure_ascii=False)
    rules = {"camera": params["CAMERA"], "seats_zone": params["SEATS_ZONE"], "table_zone": params["TABLE_ZONE"]}
    for field in fields(Rules):
        name = field.name.upper()
        if field.name not in rules and name in params:
            rules[field.name] = params[name] if field.name == "timezone" else int(params[name])
    pipeline = Rules(**rules).pipeline(params["EVENT_SOURCE"]).spec
    for name, field in EvidenceOptions.__dataclass_fields__.items():
        value = params.get("EVIDENCE_" + name.upper())
        if value is not None:
            pipeline["evidence"]["options"][name] = int(value) if isinstance(field.default, int) else float(value)
    params["PIPELINE_CONFIG_JSON"] = json.dumps(pipeline, ensure_ascii=False)
    params.update(CHANNEL_RECORD_ID=str(uuid.uuid4()), WORKER_PROMPT=prompt)
    relay = expand(json.loads((HERE / "relay-options.example.json").read_text()), params)
    relay["mqtt_sources"][0].update(port=port, tls=params["MQTT_TLS"] == "true")
    worker = {"name": params["WORKER_NAME"], "profile": {
        "model": params["MODEL_NAME"], "reasoning_effort": params["MODEL_THINKING_EFFORT"],
        "permission_mode": "autonomous", "permissions": {"services": list(dict.fromkeys(
            [params["EVIDENCE_SERVICE"], params["SHEETS_SERVICE"], params["MODEL_SERVICE"]]))},
        "system_prompt": prompt}}
    memory = ("Use the owner's configured dining Sheet below to answer dining-record questions. "
              "Include only rows with status=recorded in real meal summaries. integration_test rows are validation only. "
              "The separate CMA worker performs bounded visual analysis and writes verified rows. "
              "Report evidence uncertainty; do not identify people or infer preferences. "
              "Do not fetch new images or send external notifications for a record query.\n\n"
              + json.dumps(cfg, ensure_ascii=False))
    return {"app-spec.json": render_app(image, params["APP_SERVICE"], params), "pipeline.json": pipeline,
            "relay-options.json": relay, "dining-config.json": cfg, "worker-request.json": worker,
            "channel-request.json": expand(json.loads((HERE / "channel.example.json").read_text()), params),
            "app-secrets.json": json.loads((HERE / "app-secrets.example.json").read_text()),
            "sheet-schema.json": schema, "sheet-header.json": {"majorDimension": "ROWS", "values": [schema["columns"]]},
            "assistant-memory.md": memory}


def write_bundle(directory, payloads):
    directory = Path(directory).resolve()
    if directory.is_relative_to(ROOT):
        raise ValueError("Write installation files outside the source checkout")
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("Output directory must be empty; existing private files will not be overwritten")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for name, value in payloads.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(text + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="An immutable published image reference")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        write_bundle(args.output_dir, bundle(args.image, os.environ))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print("Wrote private bundle. Fill secret placeholders and the ready Agent ID before enabling the relay.")
