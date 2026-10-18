import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from auth import AuthStore, SessionInvalid
from db import Database


class AuthLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ops.db"
        self.auth = AuthStore(self.db, tenant_id="LPL-001")
        self.user = self.auth.create_user("staff01", "Initial-Password-2026", "员工", "employee",
                                          "LPL-001", "WH-LION-001", must_change_password=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_temp_password_must_be_changed(self):
        result = self.auth.login("staff01", "Initial-Password-2026", "WH-LION-001")
        self.assertTrue(result["context"].must_change_password)
        self.auth.change_password(self.user["user_id"], "Initial-Password-2026", "Changed-Password-2026")
        again = self.auth.login("staff01", "Changed-Password-2026", "WH-LION-001")
        self.assertFalse(again["context"].must_change_password)

    def test_change_password_revokes_existing_sessions(self):
        first = self.auth.login("staff01", "Initial-Password-2026", "WH-LION-001")
        self.auth.change_password(self.user["user_id"], "Initial-Password-2026", "Changed-Password-2026")
        with self.assertRaises(SessionInvalid):
            self.auth.resolve(first["token"])

    def test_absolute_expiry_cannot_be_extended(self):
        result = self.auth.login("staff01", "Initial-Password-2026", "WH-LION-001")
        with Database(self.db, tenant_id="LPL-001").connection() as conn:
            conn.execute("update auth_sessions set absolute_expires_at=? where token_hash=?",
                         ((datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds"),
                          __import__("auth").hash_token(result["token"])))
        with self.assertRaises(SessionInvalid):
            self.auth.resolve(result["token"])


if __name__ == "__main__":
    unittest.main()
