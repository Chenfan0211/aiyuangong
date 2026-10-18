import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore, totp_code


class AuthLifecycleApiTests(unittest.TestCase):
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
        self.owner_headers = {"Authorization": "Bearer " + token}
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def login(self, username, password):
        return self.client.post("/api/auth/login", json={"username": username, "password": password}).json()

    def test_temp_password_gate_and_change(self):
        created = self.client.post("/api/admin/members", headers=self.owner_headers,
                                   json={"username": "staff02", "display_name": "员工", "role": "employee"}).json()
        login = self.login("staff02", created["temporary_password"])
        self.assertTrue(login["must_change_password"])
        token = login["token"]
        headers = {"Authorization": "Bearer " + token}
        self.assertEqual(self.client.get("/api/bootstrap", headers=headers).status_code, 403)
        changed = self.client.post("/api/auth/password", headers=headers,
                                   json={"current_password": created["temporary_password"],
                                         "new_password": "Changed-Password-2026"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get("/api/bootstrap", headers=headers).status_code, 401)
        self.assertEqual(self.login("staff02", "Changed-Password-2026")["must_change_password"], False)

    def test_manager_forces_mfa_enrollment_after_password_change(self):
        created = self.client.post("/api/admin/members", headers=self.owner_headers,
                                   json={"username": "manager02", "display_name": "店长", "role": "manager"}).json()
        login = self.login("manager02", created["temporary_password"])
        headers = {"Authorization": "Bearer " + login["token"]}
        self.client.post("/api/auth/password", headers=headers,
                         json={"current_password": created["temporary_password"],
                               "new_password": "Changed-Password-2026"})
        old = os.environ.get("STORE_AGENT_MFA_ENFORCE")
        os.environ["STORE_AGENT_MFA_ENFORCE"] = "1"
        self.addCleanup(lambda: os.environ.pop("STORE_AGENT_MFA_ENFORCE", None) if old is None else os.environ.__setitem__("STORE_AGENT_MFA_ENFORCE", old))
        login = self.login("manager02", "Changed-Password-2026")
        headers = {"Authorization": "Bearer " + login["token"]}
        self.assertTrue(login["mfa_enrollment_required"])
        self.assertEqual(self.client.get("/api/bootstrap", headers=headers).status_code, 403)
        setup = self.client.post("/api/auth/mfa/setup", headers=headers).json()
        enabled = self.client.post("/api/auth/mfa/enable", headers=headers,
                                   json={"code": totp_code(setup["secret"])})
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(self.client.get("/api/bootstrap", headers=headers).status_code, 200)

    def test_last_owner_cannot_be_disabled(self):
        members = self.client.get("/api/admin/members", headers=self.owner_headers).json()["members"]
        owner = next(row for row in members if row["role"] == "owner")
        response = self.client.patch(f"/api/admin/members/{owner['user_id']}", headers=self.owner_headers,
                                     json={"status": "disabled"})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
