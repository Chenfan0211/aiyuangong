# -*- coding: utf-8 -*-
"""批次 6：HTTP 接口层的角色隔离、会话绑定与写操作门禁测试（每个用例独立起服务）。"""
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
from snapshot_builder import build_snapshot
from store_agent import AgentSessionStore, StoreOperationsAgent
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE_ID = "WH-LION-001"
STORE_HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG_HEADER = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
REV_HEADER = "review_id,platform,store_id,rating,created_at,status,category,replied_at\n"


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = json.loads((ROOT / "store_config.json").read_text(encoding="utf-8"))
        self.metrics = PlatformMetricStore(root / "metrics.db")
        self.tasks = TaskStore(root / "ops.db")
        self.today = date.today()
        self.yesterday = self.today - timedelta(days=1)

        def write(name, header, body):
            path = root / name
            path.write_bytes((header + body).encode("utf-8"))
            return path

        self.metrics.import_csv(write("m.csv", STORE_HEADER,
            f"{self.yesterday},meituan,{STORE_ID},100,20,5,4,300,0,1,0,4.8\n"
            f"{self.today},meituan,{STORE_ID},110,22,5,4,320,0,1,0,4.8\n"), STORE_ID)
        self.metrics.import_csv(write("d.csv", STORE_HEADER,
            f"{self.today},douyin,{STORE_ID},85,16,4,3,210,0,1,0,4.7\n"), STORE_ID)
        self.metrics.import_packages_csv(write("p.csv", PKG_HEADER,
            f"{self.today},meituan,{STORE_ID},MT-1632214010,AI全周小包2h,168,5,4,1200\n"), STORE_ID)
        self.metrics.import_reviews_csv(write("r.csv", REV_HEADER,
            f"MT-1024,meituan,{STORE_ID},2,{self.today} 09:15:00,open,服务体验,\n"), STORE_ID)

        self.tools = StoreTools(self.config, self.metrics, self.tasks, config_path=ROOT / "store_config.json")
        snapshot = build_snapshot(self.config, self.metrics, self.today, task_store=self.tasks)
        self.tasks.save_plan(generate_daily_plan(snapshot), STORE_ID)

        AgentHandler.agent = StoreOperationsAgent.from_tools(self.config, self.tools)
        AgentHandler.tools = self.tools
        AgentHandler.task_store = self.tasks
        AgentHandler.sessions = AgentSessionStore(root / "chat.db")
        AgentHandler.auth = AuthStore(root / "ops.db")
        AgentHandler.approvals = ApprovalStore(root / "ops.db")
        AgentHandler.tool_log = ToolRunLog(root / "ops.db")
        AgentHandler.audit_center = AuditCenter(root / "ops.db")
        # 本文件验证的是角色过滤与任务流程本身，身份用 demo 模式注入；
        # 真实登录/令牌/越权另见 test_auth_api.py。
        AgentHandler.auth_mode = "demo"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.invite = next(t for t in self.tools.tasks_for({"role": "manager"})
                           if t["task_id"].endswith("review-invite"))
        self.negative = next(t for t in self.tools.tasks_for({"role": "manager"})
                             if "negative" in t["task_id"])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def call(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def json_call(self, method, path, payload=None):
        status, body = self.call(method, path, payload)
        return status, json.loads(body)

    def test_bootstrap_and_task_visibility(self):
        status, data = self.json_call("GET", "/api/bootstrap?user_id=manager-001")
        self.assertEqual((status, data["mode"], data["task_count"]), (200, "tools", 3))
        self.assertEqual(data["auth_mode"], "demo")
        status, manager = self.json_call("GET", "/api/tasks/today?user_id=manager-001&status=open")
        self.assertEqual((status, len(manager["tasks"])), (200, 3))
        status, staff = self.json_call("GET", "/api/tasks/today?user_id=staff-001&status=open")
        self.assertEqual((status, [t["task_id"] for t in staff["tasks"]]), (200, [self.invite["task_id"]]))

    def test_manager_only_endpoints(self):
        for path in ("/api/data-quality?user_id=staff-001", "/api/reports/daily?user_id=staff-001"):
            with self.subTest(path=path):
                status, _ = self.call("GET", path)
                self.assertEqual(status, 403)
        status, quality = self.json_call("GET", "/api/data-quality?user_id=manager-001&days=30")
        self.assertEqual((status, "coverage" in quality), (200, True))
        status, report = self.json_call("GET", "/api/reports/daily?user_id=manager-001")
        self.assertEqual(status, 200)
        self.assertIn("经营日报", report["report"])

    def test_staff_can_read_aggregates_but_never_financial_fields(self):
        """店员可以看漏斗聚合，但响应里**不存在** GMV/退款字段（是裁剪，不是文案隐藏）。"""
        status, summary = self.json_call("GET", "/api/metrics/summary?user_id=staff-001&days=7")
        self.assertEqual(status, 200)
        self.assertFalse(summary["financial_included"])
        metrics = summary["platforms"]["meituan"]["metrics"]
        for field in ("gmv", "refunds", "refund_rate"):
            self.assertNotIn(field, metrics)
        self.assertIn("impressions", metrics)

        status, summary = self.json_call("GET", "/api/metrics/summary?user_id=manager-001&days=7")
        self.assertEqual(status, 200)
        self.assertTrue(summary["financial_included"])
        self.assertIn("gmv", summary["platforms"]["meituan"]["metrics"])

    def test_task_detail_permission(self):
        status, _ = self.call("GET", f"/api/tasks/{self.negative['task_id']}?user_id=staff-001")
        self.assertEqual(status, 403)
        status, detail = self.json_call("GET", f"/api/tasks/{self.invite['task_id']}?user_id=staff-001")
        self.assertEqual(status, 200)
        self.assertIn("events", detail)

    def test_chat_intents_and_session_binding(self):
        status, result = self.json_call("POST", "/api/chat",
                                        {"user_id": "manager-001", "session_id": "s-m", "message": "数据齐不齐？"})
        self.assertEqual((status, result["intent"]), (200, "data_quality"))
        self.assertTrue(all(isinstance(item, dict) for item in result["sources"]))
        status, _ = self.call("POST", "/api/chat", {"user_id": "staff-001", "session_id": "s-m", "message": "hi"})
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/api/chat", {"user_id": "nobody", "session_id": "s-x", "message": "hi"})
        self.assertEqual(status, 403)

    def test_write_flow_with_evidence_gate(self):
        session = "s-staff"
        status, _ = self.call("POST", f"/api/tasks/{self.invite['task_id']}/status",
                              {"user_id": "staff-001", "session_id": session, "status": "done"})
        self.assertEqual(status, 409)                     # 无证据
        status, task = self.json_call("POST", f"/api/tasks/{self.invite['task_id']}/status",
                                      {"user_id": "staff-001", "session_id": session, "status": "accepted"})
        self.assertEqual((status, task["status"]), (200, "accepted"))
        status, evidence = self.json_call("POST", f"/api/tasks/{self.invite['task_id']}/evidence",
                                          {"user_id": "staff-001", "session_id": session,
                                           "evidence_type": "脱敏订单号", "object_ref": "MT-1"})
        self.assertEqual(status, 200)
        self.assertEqual(evidence["evidence"]["submitted_by"], "员工小李")
        status, task = self.json_call("POST", f"/api/tasks/{self.invite['task_id']}/status",
                                      {"user_id": "staff-001", "session_id": session, "status": "done"})
        self.assertEqual((status, task["status"]), (200, "done"))
        status, _ = self.call("POST", f"/api/tasks/{self.invite['task_id']}/status",
                              {"user_id": "staff-001", "session_id": session, "status": "verified"})
        self.assertEqual(status, 403)                     # 店员不能自行验收
        status, task = self.json_call("POST", f"/api/tasks/{self.invite['task_id']}/status",
                                      {"user_id": "manager-001", "session_id": "s-mgr", "status": "verified"})
        self.assertEqual((status, task["status"]), (200, "verified"))

    def test_cannot_touch_someone_elses_task(self):
        status, _ = self.call("POST", f"/api/tasks/{self.negative['task_id']}/evidence",
                              {"user_id": "staff-001", "session_id": "s-staff",
                               "evidence_type": "截图", "object_ref": "x"})
        self.assertEqual(status, 403)
        status, _ = self.call("POST", f"/api/tasks/{self.invite['task_id']}/status",
                              {"user_id": "manager-001", "session_id": "s-x", "status": "不存在的状态"})
        self.assertEqual(status, 400)

    def test_static_pages_and_new_panels(self):
        for path in ("/", "/agent.html"):
            with self.subTest(path=path):
                status, html = self.call("GET", path)
                self.assertEqual(status, 200)
                for marker in ('id="taskCards"', 'id="qualityStatus"', 'id="reportBtn"'):
                    self.assertIn(marker, html)
        status, html = self.call("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertIn("门店运营系统", html)


if __name__ == "__main__":
    unittest.main()
