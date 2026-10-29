import json
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.parse import unquote
from uuid import UUID

from examples.dining.render import ROOT, bundle, write_bundle


class DiningExampleTests(unittest.TestCase):
    def parameters(self):
        return {"APP_ORIGIN": "https://dining.example.com", "CAMERA": "synthetic-camera",
                "SEATS_ZONE": "synthetic-seats", "TABLE_ZONE": "synthetic-table",
                "EVENT_SOURCE": "urn:synthetic:dining", "FRIGATE_PROXY_URL": "https://nyxid.example.com/proxy/synthetic",
                "EVIDENCE_SERVICE": "synthetic-evidence", "SHEET_ID": "synthetic-sheet",
                "SHEET_CONNECTION": "synthetic-connection", "SHEET_TAB": "Test's table",
                "CMA_OWNER_ID": "synthetic-owner", "MQTT_TLS": "true", "MQTT_PORT": "8883"}

    def test_bundle_keeps_camera_routes_credentials_and_sheet_paths_consistent(self):
        payloads = bundle("ghcr.io/example/app:synthetic", {**self.parameters(), "RELAY_TOKEN": "must-not-be-copied",
                         "EVIDENCE_DENSITY_WEIGHT": "0.3"})
        self.assertNotIn("must-not-be-copied", json.dumps(payloads))
        self.assertNotIn("${", json.dumps(payloads))
        env = payloads["app-spec.json"]["services"]["dining-api"]["env"]
        self.assertEqual(json.loads(env["PIPELINE_CONFIG_JSON"]), payloads["pipeline.json"])
        self.assertNotIn("CAMERA", env)
        self.assertEqual(payloads["pipeline.json"]["evidence"]["options"]["density_weight"], 0.3)
        relay = payloads["relay-options.json"]
        source = relay["mqtt_sources"][0]
        self.assertIs(source["tls"], True)
        self.assertEqual(source["port"], 8883)
        self.assertEqual(source["heartbeat"]["subject"], relay["routes"][0]["subjects"][0])
        self.assertEqual(source["source_uri"], self.parameters()["EVENT_SOURCE"])
        # An empty topic filter is intentional: heartbeat envelopes have no MQTT topic.
        self.assertEqual(relay["routes"][0]["topics"], [])
        cfg = payloads["dining-config.json"]
        self.assertTrue(cfg["headers_path"].startswith("v4/spreadsheets/"))
        self.assertTrue(unquote(cfg["headers_path"]).endswith("'Test''s table'!A1:J1"))
        self.assertEqual(cfg["columns"], payloads["sheet-header.json"]["values"][0])
        worker = payloads["worker-request.json"]["profile"]
        self.assertEqual(worker["permissions"]["services"], ["synthetic-evidence", "api-google-workspace", "chrono-llm-public"])
        self.assertIn(json.dumps(cfg, ensure_ascii=False), worker["system_prompt"])
        channel = payloads["channel-request.json"]
        self.assertEqual(UUID(channel["record_id"]).version, 4)
        self.assertEqual(channel["provider"], {"slug": "cma_codex"})
        self.assertEqual(channel["partition"]["key"], "per_event")
        self.assertEqual(channel["profile"]["context"]["system_prompt"], worker["system_prompt"])
        self.assertNotEqual(channel["credentials"]["agent_key"], payloads["app-secrets.json"]["PROVIDER_TOKEN"])

    def test_existing_sheet_can_supply_its_own_ten_headers(self):
        columns = ["synthetic-column-" + str(i) for i in range(10)]
        payloads = bundle("synthetic-image", {**self.parameters(), "SHEET_COLUMNS_JSON": json.dumps(columns)})
        self.assertEqual(payloads["dining-config.json"]["columns"], columns)
        with self.assertRaises(ValueError):
            bundle("synthetic-image", {**self.parameters(), "SHEET_COLUMNS_JSON": '["too-few"]'})

    def test_rejects_missing_parameters_and_unsafe_origins(self):
        with self.assertRaises(ValueError):
            bundle("synthetic-image", {})
        for origin in ("http://plain.example", "https://user:pass@example.com", "https://example.com/path"):
            with self.assertRaises(ValueError):
                bundle("synthetic-image", {**self.parameters(), "APP_ORIGIN": origin})

    def test_private_output_refuses_checkout_symlinks_and_overwrites(self):
        with self.assertRaises(ValueError):
            write_bundle(ROOT / "examples" / "private-output", {"test.json": {}})
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "checkout-link"
            link.symlink_to(ROOT, target_is_directory=True)
            with self.assertRaises(ValueError):
                write_bundle(link / "private-output", {"test.json": {}})
            output = Path(tmp) / "private"
            write_bundle(output, {"config.json": {"value": "synthetic"}})
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            path = output / "config.json"
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                write_bundle(output, {"config.json": {"value": "replacement"}})
            self.assertEqual(json.loads(path.read_text())["value"], "synthetic")
