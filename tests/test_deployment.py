import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import Config
from deploy.render import render


class DeploymentTests(unittest.TestCase):
    def parameters(self):
        return {"CAMERA": "synthetic-camera", "SEATS_ZONE": "synthetic-seats",
                "TABLE_ZONE": "synthetic-table", "EVENT_SOURCE": "urn:synthetic:source"}

    def test_site_parameters_are_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()
        with self.assertRaises(ValueError):
            render("ghcr.io/example/app:synthetic", "api", {})

    def test_renderer_copies_only_parameters_and_secret_names(self):
        env = {**self.parameters(), "PROVIDER_TOKEN": "synthetic-provider-secret",
               "API_TOKEN": "synthetic-api-secret", "UNRELATED_SETTING": "do-not-copy",
               "TARGET_ORG_ID": "synthetic-org", "WORKER_NAME": "DiningAnalysisWorker",
               "EVIDENCE_CROP_PADDING": "0.05", "EVIDENCE_JPEG_QUALITY": "94"}
        spec = render("ghcr.io/example/app:synthetic", "api", env)
        self.assertEqual(spec["target_org_id"], "synthetic-org")
        service = spec["services"]["api"]
        self.assertEqual(service["replicas"], 1)
        self.assertEqual(service["secret_keys"], ["PROVIDER_TOKEN", "API_TOKEN", "RELAY_TOKEN", "CMA_HMAC_SECRET"])
        self.assertNotIn("PROVIDER_TOKEN", service["env"])
        self.assertNotIn("API_TOKEN", service["env"])
        self.assertNotIn("UNRELATED_SETTING", service["env"])
        self.assertEqual(service["env"]["CAMERA"], "synthetic-camera")
        self.assertEqual(service["env"]["WORKER_NAME"], "DiningAnalysisWorker")
        self.assertEqual(service["env"]["EVIDENCE_CROP_PADDING"], "0.05")
        self.assertEqual(service["env"]["EVIDENCE_JPEG_QUALITY"], "94")

    def test_evidence_settings_load_without_changing_the_aggregation_rules(self):
        env = {**self.parameters(), "PROVIDER_TOKEN": "p" * 48, "API_TOKEN": "a" * 48,
               "EVIDENCE_CROP_PADDING": "0.05", "EVIDENCE_MAX_WIDTH": "1440",
               "EVIDENCE_MAX_HEIGHT": "810", "EVIDENCE_JPEG_QUALITY": "94"}
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.evidence_options.crop_padding, 0.05)
        self.assertEqual(config.evidence_options.max_width, 1440)
        self.assertEqual(config.evidence_options.jpeg_quality, 94)
        self.assertEqual(config.rules.minimum_seconds, 480)
        for key, value in (("EVIDENCE_CROP_PADDING", "nan"), ("EVIDENCE_MAX_WIDTH", "10000"),
                           ("EVIDENCE_JPEG_QUALITY", "100")):
            with patch.dict(os.environ, {**env, key: value}, clear=True), self.assertRaises(ValueError):
                Config.from_env()

    def test_renderer_can_use_mongodb_addon_without_volume_or_secret_values(self):
        env = {**self.parameters(), "PERSISTENCE_BACKEND": "mongodb"}
        spec = render("ghcr.io/example/app:synthetic", "api", env)
        self.assertEqual(spec["addons"], {"mongodb": {}})
        service = spec["services"]["api"]
        self.assertNotIn("volumes", service)
        self.assertEqual(service["addon_env"], {"mongodb": {"uri": "MONGODB_URI", "database": "MONGODB_DATABASE"}})

    def test_config_loads_mounted_secrets_and_requires_distinct_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Path(directory) / "provider"
            operator = Path(directory) / "operator"
            provider.write_text("p" * 40)
            operator.write_text("o" * 40)
            env = {**self.parameters(), "PROVIDER_TOKEN_FILE": str(provider),
                   "API_TOKEN_FILE": str(operator)}
            with patch.dict(os.environ, env, clear=True):
                config = Config.from_env()
                self.assertEqual(config.rules.camera, "synthetic-camera")
                self.assertEqual(config.provider_token, "p" * 40)
                operator.write_text("p" * 40)
                with self.assertRaises(ValueError):
                    Config.from_env()


if __name__ == "__main__":
    unittest.main()
