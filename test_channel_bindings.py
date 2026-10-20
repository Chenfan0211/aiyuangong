# -*- coding: utf-8 -*-
"""渠道绑定仓储（AI 店长第一阶段）测试。

覆盖：建表幂等、正常绑定、重复外部 ID、重复手机号、非法手机号、脱敏、
禁用后不可 resolve、跨门店不可 resolve、缺会员不可 resolve，以及 SQLite/PG 双引擎。

运行：python -m unittest -v test_channel_bindings.py
"""
import hashlib
import hmac
import os
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from auth import AuthStore
from channel_store import (
    ChannelBindingError,
    ChannelBindingStore,
    mask_mobile,
    mobile_hash,
    normalize_mobile,
)

TENANT = "LPL-001"
TENANT_B = "LPL-002"
STORE = "WH-LION-001"
STORE_B = "WH-LION-002"
STORE_C = "WH-LION-003"
PROVIDER = "wecom"
EXTERNAL_ID = "ext-1"
PASSWORD = "ShiZiZuo-2026-store"

EXPECTED_COLUMNS = {
    "binding_id", "tenant_id", "store_id", "provider", "external_user_id", "display_name",
    "mobile_hash", "mobile_masked", "user_id", "status", "created_by", "created_at", "updated_at",
}

try:
    import pgserver
    import psycopg
    HAS_PG = True
except ImportError:                                        # pragma: no cover - 环境相关
    HAS_PG = False


class MobileHelperTests(unittest.TestCase):
    """normalize_mobile / mask_mobile / mobile_hash 纯函数测试。"""

    def test_normalize_accepts_common_china_forms(self):
        expected = "13800138000"
        for raw in ("13800138000", "138 0013 8000", "138-0013-8000", "+86 13800138000",
                    "8613800138000", "  +86-138-0013-8000  "):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_mobile(raw), expected)

    def test_normalize_rejects_invalid_numbers(self):
        for raw in ("", "   ", None, "1380013800", "138001380000", "12800138000",
                    "13800138000a", "+861380013800", "abc", "１３８００１３８０００"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    normalize_mobile(raw)

    def test_mask_hides_middle_four_digits(self):
        self.assertEqual(mask_mobile("13800138000"), "138****8000")
        self.assertEqual(mask_mobile("8613800138000"), "138****8000")
        self.assertEqual(mask_mobile("+86 138-0013-8000"), "138****8000")
        with self.assertRaises(ValueError):
            mask_mobile("12345")

    def test_hash_is_deterministic_and_env_keyed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            first = mobile_hash("13800138000")
            second = mobile_hash("138 0013 8000")
            self.assertEqual(first, second)                 # 同号不同写法必须同哈希
            self.assertEqual(len(first), 64)
            self.assertNotIn("13800138000", first)          # 不直接暴露明文

        with mock.patch.dict(os.environ, {"STORE_AGENT_MOBILE_HASH_KEY": "unit-key"}, clear=True):
            keyed = mobile_hash("13800138000")
            expected = hmac.new(b"unit-key", b"13800138000", hashlib.sha256).hexdigest()
            self.assertEqual(keyed, expected)

        with mock.patch.dict(os.environ, {"STORE_AGENT_INBOUND_SIGNING_SECRET": "inbound-secret"},
                             clear=True):
            fallback = mobile_hash("13800138000")
            expected = hmac.new(b"inbound-secret", b"13800138000", hashlib.sha256).hexdigest()
            self.assertEqual(fallback, expected)

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotEqual(mobile_hash("13800138000"), keyed)   # 无密钥≠有密钥


class BindingStoreBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "ops.db"
        self.auth = AuthStore(self.db, tenant_id=TENANT)
        self.bindings = ChannelBindingStore(self.db, tenant_id=TENANT)

    def tearDown(self):
        self.temp.cleanup()

    def member(self, username="owner01", role="owner", store_id=STORE, tenant_id=TENANT):
        return self.auth.create_user(username, PASSWORD, "店长", role, tenant_id, store_id)

    def bind(self, **overrides):
        payload = dict(tenant_id=TENANT, store_id=STORE, provider=PROVIDER,
                       external_user_id=EXTERNAL_ID, display_name="张三",
                       mobile="13800138000", created_by="owner01")
        payload.update(overrides)
        return self.bindings.create(**payload)

    def raw_rows(self):
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("select * from channel_bindings")]
        finally:
            conn.close()


class ChannelBindingStoreTests(BindingStoreBase):
    def test_table_creation_is_idempotent(self):
        created = self.bind()
        again = ChannelBindingStore(self.db, tenant_id=TENANT)          # 二次建表不报错
        ChannelBindingStore(self.db, tenant_id=TENANT)
        self.assertEqual([row["binding_id"] for row in again.list(TENANT)], [created["binding_id"]])

        conn = sqlite3.connect(str(self.db))
        try:
            columns = {row[1] for row in conn.execute("pragma table_info(channel_bindings)")}
        finally:
            conn.close()
        self.assertTrue(EXPECTED_COLUMNS <= columns)
        self.assertNotIn("mobile", columns)                             # 不存在明文手机号列

    def test_create_returns_masked_phone_only(self):
        row = self.bind(mobile="138 0013 8000")
        self.assertEqual(row["mobile_masked"], "138****8000")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["display_name"], "张三")
        self.assertTrue(row["binding_id"].startswith("cb-"))
        self.assertNotIn("mobile", row)
        self.assertNotIn("mobile_hash", row)

        stored = self.raw_rows()[0]
        self.assertEqual(stored["mobile_masked"], "138****8000")
        self.assertEqual(len(stored["mobile_hash"]), 64)
        self.assertNotIn("13800138000", stored["mobile_hash"] + stored["mobile_masked"])

    def test_duplicate_external_user_id_is_rejected(self):
        self.bind(external_user_id="ext-1")
        with self.assertRaises(ChannelBindingError) as error:
            self.bind(external_user_id="ext-1", mobile="13900139000")
        self.assertIn("已绑定", str(error.exception))
        self.assertIsInstance(error.exception, ValueError)

    def test_duplicate_mobile_in_same_scope_is_rejected(self):
        self.bind(external_user_id="ext-1", mobile="13800138000")
        with self.assertRaises(ChannelBindingError):
            self.bind(external_user_id="ext-2", mobile="138-0013-8000")   # 同号不同写法
        other = self.bind(external_user_id="ext-2", store_id=STORE_B, mobile="13800138000")
        self.assertEqual(other["mobile_masked"], "138****8000")           # 换门店允许

    def test_invalid_mobile_is_rejected_on_create(self):
        with self.assertRaises(ValueError):
            self.bind(mobile="12345")
        self.assertEqual(self.bindings.list(TENANT), [])                  # 失败不留脏数据

    def test_disable_makes_binding_unresolvable(self):
        owner = self.member()
        row = self.bind(user_id=owner["user_id"])
        self.assertIsNotNone(self.bindings.resolve(TENANT, STORE, PROVIDER, EXTERNAL_ID))

        disabled = self.bindings.disable(row["binding_id"])
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(disabled["binding_id"], row["binding_id"])
        self.assertEqual(self.bindings.disable(row["binding_id"])["status"], "disabled")
        self.assertIsNone(self.bindings.resolve(TENANT, STORE, PROVIDER, EXTERNAL_ID))

    def test_resolve_is_scoped_by_store_and_tenant(self):
        owner_a = self.member("owner01", store_id=STORE)
        owner_b = self.member("owner02", store_id=STORE_B)
        self.bind(store_id=STORE, external_user_id="ext-a", user_id=owner_a["user_id"])
        self.bind(store_id=STORE_B, external_user_id="ext-a", user_id=owner_b["user_id"])

        resolved_a = self.bindings.resolve(TENANT, STORE, PROVIDER, "ext-a")
        resolved_b = self.bindings.resolve(TENANT, STORE_B, PROVIDER, "ext-a")
        self.assertEqual(resolved_a["user_id"], owner_a["user_id"])
        self.assertEqual(resolved_b["user_id"], owner_b["user_id"])
        self.assertEqual(resolved_a["role"], "owner")
        self.assertEqual(resolved_a["mobile_masked"], "138****8000")
        self.assertNotIn("mobile", resolved_a)
        self.assertNotIn("mobile_hash", resolved_a)

        self.assertIsNone(self.bindings.resolve(TENANT, STORE_C, PROVIDER, "ext-a"))   # 跨门店
        self.assertIsNone(self.bindings.resolve(TENANT_B, STORE, PROVIDER, "ext-a"))   # 跨租户

    def test_resolve_requires_active_membership(self):
        # 1) 绑定未关联用户
        self.bind(external_user_id="ext-no-user", mobile=None, user_id=None)
        self.assertIsNone(self.bindings.resolve(TENANT, STORE, PROVIDER, "ext-no-user"))

        # 2) 引用不存在的用户（join users 命不中）
        self.bind(external_user_id="ext-orphan-user", mobile="13500135000", user_id="u-not-exist")
        self.assertIsNone(self.bindings.resolve(TENANT, STORE, PROVIDER, "ext-orphan-user"))

        # 3) 会员只存在于别的门店（本门店缺会员）
        other_store = self.member("owner02", store_id=STORE_B)
        self.bind(external_user_id="ext-other-store", mobile="13900139000",
                  user_id=other_store["user_id"])
        self.assertIsNone(self.bindings.resolve(TENANT, STORE, PROVIDER, "ext-other-store"))

        # 4) 会员记录被删除（缺会员）
        removed = self.member("owner03", store_id=STORE)
        self.bind(external_user_id="ext-missing-member", mobile="13700137000",
                  user_id=removed["user_id"])
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("delete from memberships where tenant_id=? and store_id=? and user_id=?",
                         (TENANT, STORE, removed["user_id"]))
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(self.bindings.resolve(TENANT, STORE, PROVIDER, "ext-missing-member"))

        # 5) 会员被停用
        disabled_member = self.member("owner04", store_id=STORE)
        self.bind(external_user_id="ext-disabled-member", mobile="13600136000",
                  user_id=disabled_member["user_id"])
        self.auth.update_membership(TENANT, STORE, disabled_member["user_id"], status="disabled")
        self.assertIsNone(self.bindings.resolve(TENANT, STORE, PROVIDER, "ext-disabled-member"))

        # 6) 用户被停用
        disabled_user = self.member("owner05", store_id=STORE)
        self.bind(external_user_id="ext-disabled-user", mobile="13400134000",
                  user_id=disabled_user["user_id"])
        self.auth.set_user_status(disabled_user["user_id"], "disabled")
        self.assertIsNone(self.bindings.resolve(TENANT, STORE, PROVIDER, "ext-disabled-user"))

    def test_resolve_requires_active_binding_status(self):
        owner = self.member()
        self.bind(user_id=owner["user_id"])
        self.bindings.update(self.bindings.list(TENANT)[0]["binding_id"], status="disabled")
        self.assertIsNone(self.bindings.resolve(TENANT, STORE, PROVIDER, EXTERNAL_ID))

    def test_update_changes_display_name_mobile_and_status(self):
        row = self.bind(external_user_id="ext-1")
        self.bind(external_user_id="ext-2", mobile="13900139000")

        updated = self.bindings.update(row["binding_id"], display_name="李四", mobile="13700137000")
        self.assertEqual(updated["display_name"], "李四")
        self.assertEqual(updated["mobile_masked"], "137****7000")
        self.assertTrue(updated["updated_at"])
        self.assertNotIn("mobile", updated)
        self.assertNotIn("mobile_hash", updated)

        with self.assertRaises(ChannelBindingError):
            self.bindings.update(row["binding_id"], mobile="13900139000")    # 撞号
        with self.assertRaises(ValueError):
            self.bindings.update(row["binding_id"], mobile="abc")            # 非法号

        self.assertEqual(self.bindings.update(row["binding_id"], status="disabled")["status"], "disabled")
        self.assertEqual(self.bindings.update(row["binding_id"], status="active")["status"], "active")
        with self.assertRaises(ValueError):
            self.bindings.update(row["binding_id"], status="frozen")
        with self.assertRaises(ChannelBindingError):
            self.bindings.update("cb-not-exist", display_name="x")
        with self.assertRaises(ChannelBindingError):
            self.bindings.disable("cb-not-exist")

    def test_list_is_scoped_by_tenant_and_store(self):
        self.bind(external_user_id="ext-1", store_id=STORE)
        self.bind(external_user_id="ext-2", store_id=STORE_B)
        self.bind(tenant_id=TENANT_B, store_id=STORE_C, external_user_id="ext-3")

        self.assertEqual({row["store_id"] for row in self.bindings.list(TENANT)}, {STORE, STORE_B})
        self.assertEqual([row["store_id"] for row in self.bindings.list(TENANT, STORE)], [STORE])
        self.assertEqual(self.bindings.list(TENANT_B, STORE_C)[0]["external_user_id"], "ext-3")
        self.assertEqual(self.bindings.list("LPL-999"), [])
        for row in self.bindings.list(TENANT):
            self.assertNotIn("mobile", row)
            self.assertNotIn("mobile_hash", row)


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能验证 PostgreSQL DSN 兼容性")
class ChannelBindingPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_channel_pg_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("drop schema public cascade; create schema public")

    def test_postgres_dsn_roundtrip_and_uniqueness(self):
        auth = AuthStore(self.dsn, tenant_id=TENANT)                     # 建 users/memberships
        store = ChannelBindingStore(self.dsn, tenant_id=TENANT)
        owner = auth.create_user("owner01", PASSWORD, "店长", "owner", TENANT, STORE)

        ChannelBindingStore(self.dsn, tenant_id=TENANT)                  # PG 建表同样幂等
        row = store.create(tenant_id=TENANT, store_id=STORE, provider=PROVIDER,
                           external_user_id=EXTERNAL_ID, display_name="张三",
                           mobile="138 0013 8000", user_id=owner["user_id"], created_by="owner01")
        self.assertEqual(row["mobile_masked"], "138****8000")
        self.assertNotIn("mobile_hash", row)

        resolved = store.resolve(TENANT, STORE, PROVIDER, EXTERNAL_ID)
        self.assertEqual(resolved["user_id"], owner["user_id"])
        self.assertEqual(resolved["role"], "owner")
        self.assertEqual(store.resolve(TENANT, STORE_B, PROVIDER, EXTERNAL_ID), None)

        with self.assertRaises(ChannelBindingError):
            store.create(tenant_id=TENANT, store_id=STORE, provider=PROVIDER,
                         external_user_id=EXTERNAL_ID, mobile="13900139000")
        with self.assertRaises(ChannelBindingError):
            store.create(tenant_id=TENANT, store_id=STORE, provider=PROVIDER,
                         external_user_id="ext-2", mobile="13800138000")

        store.disable(row["binding_id"])
        self.assertIsNone(store.resolve(TENANT, STORE, PROVIDER, EXTERNAL_ID))


if __name__ == "__main__":
    unittest.main()