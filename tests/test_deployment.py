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
               "TARGET_ORG_ID": "synthetic-org"}
        spec = render("ghcr.io/example/app:synthetic", "api", env)
        self.assertEqual(spec["target_org_id"], "synthetic-org")
        service = spec["services"]["api"]
        self.assertEqual(service["replicas"], 1)
        self.assertEqual(service["secret_keys"], ["PROVIDER_TOKEN", "API_TOKEN", "RELAY_TOKEN", "CMA_HMAC_SECRET"])
        self.assertNotIn("PROVIDER_TOKEN", service["env"])
        self.assertNotIn("API_TOKEN", service["env"])
        self.assertNotIn("UNRELATED_SETTING", service["env"])
        self.assertEqual(service["env"]["CAMERA"], "synthetic-camera")

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
