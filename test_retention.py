# -*- coding: utf-8 -*-
"""P0 第 6 项：数据保留与删除策略（默认只计划、显式才删除、先归档、全程审计）。"""
import json
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from auth import AuthStore
from governance import ApprovalStore, ToolRunLog
from ops_backup import table_hash
from platform_data import PlatformMetricStore
from retention import (
    DEFAULT_RETENTION_DAYS,
    RetentionError,
    archive_summary,
    plan_purge,
    policy_from_config,
    purge,
)
from security import check_retention_policy, gate_report
from snapshot_builder import load_config
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
NOW = datetime(2026, 10, 18, 12, 0, 0)
HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
          "gmv,refunds,review_count,negative_review_count,avg_rating\n")

try:
    import pgserver
    import psycopg
    HAS_PG = True
except ImportError:                                   # pragma: no cover
    HAS_PG = False


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metrics_db, self.ops_db = self.root / "m.db", self.root / "s.db"
        self.metrics = PlatformMetricStore(self.metrics_db, tenant_id=TENANT)
        self.tasks = TaskStore(self.ops_db, tenant_id=TENANT)
        self.approvals = ApprovalStore(self.ops_db, tenant_id=TENANT)
        self.tool_log = ToolRunLog(self.ops_db, tenant_id=TENANT)
        self.auth = AuthStore(self.ops_db, tenant_id=TENANT)
        self.policy = dict(DEFAULT_RETENTION_DAYS)

    def tearDown(self):
        self.temp.cleanup()

    def seed_days_ago(self, days: int) -> None:
        """造一条 N 天前的平台指标 + 一条 N 天前的对话消息 + 一条 1 天前的。"""
        day = (NOW - timedelta(days=days)).date().isoformat()
        path = self.root / f"m{days}.csv"
        path.write_bytes((HEADER + f"{day},meituan,{STORE},100,20,5,4,300,0,1,0,4.8\n").encode("utf-8"))
        self.metrics.import_csv(path, STORE)

    def seed_message(self, days: int = 0) -> None:
        """插入一条消息，并把**这一条**的 created_at 改到 N 天前（不能改到其它行）。"""
        from db import Database
        from store_agent import AgentSessionStore

        content = f"消息 {days} 天前"
        AgentSessionStore(self.ops_db, tenant_id=TENANT).append("s1", "u1", "user", content)
        with Database(self.ops_db).connection() as conn:
            conn.execute("update agent_messages set created_at=? where content=?",
                         ((NOW - timedelta(days=days)).isoformat(), content))


class PolicyTests(Base):
    def test_default_policy_covers_all_tables(self):
        policy = policy_from_config({})
        self.assertEqual(policy["agent_messages"], 90)
        self.assertEqual(policy["audit_events"], 730)
        for table in ("users", "memberships", "stores", "store_configs"):
            self.assertEqual(policy[table], 0, f"{table} 应默认永久保留")
        self.assertEqual(len(policy), len(DEFAULT_RETENTION_DAYS))

    def test_config_overrides_and_validation(self):
        policy = policy_from_config({"retention": {"reviews": 30, "users": 0}})
        self.assertEqual(policy["reviews"], 30)
        self.assertEqual(policy["audit_events"], DEFAULT_RETENTION_DAYS["audit_events"])
        for bad in ({"retention": {"reviews": -1}}, {"retention": {"reviews": "abc"}},
                    {"retention": {"不存在的表": 30}}):
            with self.subTest(config=bad):
                with self.assertRaises(RetentionError):
                    policy_from_config(bad)

    def test_real_store_config_has_policy(self):
        config = load_config(ROOT / "store_config.json")
        result = check_retention_policy(ROOT / "store_config.json")
        self.assertTrue(result["ok"], result["problems"])
        self.assertGreater(result["tables_with_expiry"], 10)
        # trade_areas 是商圈主数据；competitor_stores / competitor_matches 是竞品主数据，同样永久保留
        self.assertEqual(sorted(result["forever_tables"]),
                         ['capabilities', 'capability_bindings', 'competitor_matches', 'competitor_stores', 'digital_employees', 'inspection_items', 'inspection_templates', 'knowledge_bases', 'knowledge_chunks', 'knowledge_documents', 'memberships', 'sop_versions', 'sops', 'store_configs', 'stores', 'team_members', 'teams', 'tool_credentials', 'tool_instances', 'tool_templates', 'trade_areas', 'users'])
        self.assertEqual(policy_from_config(config)["reviews"], 180)


class PlanTests(Base):
    def test_plan_counts_without_deleting(self):
        self.seed_days_ago(900)                      # 指标保留 1095 天 → 不该被清
        self.seed_message(200)                       # 对话保留 90 天 → 该被清
        plans = {plan.table: plan for plan in plan_purge(self.ops_db, self.policy, now=NOW)}
        self.assertEqual(plans["agent_messages"].rows, 1)
        self.assertEqual(plans["agent_messages"].action, "purge")
        self.assertEqual(plans["platform_metrics"].rows, 0)
        self.assertEqual(plans["users"].action, "keep")

        from db import Database

        with Database(self.ops_db).connection() as conn:
            self.assertEqual(int(conn.execute("select count(*) as c from agent_messages").fetchone()["c"]), 1)

        metric_plans = {plan.table: plan for plan in plan_purge(self.metrics_db, self.policy, now=NOW)}
        self.assertEqual(metric_plans["platform_metrics"].action, "keep")

    def test_missing_tables_are_skipped(self):
        """库里还没有的表要跳过而不是报错；且空库不该有任何清理动作。"""
        empty = self.root / "empty.db"
        plans = plan_purge(empty, self.policy, now=NOW)
        self.assertTrue(all(plan.action != "purge" for plan in plans), [p.as_dict() for p in plans])
        self.assertTrue(all(plan.rows == 0 for plan in plans))
        skipped = [plan.table for plan in plans if plan.action == "skip"]
        self.assertIn("platform_metrics", skipped)                       # 有期限但表不存在
        self.assertEqual({plan.table for plan in plans if plan.action == "keep"},
                         {"trade_areas", "competitor_stores", "competitor_matches",
                          "users", "memberships", "stores", "store_configs",
                          "inspection_templates", "inspection_items",
                          "capabilities", "capability_bindings",
                          "knowledge_bases", "knowledge_chunks", "knowledge_documents",
                          "sops", "sop_versions",
                          "tool_templates", "tool_instances", "tool_credentials",
                          "digital_employees", "teams", "team_members"})


class PurgeTests(Base):
    def test_dry_run_never_deletes(self):
        self.seed_message(200)
        result = purge(self.ops_db, self.policy, apply=False, now=NOW)
        self.assertFalse(result["apply"])
        self.assertEqual(result["planned_total"], 1)
        self.assertEqual(result["deleted"], {})
        self.assertEqual(self.auth.list_audit(store_id=STORE, limit=5), [])   # 未执行就不该有审计

    def test_apply_deletes_only_expired_rows(self):
        self.seed_message(200)                       # 过期
        self.seed_message(0)                         # 未过期
        result = purge(self.ops_db, self.policy, apply=True, now=NOW, tenant_id=TENANT, store_id=STORE)
        self.assertEqual(result["deleted"]["agent_messages"], 1)
        from db import Database

        with Database(self.ops_db).connection() as conn:
            rows = [dict(row) for row in conn.execute("select content from agent_messages").fetchall()]
        self.assertEqual(len(rows), 1)
        self.assertIn("0 天前", rows[0]["content"])

    def test_forever_tables_are_never_touched(self):
        self.auth.create_user("manager01", "ShiZiZuo-2026-store", "店长", "manager", TENANT, STORE)
        from db import Database

        with Database(self.ops_db).connection() as conn:
            conn.execute("update users set created_at='2000-01-01T00:00:00'")
            conn.execute("update memberships set created_at='2000-01-01T00:00:00'")
        result = purge(self.ops_db, self.policy, apply=True, now=NOW, tenant_id=TENANT, store_id=STORE)
        self.assertNotIn("users", result["deleted"])
        with Database(self.ops_db).connection() as conn:
            self.assertEqual(int(conn.execute("select count(*) as c from users").fetchone()["c"]), 1)
            self.assertEqual(int(conn.execute("select count(*) as c from memberships").fetchone()["c"]), 1)

    def test_purge_is_store_scoped(self):
        self.auth.record_audit(event_type="old_store", actor_type="user", actor_id="u1",
                               tenant_id=TENANT, store_id=STORE, object_type="x", object_id="1")
        self.auth.record_audit(event_type="old_other", actor_type="user", actor_id="u2",
                               tenant_id=TENANT, store_id="OTHER-STORE", object_type="x", object_id="2")
        from db import Database
        with Database(self.ops_db).connection() as conn:
            conn.execute("update audit_events set created_at='2000-01-01T00:00:00'")
        result = purge(self.ops_db, dict(self.policy, audit_events=30), apply=True, now=NOW,
                       tenant_id=TENANT, store_id=STORE)
        self.assertEqual(result["deleted"].get("audit_events"), 1)  # 只删除本店旧事件；清理审计在删除后写入
        with Database(self.ops_db).connection() as conn:
            remaining = [row["event_type"] for row in conn.execute("select event_type from audit_events").fetchall()]
        self.assertIn("old_other", remaining)
        self.assertNotIn("old_store", remaining)

    def test_archive_write_failure_keeps_rows(self):
        self.seed_message(200)
        blocked = self.root / "not-a-directory"
        blocked.write_text("x", encoding="utf-8")
        with self.assertRaises(OSError):
            purge(self.ops_db, self.policy, apply=True, export_dir=blocked, now=NOW,
                  tenant_id=TENANT, store_id=STORE)
        from db import Database
        with Database(self.ops_db).connection() as conn:
            self.assertEqual(int(conn.execute("select count(*) as c from agent_messages").fetchone()["c"]), 1)

    def test_export_archive_contains_deleted_rows(self):
        self.seed_message(200)
        export_dir = self.root / "archive"
        result = purge(self.ops_db, self.policy, apply=True, export_dir=export_dir, now=NOW,
                       tenant_id=TENANT, store_id=STORE)
        self.assertIsNotNone(result["exported"])
        path = Path(result["exported"]["path"])
        self.assertTrue(path.exists())
        summary = archive_summary(path)
        self.assertEqual(summary["tables"]["agent_messages"]["rows"], 1)
        self.assertEqual(summary["actor"], "ops:retention")

    def test_audit_record_is_written_and_survives_audit_purge(self):
        self.auth.record_audit(event_type="old_event", actor_type="user", actor_id="manager01",
                               tenant_id=TENANT, store_id=STORE, object_type="x", object_id="1")
        from db import Database

        with Database(self.ops_db).connection() as conn:
            conn.execute("update audit_events set created_at='2000-01-01T00:00:00'")
        policy = dict(self.policy, audit_events=30)
        result = purge(self.ops_db, policy, apply=True, now=NOW, tenant_id=TENANT, store_id=STORE)
        self.assertEqual(result["deleted"].get("audit_events"), 1)
        events = [row["event_type"] for row in self.auth.list_audit(store_id=STORE, limit=10)]
        self.assertNotIn("old_event", events)
        self.assertIn("retention_purge", events)          # 清理记录本身必须留下
        record = self.auth.list_audit(store_id=STORE, limit=1)[0]
        self.assertEqual(record["after_json"] and json.loads(record["after_json"])["total"], 1)


class GateTests(Base):
    def test_gate_report_includes_retention_item(self):
        report = gate_report(ROOT)
        names = [item["gate"] for item in report["items"]]
        self.assertIn("数据保留策略已配置", names)
        self.assertTrue(report["ok"], report["items"])

    def test_gate_report_flags_config_without_retention(self):
        config_path = self.root / "no_retention.json"
        config_path.write_text(json.dumps({"store_id": STORE}), encoding="utf-8")
        result = check_retention_policy(config_path)
        self.assertFalse(result["ok"])
        self.assertTrue(any("没有 retention" in problem for problem in result["problems"]))


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能验证 PostgreSQL 上的保留期清理（含外键顺序）")
class PgRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_ret_pg_"))
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

    def test_purge_respects_foreign_key_order(self):
        from db import Database

        tasks = TaskStore(self.dsn, tenant_id=TENANT)
        tasks.save_plan({"date": "2020-01-01", "tasks": [{
            "task_id": "OLD-1", "category": "reputation", "title": "旧任务", "assignee": "店长",
            "priority": "high", "due_at": "2020-01-01T18:00:00", "target": {}, "evidence_required": [],
            "reason": "r", "requires_approval": False}]}, STORE)
        tasks.add_evidence("OLD-1", "脱敏订单号", "X-1", "店长")
        with Database(self.dsn).connection() as conn:
            for table, column in (("action_tasks", "created_at"), ("action_tasks", "updated_at"),
                                  ("task_evidence", "submitted_at"), ("task_events", "created_at")):
                conn.execute(f"update {table} set {column}='2000-01-01T00:00:00'")
        result = purge(self.dsn, dict(DEFAULT_RETENTION_DAYS), apply=True, now=NOW,
                       tenant_id=TENANT, store_id=STORE)
        self.assertEqual(result["deleted"]["task_evidence"], 1)
        self.assertEqual(result["deleted"]["action_tasks"], 1)
        with Database(self.dsn).connection() as conn:
            self.assertEqual(int(conn.execute("select count(*) as c from action_tasks").fetchone()["c"]), 0)


if __name__ == "__main__":
    unittest.main()
