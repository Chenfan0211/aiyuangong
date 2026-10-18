import os
import tempfile
import unittest
from pathlib import Path

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
    def test_production_disables_openapi_docs(self):
        old = os.environ.get("STORE_AGENT_ENV")
        os.environ["STORE_AGENT_ENV"] = "production"
        try:
            with tempfile.TemporaryDirectory() as root:
                app = app_fastapi.create_app(
                    config_path=Path(app_fastapi.__file__).resolve().parent / "store_config.json",
                    metrics_db=Path(root) / "metrics.db",
                    store_db=Path(root) / "store.db",
                    chat_db=Path(root) / "chat.db",
                )
                client = TestClient(app)
                self.assertEqual(client.get("/openapi.json").status_code, 404)
                self.assertEqual(client.get("/docs").status_code, 404)
        finally:
            if old is None:
                os.environ.pop("STORE_AGENT_ENV", None)
            else:
                os.environ["STORE_AGENT_ENV"] = old


if __name__ == "__main__":
    unittest.main()
