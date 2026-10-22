# -*- coding: utf-8 -*-
"""生产化批次：数据库迁移版本化（需要 psycopg + pgserver，缺失时跳过）。

覆盖：空库、重复 apply、dry-run 只读、并发 advisory lock、失败中断（事务回滚不回滚半成品）、
状态查询、verify（版本/校验和/表结构），以及 apply 前的“可恢复保障”（恢复点清单）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import pgserver
    import psycopg
    HAS_PG = True
except ImportError:                                  # pragma: no cover - 环境相关
    HAS_PG = False

if HAS_PG:
    import pg_sync

STORE_CONFIG = ROOT / "store_config.json"

# 基线迁移（v1）完成后必须存在的表：schema.sql A 段落地表 + 迁移账本两张表
CORE_TABLES = {
    "stores", "store_configs", "platform_metrics", "package_metrics", "reviews",
    "metric_import_batches", "action_tasks", "task_evidence", "task_events", "users",
    "memberships", "channel_bindings", "auth_sessions", "login_attempts", "audit_events", "agent_sessions",
    "agent_messages", "approvals", "agent_tool_runs", "notifications",
    # 第 1 期：门店侧经营数据
    "store_traffic_daily", "time_slot_metrics", "inventory_items", "inventory_alerts",
    "trade_areas", "ops_import_batches",
    # 第 2 期：竞品监控
    "competitor_stores", "competitor_offers", "competitor_matches",
    # 第 3 期：规则库
    "action_rules",
    "schema_migrations", "schema_migration_lock",
}
# 迁移账本表：属于迁移基础设施，不算业务结构
BOOKKEEPING_TABLES = {"schema_migrations", "schema_migration_lock"}


@unittest.skipUnless(HAS_PG, "需要 psycopg + pgserver（本地 PostgreSQL）才能验证迁移版本化")
class MigrationVersioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_migtest_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        """每个用例从空 schema 开始，避免用例之间相互污染。"""
        self.recovery_dir = self.tmp / "recovery"
        with psycopg.connect(self.dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")

    # ---------------- 工具 ----------------

    def run_sync(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "pg_sync.py", "--dsn", self.dsn, "--config", str(STORE_CONFIG), *args],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)

    def run_migrate(self, mode, *extra):
        return self.run_sync("migrate", mode, "--recovery-dir", str(self.recovery_dir), *extra)

    def pg_tables(self):
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("select table_name from information_schema.tables where table_schema='public'")
            return {row[0] for row in cur.fetchall()}

    def pg_scalar(self, sql, params=()):
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]

    def applied_at(self, version):
        return self.pg_scalar("select applied_at from schema_migrations where version = %s", (version,))

    # ---------------- schema.sql 落地 ----------------

    def test_init_creates_migration_bookkeeping_tables(self):
        """init 仍是“创建全部基础表”的兼容命令，账本表随 init 一起落地（不写版本行）。"""
        result = self.run_sync("init")
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        tables = self.pg_tables()
        self.assertTrue(BOOKKEEPING_TABLES <= tables, BOOKKEEPING_TABLES - tables)
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("""select column_name from information_schema.columns
                           where table_schema = 'public' and table_name = 'schema_migrations'""")
            columns = {row[0] for row in cur.fetchall()}
        self.assertTrue({"version", "name", "checksum", "applied_at", "adopted", "applied_by"} <= columns,
                        columns)
        self.assertEqual(self.pg_scalar("select count(*) from schema_migrations"), 0)

    # ---------------- status / dry-run ----------------

    def test_status_on_empty_database_reports_baseline_pending(self):
        result = self.run_sync("migrate", "--status")
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "status")
        self.assertEqual(payload["current_version"], 0)
        self.assertEqual(payload["expected_version"], pg_sync.MIGRATIONS[-1].version)
        self.assertEqual([item["version"] for item in payload["pending"]], [item.version for item in pg_sync.MIGRATIONS])
        self.assertFalse(payload["up_to_date"])
        self.assertFalse(payload["locked"])
        self.assertEqual([item["version"] for item in payload["applied"]], [])
        self.assertEqual(self.pg_tables(), set(), "status 必须只读，不得改库")

    def test_dry_run_does_not_modify_database(self):
        result = self.run_sync("migrate", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["would_write"])
        self.assertEqual([item["version"] for item in payload["planned"]], [item.version for item in pg_sync.MIGRATIONS])
        self.assertEqual(payload["planned"][0]["action"], "apply")
        self.assertGreater(payload["planned"][0]["statements"], 20)
        self.assertIn("stores", payload["planned"][0]["tables"])
        self.assertIn("schema_migrations", payload["planned"][0]["tables"])
        self.assertEqual(self.pg_tables(), set(), "dry-run 不得改库")

    def test_dry_run_after_init_reports_baseline_adoption(self):
        self.run_sync("init")
        payload = json.loads(self.run_sync("migrate", "--dry-run").stdout)
        self.assertEqual(payload["planned"][0]["action"], "adopt")
        self.assertEqual(payload["planned"][0]["statements"], 0)
        self.assertEqual(payload["current_version"], 0, "init 不写版本行")

    # ---------------- apply ----------------

    def test_apply_bootstraps_empty_database_and_records_version(self):
        result = self.run_migrate("--apply")
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-800:])
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "apply")
        self.assertEqual([item["version"] for item in payload["applied_now"]], [item.version for item in pg_sync.MIGRATIONS])
        self.assertFalse(payload["applied_now"][0]["adopted"])
        self.assertEqual(payload["current_version"], pg_sync.MIGRATIONS[-1].version)
        self.assertTrue(payload["up_to_date"])
        self.assertTrue(CORE_TABLES <= self.pg_tables(), CORE_TABLES - self.pg_tables())
        self.assertEqual(self.pg_scalar("select count(*) from schema_migrations"), len(pg_sync.MIGRATIONS))
        self.assertTrue(self.pg_scalar("select applied_by <> '' from schema_migrations where version = 1"))
        # 恢复点清单必须在改动数据库之前写好
        manifest = Path(payload["recovery"]["manifest"])
        self.assertTrue(manifest.exists(), payload["recovery"])
        self.assertEqual(payload["recovery"]["strategy"], "transactional_ddl")
        record = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(record["target_version"], pg_sync.MIGRATIONS[-1].version)
        self.assertEqual(record["sha256"], payload["recovery"]["sha256"])
        self.assertIn("schema_fingerprint", record)
        self.assertEqual(record["inventory"]["tables"], {})
        # 迁移锁账本记录了持锁人，并在结束时释放
        self.assertEqual(self.pg_scalar("select count(*) from schema_migration_lock"), 1)
        self.assertIsNotNone(self.pg_scalar("select released_at from schema_migration_lock"))

    def test_apply_is_idempotent(self):
        first = json.loads(self.run_migrate("--apply").stdout)
        stamp = self.applied_at(1)
        second = self.run_migrate("--apply")
        self.assertEqual(second.returncode, 0, second.stderr[-800:])
        payload = json.loads(second.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["applied_now"], [])
        self.assertTrue(payload["up_to_date"])
        self.assertIsNone(payload["recovery"], "无待执行迁移时不写恢复点")
        self.assertEqual(self.pg_scalar("select count(*) from schema_migrations"), len(pg_sync.MIGRATIONS))
        self.assertEqual(self.applied_at(1), stamp, "重复 apply 不得刷新已应用版本")
        self.assertEqual([item["version"] for item in first["applied"]], [item.version for item in pg_sync.MIGRATIONS])

    # ---------------- 第 2 期：竞品监控迁移（v4） ----------------

    COMPETITOR_TABLES = ("competitor_stores", "competitor_offers", "competitor_matches")
    OPS_BATCH_INSERT = (
        "insert into ops_import_batches "
        "(store_id, source_name, checksum, rows_read, rows_written, min_date, max_date, status, "
        " imported_at, dataset) "
        "values ('WH-LION-001', 'x.csv', %s, 1, 1, '2026-10-01', '2026-10-01', 'success', now(), %s)"
    )

    def check_constraint_name(self, table, column):
        return self.pg_scalar(
            """select con.conname from pg_constraint con
               join pg_class rel on rel.oid = con.conrelid
               join pg_attribute att on att.attrelid = rel.oid and att.attnum = any(con.conkey)
               where rel.relname = %s and att.attname = %s and con.contype = 'c'""",
            (table, column))

    def test_v4_creates_competitor_tables_with_rls_and_verifies(self):
        """v4 必须自洽：既有库执行 migrate --apply 后三张表已存在，且租户隔离策略一并落地。"""
        result = self.run_migrate("--apply")
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-800:])
        tables = self.pg_tables()
        self.assertTrue(set(self.COMPETITOR_TABLES) <= tables, set(self.COMPETITOR_TABLES) - tables)
        for table in self.COMPETITOR_TABLES:
            self.assertTrue(
                self.pg_scalar("select relrowsecurity from pg_class where relname = %s", (table,)),
                f"{table} 未开启 RLS",
            )
        # 新库侧也必须是同一个显式约束名（与"既有库迁移后"收敛）
        self.assertEqual(self.check_constraint_name("ops_import_batches", "dataset"),
                         "ck_ops_import_batches_dataset")
        verify = json.loads(self.run_migrate("--verify").stdout)
        self.assertTrue(verify["ok"], verify)
        self.assertEqual(verify["missing_tables"], [])
        self.assertEqual(verify["expected_version"], pg_sync.MIGRATIONS[-1].version)
        self.assertEqual([item["version"] for item in verify["applied"]],
                         [item.version for item in pg_sync.MIGRATIONS])

    def test_v4_widens_legacy_dataset_constraint_and_converges_name(self):
        """升级路径：v4 之前是自动命名的旧枚举，v4 之后必须收敛到显式名，并能写入两类新数据集。"""
        self.run_migrate("--apply")
        with psycopg.connect(self.dsn, autocommit=True) as conn, conn.cursor() as cur:
            # 回退成 v4 之前的真实状态：PostgreSQL 自动命名 + 只含第 1 期四类
            cur.execute("alter table ops_import_batches drop constraint ck_ops_import_batches_dataset")
            cur.execute(
                "alter table ops_import_batches add constraint ops_import_batches_dataset_check "
                "check (dataset in ('ops_traffic', 'ops_timeslot', 'ops_inventory', 'ops_trade_area'))"
            )
            cur.execute("delete from schema_migrations where version = 4")
            self.assertEqual(self.check_constraint_name("ops_import_batches", "dataset"),
                             "ops_import_batches_dataset_check")
            with self.assertRaises(psycopg.errors.CheckViolation):
                cur.execute(self.OPS_BATCH_INSERT, ("legacy-check", "competitor_store"))

        again = json.loads(self.run_migrate("--apply").stdout)
        self.assertTrue(again["ok"])
        self.assertEqual([item["version"] for item in again["applied_now"]], [4])
        self.assertEqual(self.check_constraint_name("ops_import_batches", "dataset"),
                         "ck_ops_import_batches_dataset")
        with psycopg.connect(self.dsn, autocommit=True) as conn, conn.cursor() as cur:
            for index, dataset in enumerate(("competitor_store", "competitor_offer")):
                cur.execute(self.OPS_BATCH_INSERT, (f"after-v4-{index}", dataset))
        # 不用 LIKE：psycopg 会把 % 当占位符，用显式集合更直白也更稳
        self.assertEqual(self.pg_scalar(
            "select count(*) from ops_import_batches "
            "where dataset in ('competitor_store', 'competitor_offer')"), 2)
        self.assertTrue(json.loads(self.run_migrate("--verify").stdout)["ok"])

    def test_v4_rerun_is_idempotent(self):
        """v4 里的 drop + add 约束必须可重复执行。"""
        self.run_migrate("--apply")
        with psycopg.connect(self.dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("delete from schema_migrations where version = 4")
        rerun = self.run_migrate("--apply")
        self.assertEqual(rerun.returncode, 0, rerun.stdout[-1200:] + rerun.stderr[-800:])
        self.assertTrue(json.loads(rerun.stdout)["ok"])
        self.assertEqual(self.check_constraint_name("ops_import_batches", "dataset"),
                         "ck_ops_import_batches_dataset")

    def test_apply_adopts_schema_created_by_init(self):
        """历史库（init 建表、无版本行）升级：认领基线而不是重跑 DDL。"""
        self.run_sync("init")
        status = json.loads(self.run_sync("migrate", "--status").stdout)
        self.assertEqual([item["version"] for item in status["pending"]], [item.version for item in pg_sync.MIGRATIONS])
        self.assertTrue(status["pending"][0]["adoptable"])

        result = self.run_migrate("--apply")
        self.assertEqual(result.returncode, 0, result.stdout[-1200:] + result.stderr[-800:])
        payload = json.loads(result.stdout)
        self.assertTrue(payload["applied_now"][0]["adopted"])
        self.assertEqual(payload["applied_now"][0]["statements"], 0)
        self.assertTrue(payload["up_to_date"])
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("select adopted, name from schema_migrations where version = 1")
            adopted, name = cur.fetchone()
        self.assertTrue(adopted)
        self.assertTrue(name)
        self.assertTrue({"stores", "action_tasks"} <= self.pg_tables())

    # ---------------- 恢复保障 / 并发 / 失败 ----------------

    def test_apply_refuses_to_start_without_recovery_point(self):
        blocked = self.tmp / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        result = self.run_sync("migrate", "--apply", "--recovery-dir", str(blocked / "nested"))
        self.assertEqual(result.returncode, 4, result.stdout[-800:] + result.stderr[-800:])
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "recovery_point_failed")
        self.assertEqual(self.pg_tables(), set(), "恢复点写不出来时不得改动数据库")

    def test_concurrent_apply_is_rejected_by_advisory_lock(self):
        with psycopg.connect(self.dsn, autocommit=True) as blocker:
            with blocker.cursor() as cur:
                cur.execute("select pg_try_advisory_lock(%s)", (pg_sync.MIGRATION_LOCK_KEY,))
                self.assertTrue(cur.fetchone()[0])
            status = json.loads(self.run_sync("migrate", "--status").stdout)
            self.assertTrue(status["locked"], "status 应能看出 advisory lock 被占用")

            result = self.run_migrate("--apply")
            self.assertEqual(result.returncode, 3, result.stdout[-800:] + result.stderr[-800:])
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["reason"], "migration_locked")
            self.assertEqual(self.pg_tables(), set(), "抢不到锁时不得改动数据库")

            with blocker.cursor() as cur:
                cur.execute("select pg_advisory_unlock(%s)", (pg_sync.MIGRATION_LOCK_KEY,))

        again = self.run_migrate("--apply")
        self.assertEqual(again.returncode, 0, again.stdout[-800:] + again.stderr[-800:])
        self.assertTrue(CORE_TABLES <= self.pg_tables())

    def test_failed_migration_rolls_back_and_stops_after_failure(self):
        """失败必须整体回滚：不留半成品结构，不写入版本行，更不继续后面的迁移。"""
        migrations = (
            pg_sync.Migration(1, "create_first", ("create table t_first (id integer)",)),
            pg_sync.Migration(2, "broken_step", ("create table t_boom (id integer)",
                                                 "select * from table_does_not_exist_boom")),
            pg_sync.Migration(3, "create_later", ("create table t_later (id integer)",)),
        )
        with psycopg.connect(self.dsn) as pg:
            payload, code = pg_sync.run_migrations(pg, mode="apply", migrations=migrations,
                                                   recovery_dir=self.recovery_dir)
        self.assertEqual(code, 5, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "migration_failed")
        self.assertEqual(payload["failed_version"], 2)
        self.assertIn("does not exist", payload["error"])
        self.assertTrue(Path(payload["recovery"]["manifest"]).exists(), "失败也要留下恢复点证据")
        self.assertEqual(self.pg_tables() - BOOKKEEPING_TABLES, set(), "失败不得留下半成品表")
        self.assertEqual(self.pg_scalar("select count(*) from schema_migrations"), 0)
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg_sync.read_migration_state(pg), [])

    # ---------------- verify ----------------

    def test_verify_reports_pending_then_ok_then_missing_tables(self):
        before = self.run_sync("migrate", "--verify")
        self.assertEqual(before.returncode, 6, before.stdout[-800:] + before.stderr[-800:])
        payload = json.loads(before.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual([item["version"] for item in payload["missing"]], [item.version for item in pg_sync.MIGRATIONS])
        self.assertEqual(payload["checksum_mismatches"], [])

        self.run_migrate("--apply")
        after = self.run_sync("migrate", "--verify")
        self.assertEqual(after.returncode, 0, after.stdout[-1200:] + after.stderr[-800:])
        payload = json.loads(after.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["up_to_date"])
        self.assertEqual(payload["checksum_mismatches"], [])
        self.assertEqual(payload["missing_tables"], [])
        self.assertEqual(payload["unknown_applied"], [])
        self.assertTrue(payload["schema_fingerprint"])

        with psycopg.connect(self.dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("drop table reviews cascade")
        damaged = self.run_sync("migrate", "--verify")
        self.assertEqual(damaged.returncode, 6, damaged.stdout[-800:])
        payload = json.loads(damaged.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("reviews", payload["missing_tables"])

    def test_verify_detects_checksum_drift_and_unknown_version(self):
        migrations = (
            pg_sync.Migration(1, "create_t_first", ("create table t_first (id integer)",)),
            pg_sync.Migration(2, "create_t_second", ("create table t_second (id integer)",)),
        )
        with psycopg.connect(self.dsn) as pg:
            payload, code = pg_sync.run_migrations(pg, mode="apply", migrations=migrations,
                                                   recovery_dir=self.recovery_dir)
            self.assertEqual(code, 0, payload)
            payload, code = pg_sync.run_migrations(pg, mode="verify", migrations=migrations)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["up_to_date"])

        with psycopg.connect(self.dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("update schema_migrations set checksum = 'tampered' where version = 1")
            cur.execute("""insert into schema_migrations (version, name, checksum, adopted, applied_by)
                           values (99, 'from_the_future', 'x', false, 'test')""")
        with psycopg.connect(self.dsn) as pg:
            payload, code = pg_sync.run_migrations(pg, mode="verify", migrations=migrations)
        self.assertEqual(code, 6, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual([item["version"] for item in payload["checksum_mismatches"]], [1])
        self.assertEqual(payload["unknown_applied"], [99])


if __name__ == "__main__":
    unittest.main()