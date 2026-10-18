# -*- coding: utf-8 -*-
"""批次 8b：同一业务场景在 SQLite 与 PostgreSQL 上运行并逐项比对（双引擎等价性）。

这是"运行时读写切到 PostgreSQL"的核心证据：同一份业务代码、同一份输入数据，
在两种引擎上必须产出相同的任务、相同的经营口径与相同的日报文本。
"""
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from auth import AuthStore, SessionInvalid
from governance import ApprovalStore, ToolRunLog, apply_decision
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import build_snapshot, load_config
from store_agent import AgentSessionStore
from task_store import TaskStore, TaskStoreError

ROOT = Path(__file__).resolve().parent
try:
    import pgserver
    HAS_PG = True
except ImportError:                                   # pragma: no cover - 环境相关
    HAS_PG = False

STORE = "WH-LION-001"
PASSWORD = "ShiZiZuo-2026-store"
HEADERS = {
    "store": ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
              "gmv,refunds,review_count,negative_review_count,avg_rating\n"),
    "package": "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n",
    "review": "review_id,platform,store_id,rating,created_at,status,category,replied_at\n",
}


def run_scenario(target, workdir: Path) -> dict:
    """在给定引擎（SQLite 路径或 PG DSN）上跑一遍完整业务场景，返回可比较的观测结果。"""
    config = load_config(ROOT / "store_config.json")
    day = date(2026, 9, 3)
    workdir.mkdir(parents=True, exist_ok=True)

    metrics = PlatformMetricStore(target)
    tasks = TaskStore(target)
    auth = AuthStore(target)
    sessions = AgentSessionStore(target)
    approvals = ApprovalStore(target, tenant_id="LPL-001")
    tool_log = ToolRunLog(target, tenant_id="LPL-001")

    def write(name, kind, body):
        path = workdir / name
        path.write_bytes((HEADERS[kind] + body).encode("utf-8"))
        return path

    # 1) 导入：美团缺 09-03（缺日），09-02→09-04 曝光翻 5 倍（跳变）
    metrics.import_csv(write("m.csv", "store",
        "2026-09-01,meituan,WH-LION-001,100,20,5,4,300.50,0,1,0,4.8\n"
        "2026-09-02,meituan,WH-LION-001,110,22,5,4,320.25,0,2,1,4.7\n"
        "2026-09-04,meituan,WH-LION-001,550,40,6,5,410.75,1,1,0,4.6\n"), STORE)
    metrics.import_csv(write("d.csv", "store",
        "2026-09-01,douyin,WH-LION-001,80,15,4,3,200.00,0,1,0,4.7\n"
        "2026-09-02,douyin,WH-LION-001,85,16,4,3,210.00,0,1,0,4.7\n"
        "2026-09-03,douyin,WH-LION-001,90,17,4,3,220.00,0,1,0,4.7\n"
        "2026-09-04,douyin,WH-LION-001,95,18,5,4,240.00,0,1,0,4.7\n"), STORE)
    metrics.import_packages_csv(write("p.csv", "package",
        "2026-09-01,meituan,WH-LION-001,MT-1632214010,AI全周小包2h,168,5,4,1200.00\n"
        "2026-09-02,meituan,WH-LION-001,MT-1632214010,AI全周小包2h,100,1,1,200.00\n"), STORE)
    metrics.import_reviews_csv(write("r.csv", "review",
        "MT-1024,meituan,WH-LION-001,2,2026-09-03 09:15:00,open,服务体验,\n"
        "MT-1001,meituan,WH-LION-001,5,2026-09-01 18:00:00,replied,环境安静,2026-09-01 19:00:00\n"), STORE)

    quality = metrics.data_quality(STORE, days=30)
    summary = metrics.period_summary(STORE, days=7)
    daily_rows = metrics.read_daily_metrics(STORE, "1900-01-01", "2999-12-31")
    review_rows = metrics.read_reviews(STORE)

    # 2) 快照 + 计划 + 落库（幂等）
    snapshot = build_snapshot(config, metrics, day, task_store=tasks)
    plan = generate_daily_plan(snapshot)
    first = tasks.save_plan(plan, STORE)
    second = tasks.save_plan(plan, STORE)

    # 3) 任务生命周期：接受 → 证据 → 完成 → 验收；再验证非法流转
    invite = next(t for t in tasks.list_tasks(STORE) if t["task_id"].endswith("review-invite"))
    errors: list[str] = []
    try:
        tasks.change_status(invite["task_id"], "done", "员工小李")      # 无证据
    except TaskStoreError as exc:
        errors.append(str(exc))
    tasks.change_status(invite["task_id"], "accepted", "员工小李")
    tasks.add_evidence(invite["task_id"], "脱敏订单号", "MT-1", "员工小李")
    tasks.change_status(invite["task_id"], "done", "员工小李")
    tasks.change_status(invite["task_id"], "verified", "武汉狮子座店店长")
    try:
        tasks.change_status(invite["task_id"], "accepted", "员工小李")  # 终态
    except TaskStoreError as exc:
        errors.append(str(exc))
    events = [(e["from_status"], e["to_status"]) for e in tasks.events(invite["task_id"])]

    # 4) 身份：建号 → 登录 → 解析 → 登出
    # 显式 user_id：create_user 默认随机生成，会让两个引擎的观测值天然不同
    user = auth.create_user("manager01", PASSWORD, "武汉狮子座店店长", "manager", "LPL-001", STORE,
                            user_id="u-parity")
    login = auth.login("manager01", PASSWORD)
    context = auth.resolve(login["token"])
    auth.record_audit(event_type="parity_check", actor_type="user", actor_id=user["user_id"],
                      tenant_id="LPL-001", store_id=STORE, object_type="test", object_id="parity",
                      after={"phone": "13812345678", "password": "should-be-masked"})
    audit = auth.list_audit(tenant_id="LPL-001", store_id=STORE, limit=5)
    auth.logout(login["token"])
    try:
        auth.resolve(login["token"])
        logout_blocked = False
    except SessionInvalid:
        logout_blocked = True

    # 5) 会话消息（bind_session 与 /api/chat 的真实流程一致）
    tasks.bind_session("s-parity", user["user_id"], "manager")
    sessions.append("s-parity", user["user_id"], "user", "数据齐不齐？")
    sessions.append("s-parity", user["user_id"], "assistant", "近 30 天缺 1 天。", "data_quality")
    history = sessions.history("s-parity", user["user_id"])

    # 5.5) 审批流 + 工具调用留痕
    approvals.sync_from_plan(plan, STORE)
    pending = approvals.list(STORE, status="pending")
    decision = apply_decision(approvals, tasks, pending[0]["approval_id"], "approved",
                              "武汉狮子座店店长", "同意") if pending else {"task": None}
    tool_log.record(STORE, "api:data_quality", actor_id="manager01", session_id="s-parity",
                    input_payload={"days": 30, "phone": "13812345678"},
                    output_payload={"missing_day_count": quality["summary"]["missing_day_count"]})

    # 6) 店长日报（文本整体比对）
    from agent_tools import StoreTools
    tools = StoreTools(config, metrics, tasks, config_path=ROOT / "store_config.json")
    report = tools.daily_report(day)

    # 时间往返：写进去的本地时间读回来必须还是同一个"墙上时钟"
    probe_due = "2026-09-03T21:00:00+08:00"
    probe_task = plan["tasks"][0]["task_id"]
    with tasks._connection() as conn:
        conn.execute("update action_tasks set due_at = ? where task_id = ?", (probe_due, probe_task))
    time_roundtrip = tasks.get_task(probe_task)["due_at"]

    return {
        "time_roundtrip": time_roundtrip,
        "approvals": [(a["status"], a["action_type"], bool(a["task_id"])) for a in approvals.list(STORE)],
        "approval_task_status": (decision.get("task") or {}).get("status"),
        "tool_runs": [(r["tool_name"], r["status"], r["mode"]) for r in tool_log.list(STORE)],
        "quality": {
            "has_data": quality["summary"]["has_data"],
            "missing_day_count": quality["summary"]["missing_day_count"],
            "anomaly_count": quality["summary"]["anomaly_count"],
            "coverage": {key: {"expected": item["expected_days"], "actual": item["actual_days"],
                               "missing": item["missing_dates"]}
                         for key, item in quality["coverage"].items()},
            "anomalies": [(item["platform"], item["metric"], item["date"], item["change_percent"])
                          for item in quality["anomalies"]],
        },
        "metrics": summary["platforms"],
        "daily_rows": sorted((row["metric_date"], row["platform"], float(row["gmv"]),
                              int(row["paid_orders"])) for row in daily_rows),
        "reviews": sorted((row["review_id"], float(row["rating"]), row["status"]) for row in review_rows),
        "plan": [(t["task_id"], t["priority"], t["requires_approval"], t["assignee"]) for t in plan["tasks"]],
        "save_plan": (first["created"], first["updated"], second["created"], second["updated"]),
        "count_by_status": tasks.count_by_status(STORE),
        "events": events,
        "errors": errors,
        "user": (user["user_id"], user["role"], context.role, context.store_id,
                 sorted(context.permissions)),
        "login_expires": context.expires_at[:10],
        "logout_blocked": logout_blocked,
        "audit_event_types": sorted({row["event_type"] for row in audit}),
        "audit_object_id": audit[0]["object_id"],
        "audit_payload_redacted": "should-be-masked" not in (audit[0]["after_json"] or "")
                                  and "138****5678" in (audit[0]["after_json"] or ""),
        "history": [(item["message_role"], item["content"]) for item in history],
        "report": report["report"],
    }


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能跑 PostgreSQL 侧的运行时等价性验证")
class PgRuntimeParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_parity_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def setUp(cls):  # 每个用例前清空 PG，避免相互影响
        import psycopg
        with psycopg.connect(cls.dsn) as conn, conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.close()

    def test_sqlite_and_postgres_produce_identical_outcomes(self):
        sqlite_obs = run_scenario(self.tmp / "sqlite.db", self.tmp / "sqlite_files")
        pg_obs = run_scenario(self.dsn, self.tmp / "pg_files")

        for key in sqlite_obs:
            with self.subTest(field=key):
                self.assertEqual(sqlite_obs[key], pg_obs[key], f"{key} 在两种引擎上不一致")

        # 关键断言单独再钉一遍，避免"两边都空"式的假通过
        self.assertEqual(sqlite_obs["quality"]["missing_day_count"], 1)
        self.assertGreaterEqual(sqlite_obs["quality"]["anomaly_count"], 1)
        self.assertEqual(len(sqlite_obs["plan"]), 3)
        self.assertEqual(sqlite_obs["save_plan"], (3, 0, 0, 3))
        self.assertTrue(sqlite_obs["logout_blocked"])
        self.assertTrue(sqlite_obs["audit_payload_redacted"])
        self.assertIn("经营日报", sqlite_obs["report"])
        # 时间往返：两种引擎都必须读回同一个本地墙上时钟（不能出现 ±8 小时漂移）
        self.assertEqual(sqlite_obs["time_roundtrip"], "2026-09-03T21:00:00")

    def test_pg_tables_actually_hold_the_data(self):
        import psycopg
        run_scenario(self.dsn, self.tmp / "pg_files2")
        counts = {}
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            for table in ("platform_metrics", "package_metrics", "reviews", "action_tasks",
                          "task_evidence", "task_events", "users", "memberships", "auth_sessions",
                          "audit_events", "agent_sessions", "agent_messages"):
                cur.execute(f"select count(*) from {table}")
                counts[table] = int(cur.fetchone()[0])
        for table, count in counts.items():
            with self.subTest(table=table):
                self.assertGreater(count, 0, f"{table} 在 PostgreSQL 中应有数据")
        # 时间与 JSON 类型确实按 PG 类型落地
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("select due_at, target_json, approval_required from action_tasks limit 1")
            due_at, target, approval = cur.fetchone()
            self.assertIsNotNone(due_at.tzinfo)
            self.assertIsInstance(target, dict)
            self.assertIsInstance(approval, bool)
            cur.execute("select metric_date, gmv from platform_metrics order by metric_date limit 1")
            metric_date, gmv = cur.fetchone()
            self.assertEqual(str(metric_date), "2026-09-01")
            self.assertEqual(float(gmv), 300.5)

    def test_pg_runtime_supports_store_cli_style_operations(self):
        """模拟 store_cli / 服务端的真实调用序列（快照→计划→任务→日报）在 PG 上可用。"""
        config = load_config(ROOT / "store_config.json")
        metrics = PlatformMetricStore(self.dsn)
        tasks = TaskStore(self.dsn)
        auth = AuthStore(self.dsn)
        metrics.import_csv(self._seed_file("store.csv", "store",
            "2026-09-01,meituan,WH-LION-001,100,20,5,4,300.00,0,1,0,4.8\n"), STORE)
        snapshot = build_snapshot(config, metrics, date(2026, 9, 2), task_store=tasks)
        plan = generate_daily_plan(snapshot)
        saved = tasks.save_plan(plan, STORE)
        self.assertGreater(saved["created"], 0)
        self.assertEqual(tasks.save_plan(plan, STORE)["updated"], saved["created"])
        auth.create_user("ops01", PASSWORD, "线上运营", "operator", "LPL-001", STORE)
        context = auth.resolve(auth.login("ops01", PASSWORD)["token"])
        self.assertFalse(context.has("tasks:verify"))
        self.assertTrue(context.has("pricing:draft"))

    def _seed_file(self, name, kind, body):
        path = self.tmp / name
        path.write_bytes((HEADERS[kind] + body).encode("utf-8"))
        return path


if __name__ == "__main__":
    unittest.main()
