# -*- coding: utf-8 -*-
"""P0 闸门（第 10 项）：备份、恢复与恢复演练。"""
import gzip
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from auth import AuthStore
from ops_backup import (
    BackupError,
    create_backup,
    drill,
    restore_backup,
    table_hash,
    verify_backup,
)
from platform_data import PlatformMetricStore
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
          "gmv,refunds,review_count,negative_review_count,avg_rating\n")

try:
    import pgserver
    import psycopg
    HAS_PG = True
except ImportError:                                   # pragma: no cover
    HAS_PG = False


def seed(db, workdir: Path, tenant: str = TENANT, store: str = STORE) -> None:
    """`db` 是 SQLite 路径或 PostgreSQL DSN；CSV 落在 workdir（不能把 DSN 当路径用）。"""
    metrics = PlatformMetricStore(db, tenant_id=tenant)
    for platform, base in (("meituan", 100), ("douyin", 80)):
        path = workdir / f"{platform}.csv"
        path.write_bytes((HEADER + "".join(
            f"{(date.today() - timedelta(days=offset)).isoformat()},{platform},{store},"
            f"{base},{base // 5},4,3,300,0,1,0,4.8\n" for offset in (1, 2))).encode("utf-8"))
        metrics.import_csv(path, store)
    tasks = TaskStore(db, tenant_id=tenant)
    tasks.save_plan({"date": date.today().isoformat(), "tasks": [{
        "task_id": "T-1", "category": "reputation", "title": "处理差评", "assignee": "店长",
        "priority": "high", "due_at": f"{date.today().isoformat()}T18:00:00", "target": {"a": 1},
        "evidence_required": ["脱敏订单号"], "reason": "r", "requires_approval": False}]}, store)
    auth = AuthStore(db, tenant_id=tenant)
    auth.create_user("manager01", "ShiZiZuo-2026-store", "店长", "manager", tenant, store)
    auth.record_audit(event_type="probe", actor_type="user", actor_id="manager01",
                      tenant_id=tenant, store_id=store, object_type="x", object_id="1")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "ops.db"
        seed(self.db, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_manifest_and_verify(self):
        manifest = create_backup(self.db, self.root / "b.json.gz", tenant_id=TENANT, store_id=STORE)
        self.assertGreaterEqual(manifest["table_count"], 5)
        self.assertGreater(manifest["row_total"], 0)
        self.assertTrue(Path(manifest["path"]).exists())
        result = verify_backup(self.root / "b.json.gz")
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["tables"]["action_tasks"], 1)
        self.assertIn("platform_metrics", result["tables"])
        # 明文备份也要能读
        plain = create_backup(self.db, self.root / "b.json", compress=False, tenant_id=TENANT, store_id=STORE)
        self.assertTrue(verify_backup(Path(plain["path"]))["ok"])

    def test_tampered_backup_is_detected(self):
        create_backup(self.db, self.root / "b.json", compress=False)
        payload = json.loads((self.root / "b.json").read_text(encoding="utf-8"))
        payload["tables"]["action_tasks"]["rows"][0]["title"] = "被改过的标题"
        (self.root / "b.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = verify_backup(self.root / "b.json")
        self.assertFalse(result["ok"])
        self.assertTrue(any("哈希不匹配" in problem for problem in result["problems"]))

    def test_truncated_backup_is_detected(self):
        create_backup(self.db, self.root / "b.json.gz")
        raw = gzip.decompress((self.root / "b.json.gz").read_bytes())
        payload = json.loads(raw.decode("utf-8"))
        payload["tables"]["action_tasks"]["rows"] = []                 # 行数与数据不符
        (self.root / "b.json.gz").write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8")))
        result = verify_backup(self.root / "b.json.gz")
        self.assertFalse(result["ok"])
        self.assertTrue(any("行数与数据不符" in problem or "哈希不匹配" in problem
                            for problem in result["problems"]))


    def test_restore_rejects_tampered_backup_before_writing(self):
        path = self.root / "tampered.json"
        create_backup(self.db, path, compress=False, tenant_id=TENANT, store_id=STORE)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["tables"]["action_tasks"]["rows"][0]["title"] = "tampered"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(BackupError):
            restore_backup(self.root / "target.db", path, force=True, tenant_id=TENANT, store_id=STORE)
    def test_restore_into_fresh_database(self):
        manifest = create_backup(self.db, self.root / "b.json.gz")
        target = self.root / "restored.db"
        result = restore_backup(target, self.root / "b.json.gz", force=True)
        self.assertEqual(result["row_total"], manifest["row_total"])
        expected = None
        payload = json.loads(gzip.decompress((self.root / "b.json.gz").read_bytes()).decode("utf-8"))
        expected = payload["tables"]["platform_metrics"]
        with PlatformMetricStore(target, tenant_id=TENANT).db.connection() as conn:
            rows = [dict(r) for r in conn.execute("select * from platform_metrics").fetchall()]
        self.assertEqual(len(rows), expected["row_count"])
        self.assertEqual(table_hash(rows), expected["sha256"])       # 内容逐行一致

    def test_restore_refuses_non_empty_database(self):
        create_backup(self.db, self.root / "b.json.gz")
        with self.assertRaises(BackupError) as error:
            restore_backup(self.db, self.root / "b.json.gz")
        self.assertIn("--force", str(error.exception))
        result = restore_backup(self.db, self.root / "b.json.gz", force=True)
        self.assertGreater(result["row_total"], 0)

    def test_missing_or_broken_backup_file(self):
        with self.assertRaises(BackupError):
            verify_backup(self.root / "nope.json.gz")
        (self.root / "broken.json.gz").write_bytes(b"not json")
        with self.assertRaises(BackupError):
            verify_backup(self.root / "broken.json.gz")

    def test_drill_passes_and_reports_tables(self):
        report = drill(self.db, self.root / "drill")
        self.assertTrue(report["ok"], report.get("mismatches"))
        self.assertEqual(report["stage"], "compare")
        self.assertGreaterEqual(report["checked_tables"], 5)
        self.assertEqual(report["mismatches"], [])
        self.assertTrue(report["verification"]["ok"])
        self.assertTrue(list((self.root / "drill").glob("backup-*.json.gz")))


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能验证 PostgreSQL 上的备份恢复演练")
class PgBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_bk_pg_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        seed(self.dsn, self.tmp, tenant=TENANT, store=STORE)

    def test_drill_on_postgres_uses_fresh_database(self):
        report = drill(self.dsn, self.tmp / "drill", tenant_id=TENANT, store_id=STORE)
        self.assertTrue(report["ok"], report.get("mismatches"))
        self.assertGreaterEqual(report["checked_tables"], 5)
        # 演练用的临时数据库必须被清理掉，且源库数据不受影响
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("select count(*) from pg_database where datname like 'aiyg_drill_%'")
            self.assertEqual(int(cur.fetchone()[0]), 0)
            cur.execute("select count(*) from platform_metrics")
            self.assertEqual(int(cur.fetchone()[0]), 4)

    def test_restore_then_insert_does_not_collide(self):
        from ops_backup import create_backup as backup
        backup_path = self.tmp / "pg.json.gz"
        backup(self.dsn, backup_path, tenant_id=TENANT, store_id=STORE)
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        result = restore_backup(self.dsn, backup_path, force=True, tenant_id=TENANT, store_id=STORE)
        self.assertGreater(result["row_total"], 0)
        auth = AuthStore(self.dsn, tenant_id=TENANT)
        auth.record_audit(event_type="after_restore", actor_type="user", actor_id="manager01",
                          tenant_id=TENANT, store_id=STORE, object_type="x", object_id="2")
        events = [row["event_type"] for row in auth.list_audit(store_id=STORE, limit=10)]
        self.assertIn("after_restore", events)


if __name__ == "__main__":
    unittest.main()
