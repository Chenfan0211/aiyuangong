# -*- coding: utf-8 -*-
"""批次 7：会话模式下的登录、令牌、RBAC、门店隔离与审计（HTTP 层）。"""
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
from auth import MAX_LOGIN_FAILURES, AuthStore
from governance import ApprovalStore, AuditCenter, ToolRunLog
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import build_snapshot
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


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = json.loads((ROOT / "store_config.json").read_text(encoding="utf-8"))
        self.metrics = PlatformMetricStore(root / "metrics.db")
        self.tasks = TaskStore(root / "ops.db")
        self.auth = AuthStore(root / "ops.db")
        today, yesterday = date.today(), date.today() - timedelta(days=1)

        def write(name, header, body):
            path = root / name
            path.write_bytes((header + body).encode("utf-8"))
            return path

        self.metrics.import_csv(write("m.csv", STORE_HEADER,
            f"{yesterday},meituan,{STORE},100,20,5,4,300,0,1,0,4.8\n"
            f"{today},meituan,{STORE},110,22,5,4,320,0,1,0,4.8\n"), STORE)
        self.metrics.import_packages_csv(write("p.csv", PKG_HEADER,
            f"{today},meituan,{STORE},MT-1632214010,AI全周小包2h,168,5,4,1200\n"), STORE)
        self.metrics.import_reviews_csv(write("r.csv", REV_HEADER,
            f"MT-1024,meituan,{STORE},2,{today} 09:15:00,open,服务体验,\n"), STORE)

        self.tools = StoreTools(self.config, self.metrics, self.tasks, config_path=ROOT / "store_config.json")
        snapshot = build_snapshot(self.config, self.metrics, today, task_store=self.tasks)
        self.tasks.save_plan(generate_daily_plan(snapshot), STORE)
        # 另一个门店的任务，用于跨店隔离测试
        self.tasks.save_plan({
            "date": date.today().isoformat(),
            "tasks": [{"task_id": "B-store-only", "category": "reputation", "title": "B 店专属任务",
                       "assignee": "B店店长", "priority": "high", "due_at": f"{today}T18:00:00+08:00",
                       "target": {}, "evidence_required": [], "reason": "隔离测试",
                       "requires_approval": False}],
        }, STORE_B)

        for username, display, role in (
            ("owner01", "店主老王", "owner"),
            ("manager01", "武汉狮子座店店长", "manager"),
            ("staff01", "员工小李", "employee"),
            ("ops01", "线上运营", "operator"),
        ):
            self.auth.create_user(username, PASSWORD, display, role, TENANT, STORE)

        AgentHandler.agent = StoreOperationsAgent.from_tools(self.config, self.tools)
        AgentHandler.tools = self.tools
        AgentHandler.task_store = self.tasks
        AgentHandler.auth = self.auth
        AgentHandler.approvals = ApprovalStore(self.tasks.db.target)
        AgentHandler.tool_log = ToolRunLog(self.tasks.db.target)
        AgentHandler.audit_center = AuditCenter(self.tasks.db.target)
        AgentHandler.auth_mode = "session"
        AgentHandler.sessions = AgentSessionStore(root / "chat.db")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def call(self, method, path, payload=None, token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {}
        if data:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def json_call(self, method, path, payload=None, token=None):
        status, body = self.call(method, path, payload, token)
        return status, json.loads(body)

    def login(self, username, password=PASSWORD):
        status, data = self.json_call("POST", "/api/auth/login", {"username": username, "password": password})
        return status, data

    def token_for(self, username, password=PASSWORD):
        status, data = self.login(username, password)
        self.assertEqual(status, 200, data)
        return data["token"]

    # ---------------- 令牌门禁 ----------------

    def test_all_data_endpoints_require_token(self):
        for path in ("/api/bootstrap", "/api/tasks/today", "/api/data-quality", "/api/reports/daily",
                     "/api/metrics/summary"):
            with self.subTest(path=path):
                status, _ = self.call("GET", path)
                self.assertEqual(status, 401)
        status, _ = self.call("GET", "/api/bootstrap", token="not-a-real-token")
        self.assertEqual(status, 401)
        status, _ = self.call("POST", "/api/chat", {"session_id": "s1", "message": "hi"})
        self.assertEqual(status, 401)

    def test_login_me_logout_flow(self):
        status, data = self.login("manager01")
        self.assertEqual(status, 200)
        self.assertTrue(data["token"])
        self.assertEqual(data["user"]["role_label"], "店长")
        self.assertIn("tasks:verify", data["permissions"])
        token = data["token"]

        status, me = self.json_call("GET", "/api/auth/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(me["store_id"], STORE)
        self.assertEqual(me["user"]["name"], "武汉狮子座店店长")

        status, out = self.json_call("POST", "/api/auth/logout", {}, token=token)
        self.assertEqual((status, out["revoked"]), (200, True))
        status, _ = self.call("GET", "/api/auth/me", token=token)
        self.assertEqual(status, 401)

    def test_login_failure_and_lockout_over_http(self):
        status, _ = self.login("manager01", "wrong-password-1")
        self.assertEqual(status, 401)
        for _ in range(MAX_LOGIN_FAILURES):
            self.login("staff01", "wrong-password-1")
        status, data = self.login("staff01")
        self.assertEqual(status, 423)
        self.assertIn("登录失败次数过多", data["error"])

    def test_user_id_param_cannot_escalate(self):
        """店员拿店长 user_id 也不能提权：身份只认令牌。"""
        staff = self.token_for("staff01")
        status, boot = self.json_call("GET", f"/api/bootstrap?user_id=manager-001", token=staff)
        self.assertEqual(status, 200)
        self.assertEqual(boot["user"]["role"], "employee")
        self.assertNotIn("report:read", boot["permissions"])

        status, _ = self.call("GET", "/api/reports/daily?user_id=manager-001", token=staff)
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/api/data-quality?user_id=manager-001", token=staff)
        self.assertEqual(status, 403)
        status, _ = self.call("GET", f"/api/tasks/today?user_id=manager-001&status=open", token=staff)
        self.assertEqual(status, 200)
        tasks = json.loads(self.call("GET", "/api/tasks/today?status=open", token=staff)[1])["tasks"]
        self.assertEqual([t["assignee_id"] for t in tasks], ["员工小李"])

    def test_financial_fields_are_absent_for_employee(self):
        staff = self.token_for("staff01")
        status, summary = self.json_call("GET", "/api/metrics/summary?days=7", token=staff)
        self.assertEqual(status, 200)
        metrics = summary["platforms"]["meituan"]["metrics"]
        for field in ("gmv", "refunds", "refund_rate"):
            self.assertNotIn(field, metrics)
        owner = self.token_for("owner01")
        _, summary = self.json_call("GET", "/api/metrics/summary?days=7", token=owner)
        self.assertIn("gmv", summary["platforms"]["meituan"]["metrics"])

    def test_store_isolation(self):
        manager = self.token_for("manager01")
        status, data = self.json_call("GET", "/api/tasks/today?status=open", token=manager)
        self.assertEqual(status, 200)
        self.assertNotIn("B-store-only", [t["task_id"] for t in data["tasks"]])
        status, _ = self.call("GET", "/api/tasks/B-store-only", token=manager)
        self.assertEqual(status, 403)          # 跨店任务不可见

    def test_write_flow_and_permissions(self):
        staff = self.token_for("staff01")
        manager = self.token_for("manager01")
        tasks = json.loads(self.call("GET", "/api/tasks/today?status=open", token=staff)[1])["tasks"]
        invite = next(t for t in tasks if t["task_id"].endswith("review-invite"))

        status, _ = self.call("POST", f"/api/tasks/{invite['task_id']}/status",
                              {"session_id": "s-staff", "status": "done"}, token=staff)
        self.assertEqual(status, 409)                                  # 无证据
        status, task = self.json_call("POST", f"/api/tasks/{invite['task_id']}/status",
                                      {"session_id": "s-staff", "status": "accepted"}, token=staff)
        self.assertEqual((status, task["status"]), (200, "accepted"))
        status, evidence = self.json_call("POST", f"/api/tasks/{invite['task_id']}/evidence",
                                          {"session_id": "s-staff", "evidence_type": "脱敏订单号",
                                           "object_ref": "MT-0718"}, token=staff)
        self.assertEqual(status, 200)
        self.assertEqual(evidence["evidence"]["submitted_by"], "员工小李")
        status, task = self.json_call("POST", f"/api/tasks/{invite['task_id']}/status",
                                      {"session_id": "s-staff", "status": "done"}, token=staff)
        self.assertEqual((status, task["status"]), (200, "done"))
        status, _ = self.call("POST", f"/api/tasks/{invite['task_id']}/status",
                              {"session_id": "s-staff", "status": "verified"}, token=staff)
        self.assertEqual(status, 403)                                  # 店员不能自行验收
        status, task = self.json_call("POST", f"/api/tasks/{invite['task_id']}/status",
                                      {"session_id": "s-manager", "status": "verified"}, token=manager)
        self.assertEqual((status, task["status"]), (200, "verified"))

        events = [row["event_type"] for row in self.auth.list_audit(store_id=STORE, limit=50)]
        self.assertIn("task_status_changed", events)
        self.assertIn("task_evidence_submitted", events)
        self.assertIn("login_success", events)

    def test_session_hijack_is_rejected(self):
        manager = self.token_for("manager01")
        staff = self.token_for("staff01")
        status, _ = self.json_call("POST", "/api/chat",
                                   {"session_id": "shared", "message": "hi"}, token=manager)
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/api/chat", {"session_id": "shared", "message": "hi"}, token=staff)
        self.assertEqual(status, 403)

    def test_demo_mode_is_explicit_opt_in(self):
        AgentHandler.auth_mode = "demo"
        try:
            status, data = self.json_call("GET", "/api/bootstrap?user_id=manager-001")
            self.assertEqual((status, data["auth_mode"]), (200, "demo"))
        finally:
            AgentHandler.auth_mode = "session"


if __name__ == "__main__":
    unittest.main()
