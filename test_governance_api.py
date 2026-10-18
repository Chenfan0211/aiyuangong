# -*- coding: utf-8 -*-
"""批次 9：治理接口（审批 / 审计 / 工具留痕）在两种传输层上的行为一致性测试。

同一个测试类跑两遍：`--transport stdlib`（agent_server.py）与 `fastapi`（app_fastapi.py），
因此两套入口的权限、错误码与数据口径必须完全一致。
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from agent_server import AgentHandler
from agent_tools import StoreTools
from auth import AuthStore
from governance import ApprovalStore, AuditCenter, ToolRunLog
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import build_snapshot, load_config
from store_agent import AgentSessionStore, StoreOperationsAgent
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
STORE_B = "WH-LION-002"
TENANT = "LPL-001"
PASSWORD = "ShiZiZuo-2026-store"
STORE_HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG_HEADER = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
REV_HEADER = "review_id,platform,store_id,rating,created_at,status,category,replied_at\n"


class GovernanceApiBase:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = load_config(ROOT / "store_config.json")
        self.metrics_db, self.store_db, self.chat_db = root / "metrics.db", root / "ops.db", root / "chat.db"
        self.metrics = PlatformMetricStore(self.metrics_db, tenant_id=TENANT)
        self.tasks = TaskStore(self.store_db, tenant_id=TENANT)
        self.auth = AuthStore(self.store_db, tenant_id=TENANT)
        self.approvals = ApprovalStore(self.store_db, tenant_id=TENANT)
        self.tool_log = ToolRunLog(self.store_db, tenant_id=TENANT)
        self.center = AuditCenter(self.store_db, tenant_id=TENANT)
        self.today = date.today()
        self.yesterday = self.today - timedelta(days=1)

        def write(name, header, body):
            path = root / name
            path.write_bytes((header + body).encode("utf-8"))
            return path

        self.metrics.import_csv(write("m.csv", STORE_HEADER,
            f"{self.yesterday},meituan,{STORE},100,20,5,4,300,0,1,0,4.8\n"), STORE)
        self.metrics.import_packages_csv(write("p.csv", PKG_HEADER,
            f"{self.today},meituan,{STORE},MT-1632214010,AI全周小包2h,168,5,4,1200\n"), STORE)
        self.metrics.import_reviews_csv(write("r.csv", REV_HEADER,
            f"MT-1024,meituan,{STORE},2,{self.today} 09:15:00,open,服务体验,\n"), STORE)

        tools = StoreTools(self.config, self.metrics, self.tasks, config_path=ROOT / "store_config.json")
        snapshot = build_snapshot(self.config, self.metrics, self.today, task_store=self.tasks)
        plan = generate_daily_plan(snapshot)
        self.tasks.save_plan(plan, STORE)
        self.approvals.sync_from_plan(plan, STORE)
        self.gated_task = next(t for t in plan["tasks"] if t["requires_approval"])["task_id"]

        for username, display, role in (("owner01", "店主老王", "owner"),
                                        ("manager01", "武汉狮子座店店长", "manager"),
                                        ("staff01", "员工小李", "employee")):
            self.auth.create_user(username, PASSWORD, display, role, TENANT, STORE)
        # 另一个门店的数据，用于隔离断言
        self.auth.record_audit(event_type="login_success", actor_type="user", actor_id="other-store",
                               tenant_id=TENANT, store_id=STORE_B, object_type="session", object_id="s-b")
        self.approvals.request(STORE_B, "pricing:change", {"note": "B 店"}, requested_by="B店店长")

        if self.transport == "fastapi":
            from fastapi.testclient import TestClient
            from app_fastapi import create_app
            self.client = TestClient(create_app(config_path=ROOT / "store_config.json",
                                                metrics_db=self.metrics_db, store_db=self.store_db,
                                                chat_db=self.chat_db, auth_mode="session"))
        else:
            AgentHandler.agent = StoreOperationsAgent.from_tools(self.config, tools)
            AgentHandler.tools = tools
            AgentHandler.task_store = self.tasks
            AgentHandler.auth = self.auth
            AgentHandler.approvals = self.approvals
            AgentHandler.tool_log = self.tool_log
            AgentHandler.audit_center = self.center
            AgentHandler.auth_mode = "session"
            AgentHandler.sessions = AgentSessionStore(self.chat_db, tenant_id=TENANT)
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
            self.port = self.server.server_address[1]
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()

        self.tokens = {name: self.login(name) for name in ("owner01", "manager01", "staff01")}
        if hasattr(self, "client"):
            self.client.cookies.clear()

    def tearDown(self):
        if self.transport == "fastapi":
            self.client.close()
        else:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.temp.cleanup()

    # ---------------- 传输无关的调用助手 ----------------

    def _request(self, method, path, payload=None, token=None):
        headers = {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.transport == "fastapi":
            response = self.client.request(method, path, json=payload, headers=headers)
            return response.status_code, response.text
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def call(self, method, path, payload=None, token=None):
        status, body = self._request(method, path, payload, token)
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, body

    def error_of(self, body):
        return body.get("error") or body.get("detail") or ""

    def login(self, username, password=PASSWORD):
        status, data = self.call("POST", "/api/auth/login", {"username": username, "password": password})
        self.assertEqual(status, 200, data)
        return data["token"]

    # ---------------- 用例 ----------------

    def test_approval_queue_permissions(self):
        status, _ = self.call("GET", "/api/approvals")
        self.assertEqual(status, 401)
        status, body = self.call("GET", "/api/approvals", token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, body = self.call("GET", "/api/approvals?status=pending", token=self.tokens["manager01"])
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(len(body["approvals"]), 1)
        self.assertEqual(body["count_by_status"].get("pending"), 1)
        self.assertEqual({a["store_id"] for a in body["approvals"]}, {STORE})     # 跨店不可见

    def test_decision_moves_task_and_writes_audit(self):
        approval = self.approvals.list(STORE, status="pending")[0]
        status, result = self.call("POST", f"/api/approvals/{approval['approval_id']}/decide",
                                   {"decision": "approved", "reason": "毛利可接受"},
                                   token=self.tokens["manager01"])
        self.assertEqual(status, 200, result)
        self.assertEqual(result["approval"]["status"], "approved")
        self.assertEqual(result["task"]["status"], "ready")
        self.assertEqual(self.tasks.get_task(self.gated_task)["status"], "ready")
        events = [e["event_type"] for e in self.center.list_events(STORE, limit=20)]
        self.assertIn("approval_decided", events)

    def test_rejection_requires_reason_and_marks_task(self):
        approval = self.approvals.list(STORE, status="pending")[0]
        status, body = self.call("POST", f"/api/approvals/{approval['approval_id']}/decide",
                                 {"decision": "rejected", "reason": ""}, token=self.tokens["manager01"])
        self.assertEqual(status, 400)
        self.assertIn("驳回必须填写原因", self.error_of(body))
        status, result = self.call("POST", f"/api/approvals/{approval['approval_id']}/decide",
                                   {"decision": "rejected", "reason": "成本数据不全"},
                                   token=self.tokens["manager01"])
        self.assertEqual(status, 200, result)
        self.assertEqual(result["task"]["status"], "rejected")

    def test_employee_can_request_approval_but_not_decide(self):
        status, created = self.call("POST", "/api/approvals",
                                    {"action_type": "pricing:change",
                                     "payload": {"package_id": "MT-1632214010", "phone": "13812345678"},
                                     "reason": "想试单变量调价"},
                                    token=self.tokens["staff01"])
        self.assertEqual(status, 200, created)
        self.assertEqual(created["status"], "pending")
        status, _ = self.call("POST", f"/api/approvals/{created['approval_id']}/decide",
                              {"decision": "approved", "reason": "ok"}, token=self.tokens["staff01"])
        self.assertEqual(status, 403)                                              # 店员不能决策
        status, body = self.call("GET", "/api/approvals?status=pending", token=self.tokens["manager01"])
        self.assertEqual(body["count_by_status"]["pending"], 2)

    def test_audit_requires_permission_and_is_store_scoped(self):
        status, _ = self.call("GET", "/api/audit", token=self.tokens["staff01"])
        self.assertEqual(status, 403)                                             # 店员无 audit:read
        status, body = self.call("GET", "/api/audit?limit=20", token=self.tokens["owner01"])
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(body["summary"]["total"], 3)                      # 3 次登录
        self.assertEqual({e["store_id"] for e in body["events"]}, {STORE})        # 另一门店不可见
        self.assertIn("login_success", body["event_types"])
        status, filtered = self.call("GET", "/api/audit?event_type=login_success&actor_id=manager01",
                                     token=self.tokens["owner01"])
        self.assertEqual(status, 200)
        self.assertTrue(all(e["event_type"] == "login_success" and e["actor_id"] == "manager01"
                            for e in filtered["events"]))
        status, denied = self.call("GET", "/api/audit?event_type=permission_denied", token=self.tokens["owner01"])
        self.assertGreaterEqual(len(denied["events"]), 1)                          # 上面的越权被记录

    def test_tool_runs_are_recorded_and_redacted(self):
        self.call("GET", "/api/data-quality?days=30", token=self.tokens["manager01"])
        self.call("GET", "/api/reports/daily", token=self.tokens["manager01"])
        status, body = self.call("GET", "/api/tool-runs?limit=20", token=self.tokens["owner01"])
        self.assertEqual(status, 200, body)
        tools_seen = {r["tool_name"] for r in body["runs"]}
        self.assertIn("api:data_quality", tools_seen)
        self.assertIn("api:daily_report", tools_seen)
        status, _ = self.call("GET", "/api/tool-runs", token=self.tokens["staff01"])
        self.assertEqual(status, 403)

    def test_governance_page_is_served(self):
        status, html = self._request("GET", "/governance")
        self.assertEqual(status, 200)
        for marker in ('id="queue"', 'id="auditBody"', 'id="toolBody"'):
            self.assertIn(marker, html)
        script = (Path(__file__).resolve().parent / "static" / "governance.js").read_text(encoding="utf-8")
        self.assertIn("/api/approvals", script)


class GovernanceApiStdlibTests(GovernanceApiBase, unittest.TestCase):
    """标准库传输层（agent_server.py）。"""

    transport = "stdlib"


class GovernanceApiFastApiTests(GovernanceApiBase, unittest.TestCase):
    """FastAPI 传输层（app_fastapi.py）。"""

    transport = "fastapi"


if __name__ == "__main__":
    unittest.main()
