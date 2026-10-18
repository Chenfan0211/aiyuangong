import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "store_config.json"
        self.config.write_text((Path(__file__).resolve().parent / "store_config.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.auth = AuthStore(self.root / "ops.db", tenant_id="LPL-001")
        self.auth.create_user("owner01", "ShiZiZuo-2026-store", "店主", "owner", "LPL-001", "WH-LION-001")
        self.app = create_app(config_path=self.config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                              evidence_dir=self.root / "evidence")
        self.client = TestClient(self.app)
        token = self.client.post("/api/auth/login", json={"username": "owner01", "password": "ShiZiZuo-2026-store"}).json()["token"]
        self.headers = {"Authorization": "Bearer " + token}
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_settings_page_is_served(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/static/settings.js", response.text)

    def test_read_and_patch_store_config(self):
        current = self.client.get("/api/admin/config", headers=self.headers)
        self.assertEqual(current.status_code, 200)
        patched = self.client.patch("/api/admin/config", headers=self.headers,
                                    json={"config": {"review_cycle": {"target_qualified_month": 55},
                                                     "rules": {"fresh_days": 3}}})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["config"]["review_cycle"]["target_qualified_month"], 55)
        self.assertEqual(patched.json()["config"]["rules"]["fresh_days"], 3)

    def test_create_member_and_reset_password_and_mfa(self):
        created = self.client.post("/api/admin/members", headers=self.headers,
                                   json={"username": "staff02", "display_name": "员工小张", "role": "employee"})
        self.assertEqual(created.status_code, 200, created.text)
        user_id = created.json()["member"]["user_id"]
        self.assertTrue(created.json()["temporary_password"])
        reset = self.client.post(f"/api/admin/members/{user_id}/reset-password", headers=self.headers)
        self.assertEqual(reset.status_code, 200)
        self.assertTrue(reset.json()["temporary_password"])
        reset_mfa = self.client.post(f"/api/admin/members/{user_id}/reset-mfa", headers=self.headers)
        self.assertEqual(reset_mfa.status_code, 200)
        self.assertFalse(reset_mfa.json()["mfa_enabled"])

    def test_employee_cannot_manage_admin_resources(self):
        self.auth.create_user("staff01", "ShiZiZuo-2026-store", "员工小李", "employee", "LPL-001", "WH-LION-001")
        token = self.client.post("/api/auth/login", json={"username": "staff01", "password": "ShiZiZuo-2026-store"}).json()["token"]
        self.client.cookies.clear()
        response = self.client.get("/api/admin/config", headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
