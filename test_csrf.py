import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore


class CsrfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "store_config.json"
        self.config.write_text((Path(__file__).resolve().parent / "store_config.json").read_text(encoding="utf-8"), encoding="utf-8")
        AuthStore(self.root / "ops.db", tenant_id="LPL-001").create_user(
            "owner01", "ShiZiZuo-2026-store", "店主", "owner", "LPL-001", "WH-LION-001")
        self.app = create_app(config_path=self.config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                              evidence_dir=self.root / "evidence")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_cookie_write_requires_csrf_header(self):
        login = self.client.post("/api/auth/login", json={"username": "owner01", "password": "ShiZiZuo-2026-store"})
        self.assertEqual(login.status_code, 200)
        csrf = self.client.cookies.get("store_ops_csrf")
        self.assertTrue(csrf)
        denied = self.client.post("/api/admin/members", json={"username": "staff02", "display_name": "员工", "role": "employee"})
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.post("/api/admin/members", json={"username": "staff02", "display_name": "员工", "role": "employee"},
                                   headers={"X-CSRF-Token": csrf})
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_bearer_api_is_csrf_exempt(self):
        token = self.client.post("/api/auth/login", json={"username": "owner01", "password": "ShiZiZuo-2026-store"}).json()["token"]
        self.client.cookies.clear()
        response = self.client.post("/api/admin/members", json={"username": "staff03", "display_name": "员工", "role": "employee"},
                                    headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
