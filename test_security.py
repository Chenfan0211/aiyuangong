# -*- coding: utf-8 -*-
"""P0 闸门（第 4/5/7 项）：Webhook 验签与重放防护、凭据轮换、危险能力与密钥自检。"""
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from security import (
    ALLOWED_SECRET_VALUES,
    ReplayGuard,
    check_postgres_hardening,
    gate_report,
    new_nonce,
    rotate_pg_role_password,
    rotation_checklist,
    scan_dangerous_capabilities,
    scan_secrets,
    sign_webhook,
    verify_inbound,
    verify_signature,
)

ROOT = Path(__file__).resolve().parent
SECRET = "webhook-secret-2026"          # noqa: secret - 测试夹具

try:
    import pgserver
    import psycopg
    HAS_PG = True
except ImportError:                                   # pragma: no cover
    HAS_PG = False


class SignatureTests(unittest.TestCase):
    def test_sign_and_verify_roundtrip(self):
        body = b'{"event":"approval"}'
        signed = sign_webhook(SECRET, body, timestamp=1_700_000_000)
        result = verify_signature(SECRET, body, signed["signature"], signed["timestamp"],
                                  now=1_700_000_010)
        self.assertTrue(result["ok"], result["reason"])

    def test_wrong_secret_and_tampered_body_are_rejected(self):
        body = b'{"event":"approval"}'
        signed = sign_webhook(SECRET, body, timestamp=int(time.time()))
        self.assertFalse(verify_signature("other-secret", body, signed["signature"], signed["timestamp"])["ok"])
        self.assertFalse(verify_signature(SECRET, b'{"event":"changed"}', signed["signature"],
                                          signed["timestamp"])["ok"])
        self.assertFalse(verify_signature(SECRET, body, "deadbeef", signed["timestamp"])["ok"])

    def test_timestamp_window_and_bad_timestamp(self):
        body = b"payload"
        now = int(time.time())
        old = sign_webhook(SECRET, body, timestamp=now - 400)
        future = sign_webhook(SECRET, body, timestamp=now + 400)
        self.assertIn("超出容忍窗口", verify_signature(SECRET, body, old["signature"], old["timestamp"], now=now)["reason"])
        self.assertFalse(verify_signature(SECRET, body, future["signature"], future["timestamp"], now=now)["ok"])
        self.assertIn("timestamp 非法", verify_signature(SECRET, body, "x", "abc")["reason"])


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ops.db"
        self.guard = ReplayGuard(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_nonce_is_consumed_once(self):
        nonce = new_nonce()
        self.assertTrue(self.guard.consume(nonce, "feishu"))
        self.assertFalse(self.guard.consume(nonce, "feishu"))
        self.assertTrue(self.guard.consume(new_nonce(), "feishu"))
        self.assertEqual(self.guard.count(), 2)
        self.assertFalse(self.guard.consume("", "feishu"))          # 空 nonce 直接拒绝

    def test_verify_inbound_handles_replay_and_does_not_consume_on_bad_signature(self):
        body = b'{"action":"approve"}'
        now = int(time.time())
        signed = sign_webhook(SECRET, body, timestamp=now)
        nonce = new_nonce()
        first = verify_inbound(SECRET, body, signed["signature"], signed["timestamp"], nonce,
                               self.guard, now=now)
        self.assertTrue(first["ok"])
        replay = verify_inbound(SECRET, body, signed["signature"], signed["timestamp"], nonce,
                                self.guard, now=now)
        self.assertFalse(replay["ok"])
        self.assertIn("重放", replay["reason"])

        other = new_nonce()
        bad = verify_inbound(SECRET, body, "wrong-signature", signed["timestamp"], other,
                             self.guard, now=now)
        self.assertFalse(bad["ok"])
        # 验签失败不得占用 nonce（否则攻击者可以用坏签名把合法请求挤掉）
        self.assertTrue(self.guard.consume(other, "test"))

    def test_prune_removes_old_nonces(self):
        with self.guard._connection() as conn:
            conn.execute("insert into webhook_nonces(nonce,source,received_at) values(?,?,?)",
                         ("old-nonce", "feishu", "2000-01-01T00:00:00"))
        self.guard.consume(new_nonce(), "feishu")
        removed = self.guard.prune(hours=24)
        self.assertEqual(removed, 1)
        self.assertEqual(self.guard.count(), 1)


class RotationTests(unittest.TestCase):
    def test_checklist_covers_all_credentials(self):
        steps = rotation_checklist()
        self.assertGreaterEqual(len(steps), 5)
        targets = " ".join(step["target"] for step in steps)
        for keyword in ("PostgreSQL", "API Key", "Webhook", "口令", "备份"):
            self.assertIn(keyword, targets)
        for step in steps:
            self.assertTrue(step["action"] and step["verify"], step)
        self.assertTrue(any(step["automated"] for step in steps))

    def test_role_password_validation(self):
        with self.assertRaises(ValueError):
            rotate_pg_role_password("postgresql://x/y", "bad role!", "long-enough-pass")
        with self.assertRaises(ValueError):
            rotate_pg_role_password("postgresql://x/y", "store_app", "short")


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_dangerous_capabilities_are_flagged(self):
        self.write("runtime_bad.py", "import subprocess\nsubprocess.run(['ls'])\n")
        self.write("runtime_clean.py", "value = 1 + 1\n")
        result = scan_dangerous_capabilities(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["findings"]), 1)
        self.assertIn("runtime_bad.py", result["findings"][0]["file"])
        self.assertEqual(result["findings"][0]["line"], 2)

    def test_noqa_line_is_exempt(self):
        self.write("runtime_ok.py", "path.unlink()  # noqa: dangerous - 只删本次演练自己的文件\n")
        self.assertTrue(scan_dangerous_capabilities(self.root)["ok"])

    def test_test_files_are_not_scanned_for_capabilities(self):
        self.write("test_uses_subprocess.py", "import subprocess\nsubprocess.run(['pytest'])\n")
        self.assertTrue(scan_dangerous_capabilities(self.root)["ok"])

    def test_secret_scanning(self):
        self.write("cfg_bad.py", 'API_KEY = "sk-abcdef0123456789abcdef"\n')  # noqa: secret - 夹具
        result = scan_secrets(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("sk-", result["findings"][0]["snippet"])

    def test_secret_scan_allows_empty_and_known_fixtures(self):
        self.write("cfg_ok.py", 'PASSWORD = ""\nTOKEN = "<口令>"\n')
        self.write("test_fixture.py", 'PASSWORD = "ShiZiZuo-2026-store"\n')
        self.assertTrue(scan_secrets(self.root)["ok"])
        self.assertIn("ShiZiZuo-2026-store", ALLOWED_SECRET_VALUES)

    def test_secret_scan_noqa_exempt(self):
        self.write("cfg_ok.py", 'TOKEN = "sk-abcdef0123456789abcdef"  # noqa: secret - 文档示例\n')  # noqa: secret
        self.assertTrue(scan_secrets(self.root)["ok"])

    def test_real_project_is_clean(self):
        """对真实项目跑一遍自检——这是闸门第 4/5 项的可复现证据。"""
        capabilities = scan_dangerous_capabilities(ROOT)
        secrets_result = scan_secrets(ROOT)
        self.assertTrue(capabilities["ok"], capabilities["findings"])
        self.assertTrue(secrets_result["ok"], secrets_result["findings"])
        report = gate_report(ROOT)
        self.assertTrue(report["ok"], report["items"])


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能验证 PostgreSQL 加固检查与角色轮换")
class PgHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_sec_pg_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def _with_role(dsn: str, role: str, password: str) -> str:
        parts = urlsplit(dsn)
        netloc = f"{role}:{password}@{parts.hostname}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def test_owner_connection_is_flagged(self):
        from auth import AuthStore
        AuthStore(self.dsn, tenant_id="LPL-001")                     # 建表并开启 RLS
        result = check_postgres_hardening(self.dsn)
        self.assertFalse(result["ok"])
        self.assertTrue(any("所有者" in problem or "超级用户" in problem for problem in result["problems"]))
        self.assertGreater(result["rls_tables"], 0)

    def test_restricted_role_passes_and_rotation_works(self):
        from auth import AuthStore
        AuthStore(self.dsn, tenant_id="LPL-001")
        role, old_password, new_password = "store_app", "First-App-Pass-2026", "Second-App-Pass-2026"
        with psycopg.connect(self.dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f"drop role if exists {role}")
            cur.execute(f"create role {role} login password '{old_password}'")
            cur.execute(f"grant usage on schema public to {role}")
            cur.execute(f"grant select on all tables in schema public to {role}")
        restricted = self._with_role(self.dsn, role, old_password)
        result = check_postgres_hardening(restricted)
        self.assertTrue(result["ok"], result["problems"])

        def stored_password(conn) -> str:
            with conn.cursor() as cur:
                cur.execute("select rolpassword from pg_authid where rolname=%s", (role,))
                return str(cur.fetchone()[0])

        with psycopg.connect(self.dsn) as conn:
            before = stored_password(conn)
        rotate_pg_role_password(self.dsn, role, new_password)
        with psycopg.connect(self.dsn) as conn:
            after = stored_password(conn)
        self.assertNotEqual(before, after, "轮换后 catalog 里的口令哈希必须变化")
        # 新口令可以连上（注意：本地 pgserver 是 trust 认证，口令不参与校验，
        # 因此"旧口令连不上"这一条只能在 md5/scram 认证的生产环境验证——已在 README 与轮换清单里注明）
        with psycopg.connect(self._with_role(self.dsn, role, new_password)) as conn, conn.cursor() as cur:
            cur.execute("select current_user")
            self.assertEqual(cur.fetchone()[0], role)

    def test_gate_report_with_dsn(self):
        from auth import AuthStore
        AuthStore(self.dsn, tenant_id="LPL-001")
        report = gate_report(ROOT, dsn=self.dsn)
        names = [item["gate"] for item in report["items"]]
        self.assertIn("PostgreSQL RLS 与应用角色", names)
        self.assertFalse(report["ok"])                               # 用所有者连接必须被判不通过
        pg_item = next(item for item in report["items"] if item["gate"].startswith("PostgreSQL"))
        self.assertFalse(pg_item["ok"])


if __name__ == "__main__":
    unittest.main()
