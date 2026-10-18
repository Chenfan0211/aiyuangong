import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore, totp_code


class MfaApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Path(__file__).resolve().parent / "store_config.json"
        self.store = AuthStore(self.root / "ops.db", tenant_id="LPL-001")
        self.store.create_user("manager01", "ShiZiZuo-2026-store", "店长", "manager", "LPL-001", "WH-LION-001")
        self.app = create_app(config_path=self.config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_mfa_setup_and_enable(self):
        login = self.client.post("/api/auth/login", json={"username": "manager01", "password": "ShiZiZuo-2026-store"})
        token = login.json()["token"]
        setup = self.client.post("/api/auth/mfa/setup", headers={"Authorization": "Bearer " + token})
        self.assertEqual(setup.status_code, 200)
        code = totp_code(setup.json()["secret"])
        enabled = self.client.post("/api/auth/mfa/enable", json={"code": code},
                                   headers={"Authorization": "Bearer " + token})
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(len(enabled.json()["recovery_codes"]), 8)

    def test_mfa_login_requires_second_step(self):
        user_id = self.store.list_users("LPL-001", "WH-LION-001")[0]["user_id"]
        enrollment = self.store.begin_mfa_enrollment(user_id)
        self.store.confirm_mfa_enrollment(user_id, totp_code(enrollment["secret"]))
        old = os.environ.get("STORE_AGENT_MFA_ENFORCE")
        os.environ["STORE_AGENT_MFA_ENFORCE"] = "1"
        try:
            login = self.client.post("/api/auth/login", json={"username": "manager01", "password": "ShiZiZuo-2026-store"})
            self.assertEqual(login.status_code, 200)
            self.assertTrue(login.json()["mfa_required"])
            verify = self.client.post("/api/auth/mfa/verify", json={"mfa_token": login.json()["mfa_token"],
                                                                    "code": totp_code(enrollment["secret"])})
            self.assertEqual(verify.status_code, 200)
            self.assertFalse(verify.json()["mfa_required"])
            self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": "Bearer " + verify.json()["token"]}).status_code, 200)
        finally:
            if old is None:
                os.environ.pop("STORE_AGENT_MFA_ENFORCE", None)
            else:
                os.environ["STORE_AGENT_MFA_ENFORCE"] = old


if __name__ == "__main__":
    unittest.main()
