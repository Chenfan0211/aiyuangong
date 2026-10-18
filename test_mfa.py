import os
import tempfile
import unittest
from pathlib import Path

from auth import AuthStore, totp_code, verify_totp


class TotpTests(unittest.TestCase):
    def test_totp_round_trip_and_window(self):
        secret = "JBSWY3DPEHPK3PXP"
        code = totp_code(secret, timestamp=1_700_000_000)
        self.assertEqual(len(code), 6)
        self.assertTrue(verify_totp(secret, code, timestamp=1_700_000_000))
        self.assertTrue(verify_totp(secret, code, timestamp=1_700_000_030))
        self.assertFalse(verify_totp(secret, "000000", timestamp=1_700_000_000))


class MfaStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ops.db"
        self.store = AuthStore(self.db, tenant_id="LPL-001")
        self.store.create_user("manager01", "ShiZiZuo-2026-store", "店长", "manager", "LPL-001", "WH-LION-001")

    def tearDown(self):
        self.tmp.cleanup()

    def user_id(self):
        return self.store.list_users("LPL-001", "WH-LION-001")[0]["user_id"]

    def test_enrollment_recovery_and_reset(self):
        user_id = self.user_id()
        enrollment = self.store.begin_mfa_enrollment(user_id)
        self.assertTrue(enrollment["secret"])
        self.assertIn("otpauth://", enrollment["otpauth_uri"])
        code = totp_code(enrollment["secret"])
        codes = self.store.confirm_mfa_enrollment(user_id, code)
        self.assertEqual(len(codes), 8)
        self.assertTrue(self.store.is_mfa_enabled(user_id))
        self.assertTrue(self.store.verify_mfa(user_id, codes[0]))
        self.assertFalse(self.store.verify_mfa(user_id, codes[0]))
        self.store.reset_mfa(user_id)
        self.assertFalse(self.store.is_mfa_enabled(user_id))

    def test_login_requires_mfa_when_enforced(self):
        user_id = self.user_id()
        enrollment = self.store.begin_mfa_enrollment(user_id)
        self.store.confirm_mfa_enrollment(user_id, totp_code(enrollment["secret"]))
        old = os.environ.get("STORE_AGENT_MFA_ENFORCE")
        os.environ["STORE_AGENT_MFA_ENFORCE"] = "1"
        try:
            first = self.store.login("manager01", "ShiZiZuo-2026-store", "WH-LION-001")
            self.assertTrue(first["mfa_required"])
            completed = self.store.complete_mfa(first["mfa_token"], totp_code(enrollment["secret"]))
            self.assertFalse(completed["mfa_required"])
            self.assertTrue(completed["token"])
            self.assertEqual(completed["context"].user_id, user_id)
        finally:
            if old is None:
                os.environ.pop("STORE_AGENT_MFA_ENFORCE", None)
            else:
                os.environ["STORE_AGENT_MFA_ENFORCE"] = old


if __name__ == "__main__":
    unittest.main()
