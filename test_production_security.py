import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app_fastapi


class StartupGuardTests(unittest.TestCase):
    def test_demo_mode_rejects_non_loopback_host(self):
        with self.assertRaisesRegex(ValueError, "demo"):
            app_fastapi.validate_startup("0.0.0.0", "demo")

    def test_demo_mode_allows_loopback_host(self):
        app_fastapi.validate_startup("127.0.0.1", "demo")
        app_fastapi.validate_startup("localhost", "demo")

    def test_session_mode_allows_public_host(self):
        app_fastapi.validate_startup("0.0.0.0", "session")


class ProductionDocsTests(unittest.TestCase):
    def test_live_ready_and_metrics_are_public(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            app = app_fastapi.create_app(
                config_path=Path(app_fastapi.__file__).resolve().parent / "store_config.json",
                metrics_db=root_path / "metrics.db",
                store_db=root_path / "store.db",
                chat_db=root_path / "chat.db",
            )
            client = TestClient(app)
            self.assertEqual(client.get("/livez").json()["status"], "ok")
            ready = client.get("/readyz")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json().get("migrations"), "sqlite")
            metrics = client.get("/metrics")
            self.assertEqual(metrics.status_code, 200)
            self.assertIn("mall_ops_up 1", metrics.text)
            self.assertIn("x-request-id", {key.lower() for key in metrics.headers})

    def test_production_disables_openapi_docs(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            env = {
                "STORE_AGENT_ENV": "production",
                "STORE_AGENT_MFA_ENFORCE": "1",
                "STORE_AGENT_MFA_KEY": "mfa-" + "x" * 48,
                "POSTGRES_PASSWORD": "db-" + "p" * 40,
                "DOMAIN": "ops.shizizuo.cn",
                "ACME_EMAIL": "ops@shizizuo.cn",
                "STORE_AGENT_BASE_URL": "https://ops.shizizuo.cn",
                "STORE_AGENT_EVIDENCE_DIR": str(root_path / "evidence"),
                "STORE_AGENT_IMPORT_INBOX": str(root_path / "imports"),
                "STORE_AGENT_BACKUP_LOCAL_ROOT": str(root_path / "backups"),
                "STORE_AGENT_OPS_LOCK": str(root_path / "ops.lock"),
            }
            with patch.dict(os.environ, env, clear=False):
                app = app_fastapi.create_app(
                    config_path=Path(app_fastapi.__file__).resolve().parent / "store_config.json",
                    metrics_db=root_path / "metrics.db",
                    store_db=root_path / "store.db",
                    chat_db=root_path / "chat.db",
                )
            client = TestClient(app)
            self.assertEqual(client.get("/openapi.json").status_code, 404)
            self.assertEqual(client.get("/docs").status_code, 404)


if __name__ == "__main__":
    unittest.main()
