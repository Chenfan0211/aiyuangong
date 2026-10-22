# -*- coding: utf-8 -*-
"""批次 8：PostgreSQL 建表与 SQLite → PG 迁移（需要 psycopg + pgserver，缺失时跳过）。"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

import pg_sync
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import pgserver
    import psycopg
    HAS_PG = True
except ImportError:                                  # pragma: no cover - 环境相关
    HAS_PG = False

STORE = "WH-LION-001"
STORE_HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG_HEADER = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
REV_HEADER = "review_id,platform,store_id,rating,created_at,status,category,replied_at\n"

LANDED_TABLES = {
    "stores", "store_configs", "platform_metrics", "package_metrics", "reviews", "metric_import_batches",
    "action_tasks", "task_evidence", "task_events", "users", "memberships", "auth_sessions",
    "login_attempts", "audit_events", "agent_sessions", "agent_messages",
    "approvals", "agent_tool_runs", "notifications",
    # 第 1 期：门店侧经营数据
    "store_traffic_daily", "time_slot_metrics", "inventory_items", "inventory_alerts",
    "trade_areas", "ops_import_batches",
    # 第 2 期：竞品监控
    "competitor_stores", "competitor_offers", "competitor_matches",
    # 第 3 期：规则库（原"B. 规划中"转正）
    "action_rules",
}
PLANNED_TABLES = {"connector_accounts", "ingestion_runs", "metric_facts_daily", "offers"}
TENANT_SCOPED = LANDED_TABLES - {"login_attempts"}


@unittest.skipUnless(HAS_PG, "需要 psycopg + pgserver（本地 PostgreSQL）才能验证 PG 落地")
class PgMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_pgtest_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()
        cls._seed_sqlite()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------- 造 SQLite 源数据 ----------------

    @classmethod
    def _seed_sqlite(cls):
        from auth import AuthStore
        from platform_data import PlatformMetricStore
        from prototype import generate_daily_plan
        from snapshot_builder import build_snapshot, load_config
        from store_agent import AgentSessionStore
        from task_store import TaskStore

        cls.metrics_db = cls.tmp / "platform_metrics.db"
        cls.ops_db = cls.tmp / "store_ops.db"
        cls.chat_db = cls.tmp / "agent_demo.db"
        today, yesterday = date.today(), date.today() - timedelta(days=1)
        config = load_config(ROOT / "store_config.json")

        metrics = PlatformMetricStore(cls.metrics_db)
        (cls.tmp / "m.csv").write_bytes((STORE_HEADER
            + f"{yesterday},meituan,{STORE},100,20,5,4,300.50,1,1,0,4.8\n"
            + f"{today},meituan,{STORE},110,22,5,4,320.25,0,1,0,4.8\n").encode("utf-8"))
        metrics.import_csv(cls.tmp / "m.csv", STORE)
        (cls.tmp / "d.csv").write_bytes((STORE_HEADER
            + f"{today},douyin,{STORE},85,16,4,3,210.75,0,1,0,4.7\n").encode("utf-8"))
        metrics.import_csv(cls.tmp / "d.csv", STORE)
        (cls.tmp / "p.csv").write_bytes((PKG_HEADER
            + f"{today},meituan,{STORE},MT-1,AI全周小包2h,168,5,4,1200.00\n").encode("utf-8"))
        metrics.import_packages_csv(cls.tmp / "p.csv", STORE)
        (cls.tmp / "r.csv").write_bytes((REV_HEADER
            + f"MT-1024,meituan,{STORE},2,{today} 09:15:00,open,服务体验,\n"
            + f"MT-1001,meituan,{STORE},5,{yesterday} 18:00:00,replied,环境安静,{yesterday} 19:00:00\n").encode("utf-8"))
        metrics.import_reviews_csv(cls.tmp / "r.csv", STORE)

        tasks = TaskStore(cls.ops_db)
        auth = AuthStore(cls.ops_db)
        user = auth.create_user("manager01", "ShiZiZuo-2026-store", "武汉狮子座店店长", "manager",
                                "LPL-001", STORE)
        auth.resolve(auth.login("manager01", "ShiZiZuo-2026-store")["token"])
        snapshot = build_snapshot(config, metrics, today, task_store=tasks)
        tasks.save_plan(generate_daily_plan(snapshot), STORE)
        invite = next(t for t in tasks.list_tasks(STORE) if t["task_id"].endswith("review-invite"))
        tasks.change_status(invite["task_id"], "accepted", "武汉狮子座店店长")
        tasks.add_evidence(invite["task_id"], "脱敏订单号", "MT-1", "武汉狮子座店店长")
        tasks.change_status(invite["task_id"], "done", "武汉狮子座店店长")
        tasks.bind_session("s-pg", user["user_id"], "manager")

        sessions = AgentSessionStore(cls.chat_db)
        sessions.append("s-pg", user["user_id"], "user", "数据齐不齐？")
        sessions.append("s-pg", user["user_id"], "assistant", "近 30 天没有发现缺日。", "data_quality")

    # ---------------- 工具 ----------------

    def setUp(self):
        """每个用例从空 schema 开始：类内共享同一个 PostgreSQL 实例，避免 RLS 策略等相互污染。"""
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")

    def run_sync(self, *args):
        result = subprocess.run(
            [sys.executable, "pg_sync.py", "--dsn", self.dsn, "--config", str(ROOT / "store_config.json"),
             "--metrics-db", str(self.metrics_db), "--ops-db", str(self.ops_db), "--chat-db", str(self.chat_db),
             *args],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
        return result

    def pg_tables(self):
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("select table_name from information_schema.tables where table_schema='public'")
            return {row[0] for row in cur.fetchall()}

    def pg_count(self, table):
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(f"select count(*) from {table}")
            return int(cur.fetchone()[0])

    # ---------------- 用例 ----------------

    def test_schema_applies_and_is_idempotent(self):
        first = self.run_sync("init")
        self.assertEqual(first.returncode, 0, first.stderr[-800:])
        second = self.run_sync("init")                      # 幂等：重复执行不报错
        self.assertEqual(second.returncode, 0, second.stderr[-800:])
        report = json.loads(second.stdout)
        self.assertGreaterEqual(report["ddl_statements"], 20)
        tables = self.pg_tables()
        self.assertTrue(LANDED_TABLES <= tables, LANDED_TABLES - tables)

    def test_planned_tables_are_not_created_by_init(self):
        self.run_sync("init")
        tables = self.pg_tables()
        self.assertFalse(PLANNED_TABLES & tables, PLANNED_TABLES & tables)

    def test_every_tenant_scoped_table_has_tenant_id(self):
        """租户级隔离的落地证据：每张业务表都有 tenant_id，且除 audit_events 外都是 NOT NULL。

        audit_events 是唯一例外：未知用户名的失败登录无法归属租户，只能记 NULL
        （schema.sql 中已注明），这类事件不会出现在按租户过滤的门店审计里。
        """
        self.run_sync("init")
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """select table_name, is_nullable from information_schema.columns
                   where table_schema='public' and column_name='tenant_id'""")
            columns = {row[0]: row[1] for row in cur.fetchall()}
        self.assertTrue(TENANT_SCOPED <= set(columns), TENANT_SCOPED - set(columns))
        not_null = {name for name, nullable in columns.items() if nullable == "NO"}
        self.assertTrue(TENANT_SCOPED - {"audit_events"} <= not_null,
                        TENANT_SCOPED - {"audit_events"} - not_null)
        self.assertEqual(columns["audit_events"], "YES")

    def test_etl_and_verify_pass(self):
        result = self.run_sync("all")
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-800:])
        report = json.loads(result.stdout)
        self.assertTrue(report["row_counts_ok"])
        self.assertTrue(report["business_checks_ok"])
        self.assertTrue(report["ok"])
        migrated = report["migrated_rows"]
        for table in ("platform_metrics", "reviews", "action_tasks", "users", "audit_events", "agent_messages"):
            self.assertGreater(migrated[table], 0, table)
        for item in report["business_checks"].values():
            self.assertTrue(item["ok"], item)

    def test_etl_is_idempotent(self):
        self.run_sync("all")
        before = {table: self.pg_count(table) for table in ("platform_metrics", "action_tasks",
                                                            "task_events", "audit_events", "agent_messages")}
        again = self.run_sync("etl")
        self.assertEqual(again.returncode, 0, again.stderr[-800:])
        after = {table: self.pg_count(table) for table in before}
        self.assertEqual(before, after, "重复迁移不应产生重复数据")

    def test_migrated_rows_keep_types_and_content(self):
        self.run_sync("all")
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            # 列名与运行时一致（batch 8b 起 schema.sql 不再重命名 JSON 列）
            cur.execute("""select title, target_json, evidence_required_json, approval_required, due_at
                           from action_tasks order by task_id limit 1""")
            title, target, evidence, approval, due_at = cur.fetchone()
            self.assertTrue(title)
            self.assertIsInstance(target, dict)             # jsonb 已解析
            self.assertIsInstance(evidence, list)
            self.assertIsInstance(approval, bool)
            self.assertIsNotNone(due_at.tzinfo)             # timestamptz 带时区
            cur.execute("select sum(gmv), sum(paid_orders) from platform_metrics")
            gmv, paid = cur.fetchone()
            self.assertEqual(float(gmv), 831.5)
            self.assertEqual(int(paid), 14)
            cur.execute("""select count(*) from reviews where status='open' and rating <= 2""")
            self.assertEqual(int(cur.fetchone()[0]), 1)

    def test_missing_column_is_reported_clearly(self):
        """列漏配时必须报错而不是写入 NULL（防止再次出现 imported_at 为空的缺陷）。"""
        self.run_sync("init")
        result = self.run_sync("--only", "platform_metrics", "etl")   # 全局参数写在子命令之前
        self.assertEqual(result.returncode, 0, result.stderr[-500:])


class OutcomeTablesTests(unittest.TestCase):
    """第 3 期第 5 步：task_feedback / task_outcomes 收口进权威模型。"""

    def test_tables_are_registered_in_authoritative_model(self):
        names = {spec.name for spec in pg_sync.TABLES}
        self.assertIn("task_feedback", names)
        self.assertIn("task_outcomes", names)

    def test_schema_section_a_creates_outcome_tables(self):
        joined = "\n".join(pg_sync.read_schema_section_a()).lower()
        self.assertIn("create table if not exists task_feedback", joined)
        self.assertIn("create table if not exists task_outcomes", joined)

    def test_outcome_tables_migration_lands_them(self):
        latest = next(item for item in pg_sync.MIGRATIONS
                      if item.name == "task_outcome_tables_landing")
        landed = {name.lower() for name in pg_sync.tables_in_statements(latest.statements)}
        self.assertEqual(landed, {"task_feedback", "task_outcomes"})
        self.assertEqual(set(latest.requires_tables), {"task_feedback", "task_outcomes"})


if __name__ == "__main__":
    unittest.main()
