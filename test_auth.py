# -*- coding: utf-8 -*-
"""批次 7：身份、口令、会话、RBAC、脱敏与审计的测试。"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from auth import (
    AccountLocked,
    AuthError,
    AuthStore,
    CredentialsError,
    MAX_LOGIN_FAILURES,
    PasswordPolicyError,
    ROLES,
    ROLE_PERMISSIONS,
    SecurityContext,
    SessionInvalid,
    has_permission,
    hash_password,
    hash_token,
    permissions_for,
    redact,
    redact_payload,
    validate_password,
    verify_password,
)

TENANT = "LPL-001"
STORE = "WH-LION-001"
STORE_B = "WH-LION-002"
PASSWORD = "ShiZiZuo-2026-store"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "auth.db"
        self.auth = AuthStore(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def make_user(self, username="manager01", role="manager", password=PASSWORD, store=STORE, display="店长张三"):
        return self.auth.create_user(username, password, display, role, TENANT, store)


class PasswordTests(Base):
    def test_hash_is_salted_and_verifiable(self):
        algo, salt, digest = hash_password(PASSWORD)
        algo2, salt2, digest2 = hash_password(PASSWORD)
        self.assertNotEqual(salt, salt2)
        self.assertNotEqual(digest, digest2)
        self.assertTrue(verify_password(PASSWORD, algo, salt, digest))
        self.assertFalse(verify_password(PASSWORD + "x", algo, salt, digest))
        self.assertFalse(verify_password(PASSWORD, "broken", salt, digest))

    def test_password_policy(self):
        for bad in ("short1!", "password", "1234567890", "aaaaaaaaaaa"):
            with self.subTest(password=bad):
                with self.assertRaises(PasswordPolicyError):
                    validate_password(bad, "manager01")
        with self.assertRaises(PasswordPolicyError):
            validate_password("manager01ABCD", "manager01")
        validate_password(PASSWORD, "manager01")

    def test_user_is_not_stored_with_plaintext_password(self):
        user = self.make_user()
        raw = self.db.read_bytes()
        self.assertNotIn(PASSWORD.encode("utf-8"), raw)
        with self.auth._connection() as conn:
            row = conn.execute("select * from users where user_id=?", (user["user_id"],)).fetchone()
        self.assertEqual(row["password_algo"].split("$")[0], "scrypt")
        self.assertNotIn(PASSWORD, row["password_hash"])

    def test_duplicate_username_rejected(self):
        self.make_user()
        with self.assertRaises(AuthError):
            self.make_user(display="另一个人")


class LoginTests(Base):
    def test_login_and_resolve(self):
        user = self.make_user()
        result = self.auth.login("manager01", PASSWORD)
        self.assertTrue(result["token"])
        context = self.auth.resolve(result["token"])
        self.assertEqual((context.user_id, context.role, context.store_id), (user["user_id"], "manager", STORE))
        self.assertTrue(context.has("tasks:verify"))
        self.assertEqual(context.to_user_dict()["role_label"], "店长")

    def test_wrong_password_fails_and_is_audited(self):
        self.make_user()
        with self.assertRaises(CredentialsError):
            self.auth.login("manager01", "wrong-password-1")
        events = [row["event_type"] for row in self.auth.list_audit(tenant_id=TENANT)]
        self.assertIn("login_failed", events)

    def test_unknown_user_has_same_error_as_wrong_password(self):
        self.make_user()
        with self.assertRaises(CredentialsError) as unknown:
            self.auth.login("nobody", PASSWORD)
        with self.assertRaises(CredentialsError) as wrong:
            self.auth.login("manager01", "wrong-password-1")
        self.assertEqual(str(unknown.exception), str(wrong.exception))

    def test_lockout_after_repeated_failures(self):
        self.make_user()
        for _ in range(MAX_LOGIN_FAILURES):
            with self.assertRaises(CredentialsError):
                self.auth.login("manager01", "wrong-password-1")
        with self.assertRaises(AccountLocked):
            self.auth.login("manager01", PASSWORD)      # 锁定后即使口令正确也拒绝
        status = self.auth.lock_status("manager01")
        self.assertEqual(status["fail_count"], MAX_LOGIN_FAILURES)
        self.assertIsNotNone(status["locked_until"])
        self.assertTrue(any(row["event_type"] == "login_locked" for row in self.auth.list_audit()))

    def test_successful_login_resets_failures(self):
        self.make_user()
        with self.assertRaises(CredentialsError):
            self.auth.login("manager01", "wrong-password-1")
        self.auth.login("manager01", PASSWORD)
        self.assertEqual(self.auth.lock_status("manager01")["fail_count"], 0)

    def test_membership_scoping(self):
        self.make_user(store=STORE)
        with self.assertRaises(CredentialsError):
            self.auth.login("manager01", PASSWORD, store_id=STORE_B)
        multi = self.auth.create_user("multi01", PASSWORD, "多店店长", "manager", TENANT, STORE)
        self.auth.create_user  # noqa: B018 - 保持行可见性
        with self.auth._connection() as conn:
            conn.execute(
                "insert into memberships(tenant_id,store_id,user_id,role,status,created_at) values(?,?,?,?,?,?)",
                (TENANT, STORE_B, multi["user_id"], "manager", "active", datetime.now().isoformat(timespec="seconds")),
            )
        with self.assertRaises(AuthError):
            self.auth.login("multi01", PASSWORD)
        result = self.auth.login("multi01", PASSWORD, store_id=STORE_B)
        self.assertEqual(result["context"].store_id, STORE_B)

    def test_audit_records_login_success(self):
        self.make_user()
        self.auth.login("manager01", PASSWORD, actor_ip="127.0.0.1")
        events = self.auth.list_audit(event_type="login_success")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["store_id"], STORE)


class SessionTests(Base):
    def test_token_is_stored_as_hash_only(self):
        self.make_user()
        token = self.auth.login("manager01", PASSWORD)["token"]
        raw = self.db.read_bytes()
        self.assertNotIn(token.encode("utf-8"), raw)
        with self.auth._connection() as conn:
            stored = conn.execute("select token_hash from auth_sessions").fetchone()["token_hash"]
        self.assertEqual(stored, hash_token(token))

    def test_logout_revokes_session(self):
        self.make_user()
        token = self.auth.login("manager01", PASSWORD)["token"]
        self.assertTrue(self.auth.logout(token))
        with self.assertRaises(SessionInvalid):
            self.auth.resolve(token)
        self.assertFalse(self.auth.logout(token))

    def test_expired_and_unknown_sessions(self):
        self.make_user()
        short = AuthStore(self.db, session_ttl_hours=-1)
        token = short.login("manager01", PASSWORD)["token"]
        with self.assertRaises(SessionInvalid):
            self.auth.resolve(token)
        with self.assertRaises(SessionInvalid):
            self.auth.resolve("not-a-real-token")
        with self.assertRaises(SessionInvalid):
            self.auth.resolve("")

    def test_sliding_renewal_extends_soon_to_expire_session(self):
        self.make_user()
        token = self.auth.login("manager01", PASSWORD)["token"]
        soon = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        with self.auth._connection() as conn:
            conn.execute("update auth_sessions set expires_at=? where token_hash=?", (soon, hash_token(token)))
        context = self.auth.resolve(token)
        self.assertGreater(context.expires_at, soon)

    def test_password_change_revokes_sessions(self):
        user = self.make_user()
        token = self.auth.login("manager01", PASSWORD)["token"]
        self.auth.set_password(user["user_id"], "Brand-New-Pass-2026")
        with self.assertRaises(SessionInvalid):
            self.auth.resolve(token)
        self.auth.login("manager01", "Brand-New-Pass-2026")

    def test_revoke_expired_and_active_sessions_listing(self):
        self.make_user()
        token = self.auth.login("manager01", PASSWORD)["token"]
        context = self.auth.resolve(token)
        active = self.auth.active_sessions(context.user_id)
        self.assertEqual(len(active), 1)
        self.assertIsNone(active[0]["token_hash"])          # 列表不回传哈希
        self.assertEqual(active[0]["session_id"], context.session_id)
        with self.auth._connection() as conn:
            conn.execute("update auth_sessions set expires_at='2000-01-01T00:00:00'")
        self.assertEqual(self.auth.revoke_expired(), 1)


class RbacTests(unittest.TestCase):
    def test_role_matrix(self):
        expected = {
            "owner": {"config:write", "store:manage", "audit:read", "tasks:verify", "metrics:read:financial"},
            "area_manager": {"audit:read", "tasks:verify", "metrics:read:financial"},
            "manager": {"tasks:verify", "pricing:draft", "metrics:read:financial", "report:read"},
            "operator": {"pricing:draft", "metrics:read:financial", "quality:read"},
            "employee": {"metrics:read", "tasks:read:own", "tasks:accept", "tasks:evidence:submit"},
        }
        for role, permissions in expected.items():
            with self.subTest(role=role):
                for permission in permissions:
                    self.assertTrue(has_permission(role, permission), f"{role} 应有 {permission}")
        for forbidden in ("metrics:read:financial", "quality:read", "report:read", "tasks:verify", "audit:read"):
            with self.subTest(permission=forbidden):
                self.assertFalse(has_permission("employee", forbidden), f"employee 不应有 {forbidden}")
        self.assertFalse(has_permission("operator", "tasks:verify"))
        self.assertFalse(has_permission("manager", "config:write"))
        self.assertEqual(set(ROLE_PERMISSIONS), set(ROLES))
        for role in ROLES:
            self.assertIn("agent:chat", permissions_for(role))

    def test_context_require_raises(self):
        context = SecurityContext("u1", TENANT, STORE, "employee", "员工小李",
                                  permissions=permissions_for("employee"))
        context.require("tasks:read:own")
        with self.assertRaises(PermissionError):
            context.require("tasks:verify")
        self.assertEqual(context.to_user_dict()["name"], "员工小李")

    def test_unknown_role_rejected(self):
        with self.assertRaises(AuthError):
            permissions_for("superadmin")


class RedactionTests(unittest.TestCase):
    def test_phone_and_id_card_are_masked(self):
        self.assertEqual(redact("联系 13812345678 与 13900001111"), "联系 138****5678 与 139****1111")
        self.assertEqual(redact("身份证 420106199001011234"), "身份证 420106********1234")
        self.assertEqual(redact(None), "")

    def test_payload_secrets_are_masked_recursively(self):
        payload = {
            "username": "manager01",
            "password": "ShiZiZuo-2026-store",
            "nested": {"api_key": "sk-abc", "phone": "13812345678"},
            "items": [{"token": "t-1", "note": "顾客 13812345678 反馈"}],
        }
        cleaned = redact_payload(payload)
        self.assertEqual(cleaned["password"], "***")
        self.assertEqual(cleaned["nested"]["api_key"], "***")
        self.assertEqual(cleaned["nested"]["phone"], "138****5678")
        self.assertEqual(cleaned["items"][0]["token"], "***")
        self.assertIn("138****5678", cleaned["items"][0]["note"])
        self.assertNotIn("ShiZiZuo-2026-store", str(cleaned))

    def test_audit_payload_is_redacted(self):
        temp = tempfile.TemporaryDirectory()
        try:
            auth = AuthStore(Path(temp.name) / "auth.db")
            auth.record_audit(
                event_type="test_event", actor_type="user", actor_id="u1",
                object_type="customer", object_id="13812345678",
                after={"password": "secret-value", "phone": "13812345678"},
            )
            row = auth.list_audit(limit=1)[0]
            self.assertEqual(row["object_id"], "138****5678")
            self.assertNotIn("secret-value", row["after_json"])
            self.assertIn("138****5678", row["after_json"])
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
