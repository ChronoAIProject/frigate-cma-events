"""Portable, data-only scenario templates and revision-owned analysis plans."""
import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
SCHEMA = "video-event-monitor.scenario.v1"
SECRET_KEYS = {"token", "api_key", "access_token", "refresh_token", "password", "secret",
               "client_secret", "authorization", "hmac_secret", "private_key"}


def data_only(value, depth=0):
    if depth > 10:
        raise ValueError("Scenario data is too deeply nested")
    if isinstance(value, dict):
        if len(value) > 128 or any(not isinstance(k, str) or len(k) > 256 for k in value):
            raise ValueError("Invalid scenario object")
        if any(k.lower() in SECRET_KEYS for k in value):
            raise ValueError("Keep credentials in deployment service connections")
        for item in value.values():
            data_only(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 128:
            raise ValueError("Scenario lists are limited to 128 entries")
        for item in value:
            data_only(item, depth + 1)
    elif not (value is None or isinstance(value, (str, bool, int)) or
              isinstance(value, float) and math.isfinite(value)):
        raise ValueError("Scenario data must be finite JSON")


def stable_name(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value)


def validate_analysis(value):
    if not isinstance(value, dict) or set(value) - {"version", "instructions", "context", "template"}:
        raise ValueError("Invalid analysis plan fields")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("Analysis plan requires version 1")
    instructions = value.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 16384:
        raise ValueError("Analysis instructions require 1..16384 characters")
    context = value.get("context", {})
    if not isinstance(context, dict) or len(json.dumps(context, allow_nan=False).encode()) > 16384:
        raise ValueError("Analysis context requires an object of at most 16 KiB")
    data_only(context)
    if "template" in value:
        template = value["template"]
        if (not isinstance(template, dict) or set(template) != {"id", "version", "sha256"}
                or not stable_name(template["id"]) or not isinstance(template["version"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", template["version"])
                or not isinstance(template["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", template["sha256"])):
            raise ValueError("Invalid scenario provenance")


def validate_parameter(spec, value):
    kind = spec["type"]
    if kind == "number":
        try:
            valid = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid = False
    else:
        valid = {"string": isinstance(value, str) and len(value) <= 16384,
                 "integer": type(value) is int, "boolean": type(value) is bool,
                 "object": isinstance(value, dict)}[kind]
    if not valid:
        raise ValueError("Scenario parameter has the wrong type")
    data_only(value)


def validate_template(value):
    if len(json.dumps(value, allow_nan=False).encode()) > 49152:
        raise ValueError("Scenario template exceeds 48 KiB")
    if (not isinstance(value, dict) or set(value) != {"schema", "id", "version", "title", "parameters", "pipeline"}
            or value["schema"] != SCHEMA or not stable_name(value["id"])
            or not isinstance(value["version"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value["version"])
            or not isinstance(value["title"], str) or not 1 <= len(value["title"]) <= 128
            or not isinstance(value["parameters"], dict) or len(value["parameters"]) > 32
            or not isinstance(value["pipeline"], dict)):
        raise ValueError("Invalid scenario template")
    data_only(value)
    for name, spec in value["parameters"].items():
        if (not stable_name(name) or not isinstance(spec, dict) or set(spec) - {"type", "title", "default"}
                or spec.get("type") not in {"string", "integer", "number", "boolean", "object"}
                or not isinstance(spec.get("title"), str) or not 1 <= len(spec["title"]) <= 128):
            raise ValueError("Invalid scenario parameter")
        if "default" in spec:
            validate_parameter(spec, spec["default"])
    return deepcopy(value)


def render_template(template, parameters):
    from pipeline import Pipeline
    template = validate_template(template)
    specs = template["parameters"]
    if not isinstance(parameters, dict) or set(parameters) - set(specs):
        raise ValueError("Unknown scenario parameters")
    values = {}
    for name, spec in specs.items():
        if name not in parameters and "default" not in spec:
            raise ValueError("Missing scenario parameter: " + name)
        values[name] = deepcopy(parameters[name] if name in parameters else spec["default"])
        validate_parameter(spec, values[name])

    def expand(value):
        if isinstance(value, dict):
            if "$param" in value:
                if set(value) != {"$param"} or not isinstance(value["$param"], str) or value["$param"] not in values:
                    raise ValueError("Invalid scenario parameter reference")
                return deepcopy(values[value["$param"]])
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    pipeline = expand(template["pipeline"])
    if not isinstance(pipeline.get("analysis"), dict):
        raise ValueError("Scenario template requires an analysis plan")
    pipeline["analysis"]["template"] = {"id": template["id"], "version": template["version"],
        "sha256": hashlib.sha256(json.dumps(template, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    return Pipeline.parse(pipeline).spec


def catalog():
    return [validate_template(json.loads(p.read_text())) for p in sorted((ROOT / "scenarios").glob("*.json"))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--parameters", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error("Write bound installation configuration outside the source checkout")
    result = render_template(json.loads(args.template.read_text()), json.loads(args.parameters.read_text()))
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
