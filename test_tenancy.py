# -*- coding: utf-8 -*-
"""批次 8c：多租户（租户归属、门店注册表、跨租户守卫、PostgreSQL 行级安全）。

三层防线各自对应一个测试：
  1. 写入归属：每个 store 写入的行都带正确的 tenant_id；
  2. 注册表守卫：同一 store_id 不允许出现在两个租户下（SQLite 侧门店作用域=租户隔离的前提）；
  3. PostgreSQL 行级安全：非所有者连接**即使不带租户条件**也只能看到自己租户的行。
"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from auth import AuthStore
from governance import ApprovalStore, ToolRunLog
from platform_data import PlatformMetricStore
from registry import RegistryError, RegistryStore
from snapshot_builder import load_config
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
TENANT_A = "LPL-001"
TENANT_B = "LPL-002"
STORE = "WH-LION-001"
HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
          "gmv,refunds,review_count,negative_review_count,avg_rating\n")

try:
    import pgserver
    import psycopg
    HAS_PG = True
except ImportError:                                  # pragma: no cover
    HAS_PG = False


class LocalTenancyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metrics_db = self.root / "m.db"
        self.ops_db = self.root / "s.db"
        self.config = load_config(ROOT / "store_config.json")
        self.config = dict(self.config, tenant_id=TENANT_A)

    def tearDown(self):
        self.temp.cleanup()

    def seed_metrics(self, store, tenant, day=None):
        day = day or date.today()
        path = self.root / f"{tenant}.csv"
        path.write_bytes((HEADER + f"{day.isoformat()},meituan,{STORE},100,20,5,4,300,0,1,0,4.8\n").encode("utf-8"))
        (store or PlatformMetricStore(self.metrics_db, tenant_id=tenant)).import_csv(path, STORE)

    # ---------------- 注册表 ----------------

    def test_registry_roundtrip_and_update(self):
        registry = RegistryStore(self.ops_db, tenant_id=TENANT_A)
        first = registry.register(self.config)
        self.assertEqual((first["tenant_id"], first["store_id"]), (TENANT_A, STORE))
        self.assertEqual([s["store_id"] for s in registry.list_stores(TENANT_A)], [STORE])
        self.assertEqual(registry.list_stores(TENANT_B), [])
        self.assertEqual(registry.store_config(TENANT_A, STORE)["name"], self.config["name"])
        self.assertIsNone(registry.store_config(TENANT_B, STORE))

        changed = dict(self.config, name="武汉狮子座店（改名）")
        registry.register(changed)
        self.assertEqual(registry.list_stores(TENANT_A)[0]["name"], "武汉狮子座座店（改名）".replace("座座", "座"))
        self.assertEqual(len(registry.list_stores(TENANT_A)), 1)          # 幂等：不重复插入

    def test_guard_rejects_same_store_id_under_two_tenants(self):
        RegistryStore(self.ops_db, tenant_id=TENANT_A).register(self.config)
        other = dict(self.config, tenant_id=TENANT_B)
        with self.assertRaises(RegistryError) as error:
            RegistryStore(self.ops_db, tenant_id=TENANT_B).register(other)
        self.assertIn("已注册在租户", str(error.exception))
        # 同一租户重复注册不受影响
        RegistryStore(self.ops_db, tenant_id=TENANT_A).register(self.config)

    def test_guard_rejects_missing_scope(self):
        with self.assertRaises(RegistryError):
            RegistryStore(self.ops_db, tenant_id=TENANT_A).register({"store_id": STORE})
        with self.assertRaises(RegistryError):
            RegistryStore(self.ops_db, tenant_id=TENANT_A).register({"tenant_id": TENANT_A})

    # ---------------- 写入归属 ----------------

    def test_rows_carry_tenant_on_sqlite(self):
        store = PlatformMetricStore(self.metrics_db, tenant_id=TENANT_A)
        self.seed_metrics(store, TENANT_A)
        tasks = TaskStore(self.ops_db, tenant_id=TENANT_A)
        tasks.save_plan({"date": date.today().isoformat(), "tasks": [{
            "task_id": "T-1", "category": "reputation", "title": "处理差评", "assignee": "员工小李",
            "priority": "high", "due_at": f"{date.today().isoformat()}T18:00:00", "target": {},
            "evidence_required": [], "reason": "r", "requires_approval": False}]}, STORE)
        auth = AuthStore(self.ops_db, tenant_id=TENANT_A)
        auth.create_user("manager01", "ShiZiZuo-2026-store", "武汉狮子座店店长", "manager", TENANT_A, STORE)
        auth.record_audit(event_type="probe", actor_type="user", actor_id="manager01",
                          tenant_id=TENANT_A, store_id=STORE, object_type="x", object_id="1")
        approvals = ApprovalStore(self.ops_db, tenant_id=TENANT_A)
        approvals.request(STORE, "pricing:change", {"a": 1}, requested_by="线上运营")
        ToolRunLog(self.ops_db, tenant_id=TENANT_A).record(STORE, "api:probe", actor_id="manager01")

        conn = sqlite3.connect(str(self.ops_db))
        conn.row_factory = sqlite3.Row
        try:
            for table in ("action_tasks", "users", "audit_events", "approvals", "agent_tool_runs"):
                tenants = {row["tenant_id"] for row in conn.execute(f"select tenant_id from {table}")}
                self.assertEqual(tenants, {TENANT_A}, f"{table} 的租户归属不正确")
        finally:
            conn.close()
        metrics = sqlite3.connect(str(self.metrics_db))
        try:
            tenants = {row[0] for row in metrics.execute("select tenant_id from platform_metrics")}
        finally:
            metrics.close()
        self.assertEqual(tenants, {TENANT_A})

    def test_legacy_sqlite_db_is_upgraded_with_tenant_column(self):
        legacy = self.root / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.executescript(
            """
            create table platform_metrics (
              store_id text not null, metric_date text not null, platform text not null,
              impressions integer, detail_views integer, paid_orders integer, verified_orders integer,
              gmv real, refunds integer, review_count integer, negative_review_count integer,
              avg_rating real, source_type text not null, source_name text not null,
              imported_at text not null, primary key (store_id, metric_date, platform));
            """
        )
        conn.execute("insert into platform_metrics(store_id,metric_date,platform,source_type,source_name,imported_at)"
                     " values(?,'2026-01-01','meituan','manual','old.csv','2026-01-01T00:00:00')", (STORE,))
        conn.commit()
        conn.close()

        store = PlatformMetricStore(legacy, tenant_id=TENANT_A)
        conn = sqlite3.connect(str(legacy))
        conn.row_factory = sqlite3.Row
        try:
            columns = {row["name"] for row in conn.execute("pragma table_info(platform_metrics)")}
            self.assertIn("tenant_id", columns)
            old_row = conn.execute("select tenant_id from platform_metrics").fetchone()
            self.assertEqual(old_row["tenant_id"], "")            # 历史行保留为空租户，不臆造归属
            indexes = {row["name"] for row in conn.execute("pragma index_list(platform_metrics)")}
            self.assertIn("ux_platform_metrics_runtime", indexes)
        finally:
            conn.close()
        self.seed_metrics(store, TENANT_A, day=date.today() - timedelta(days=1))
        conn = sqlite3.connect(str(legacy))
        try:
            tenants = sorted(row[0] for row in conn.execute("select tenant_id from platform_metrics"))
        finally:
            conn.close()
        self.assertEqual(tenants, ["", TENANT_A])


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能验证 PostgreSQL 行级安全")
class PgRlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_rls_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()
        cls.role = "rls_probe"
        cls.role_password = "rls-probe-pass"
        cls.config = dict(load_config(ROOT / "store_config.json"), tenant_id=TENANT_A)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")

    def _role_dsn(self) -> str:
        head, tail = self.dsn.split("@", 1)
        return f"postgresql://{self.role}:{self.role_password}@{tail}"

    def _prepare_two_tenants(self):
        """租户 A 用业务代码写入；租户 B 用所有者连接直写（模拟另一个租户的部署）。"""
        day = date.today().isoformat()
        path = self.tmp / "a.csv"
        path.write_bytes((HEADER + f"{day},meituan,{STORE},100,20,5,4,300,0,1,0,4.8\n").encode("utf-8"))
        PlatformMetricStore(self.dsn, tenant_id=TENANT_A).import_csv(path, STORE)
        TaskStore(self.dsn, tenant_id=TENANT_A).save_plan({"date": day, "tasks": [{
            "task_id": "A-TASK", "category": "reputation", "title": "A 店任务", "assignee": "员工小李",
            "priority": "high", "due_at": f"{day}T18:00:00", "target": {}, "evidence_required": [],
            "reason": "r", "requires_approval": False}]}, STORE)
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """insert into platform_metrics(tenant_id,store_id,metric_date,platform,impressions,
                   detail_views,paid_orders,verified_orders,gmv,refunds,review_count,
                   negative_review_count,avg_rating,source_type,source_name,imported_at)
                   values(%s,%s,%s,'meituan',999,99,9,9,9999,0,9,9,4.9,'manual','b.csv',now())""",
                (TENANT_B, STORE, day),
            )
            cur.execute("drop role if exists " + self.role)
            cur.execute(f"create role {self.role} login password '{self.role_password}'")
            cur.execute(f"grant usage on schema public to {self.role}")
            cur.execute(f"grant select on all tables in schema public to {self.role}")

    def test_rls_blocks_cross_tenant_reads_without_filters(self):
        self._prepare_two_tenants()
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("select tenant_id, count(*) from platform_metrics group by tenant_id order by tenant_id")
            owner_view = cur.fetchall()
        self.assertEqual([row[0] for row in owner_view], [TENANT_A, TENANT_B])   # 所有者可见全部（迁移用）

        with psycopg.connect(self._role_dsn()) as conn, conn.cursor() as cur:
            cur.execute("select count(*) from platform_metrics")               # 故意不带租户条件
            self.assertEqual(int(cur.fetchone()[0]), 0, "未设置租户时不应看到任何行")
            cur.execute("select set_config('app.tenant_id', %s, false)", (TENANT_A,))
            cur.execute("select tenant_id, gmv from platform_metrics")
            rows = cur.fetchall()
            self.assertEqual({row[0] for row in rows}, {TENANT_A})
            self.assertEqual(float(rows[0][1]), 300.0)
            cur.execute("select count(*) from action_tasks")
            self.assertEqual(int(cur.fetchone()[0]), 1)
            cur.execute("select set_config('app.tenant_id', %s, false)", (TENANT_B,))
            cur.execute("select gmv from platform_metrics")
            self.assertEqual([float(row[0]) for row in cur.fetchall()], [9999.0])

    def test_registry_and_tenant_attribution_on_postgres(self):
        RegistryStore(self.dsn, tenant_id=TENANT_A).register(self.config)
        registry = RegistryStore(self.dsn, tenant_id=TENANT_A)
        self.assertEqual([s["store_id"] for s in registry.list_stores(TENANT_A)], [STORE])
        self.assertEqual(registry.store_config(TENANT_A, STORE)["name"], self.config["name"])
        path = self.tmp / "b.csv"
        path.write_bytes((HEADER + f"{date.today().isoformat()},meituan,{STORE},50,10,2,2,100,0,1,0,4.5\n").encode("utf-8"))
        PlatformMetricStore(self.dsn, tenant_id=TENANT_A).import_csv(path, STORE)
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("select distinct tenant_id from platform_metrics")
            self.assertEqual([row[0] for row in cur.fetchall()], [TENANT_A])
            cur.execute("select count(*) from stores")
            self.assertEqual(int(cur.fetchone()[0]), 1)
            cur.execute("select config_json->>'store_id' from store_configs")
            self.assertEqual(cur.fetchone()[0], STORE)

    def test_guard_on_postgres(self):
        RegistryStore(self.dsn, tenant_id=TENANT_A).register(self.config)
        other = dict(self.config, tenant_id=TENANT_B)
        with self.assertRaises(RegistryError):
            RegistryStore(self.dsn, tenant_id=TENANT_B).register(other)


if __name__ == "__main__":
    unittest.main()
