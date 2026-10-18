# -*- coding: utf-8 -*-
"""批次 8：FastAPI 传输层与 stdlib 版的行为等价性（认证、权限、门店隔离、写流程）。"""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import build_snapshot
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


class FastApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.metrics = PlatformMetricStore(root / "metrics.db")
        self.tasks = TaskStore(root / "ops.db")
        self.auth = AuthStore(root / "ops.db")
        self.config = json.loads((ROOT / "store_config.json").read_text(encoding="utf-8"))
        today, yesterday = date.today(), date.today() - timedelta(days=1)

        def write(name, header, body):
            path = root / name
            path.write_bytes((header + body).encode("utf-8"))
            return path

        self.metrics.import_csv(write("m.csv", STORE_HEADER,
            f"{yesterday},meituan,{STORE},100,20,5,4,300,0,1,0,4.8\n"
            f"{today},meituan,{STORE},110,22,5,4,320,0,1,0,4.8\n"), STORE)
        self.metrics.import_packages_csv(write("p.csv", PKG_HEADER,
            f"{today},meituan,{STORE},MT-1,小包,168,5,4,1200\n"), STORE)
        self.metrics.import_reviews_csv(write("r.csv", REV_HEADER,
            f"MT-1024,meituan,{STORE},2,{today} 09:15:00,open,服务体验,\n"), STORE)

        from agent_tools import StoreTools
        tools = StoreTools(self.config, self.metrics, self.tasks, config_path=ROOT / "store_config.json")
        snapshot = build_snapshot(self.config, self.metrics, today, task_store=self.tasks)
        self.tasks.save_plan(generate_daily_plan(snapshot), STORE)
        self.tasks.save_plan({
            "date": today.isoformat(),
            "tasks": [{"task_id": "B-store-only", "category": "reputation", "title": "B 店任务",
                       "assignee": "B店店长", "priority": "high", "due_at": f"{today}T18:00:00+08:00",
                       "target": {}, "evidence_required": [], "reason": "隔离测试",
                       "requires_approval": False}],
        }, STORE_B)
        for username, display, role in (("owner01", "店主老王", "owner"),
                                        ("manager01", "武汉狮子座店店长", "manager"),
                                        ("staff01", "员工小李", "employee")):
            self.auth.create_user(username, PASSWORD, display, role, TENANT, STORE)

        self.app = create_app(config_path=ROOT / "store_config.json", metrics_db=root / "metrics.db",
                              store_db=root / "ops.db", chat_db=root / "chat.db", auth_mode="session")
        self.client = TestClient(self.app)
        self.tokens = {name: self.login(name) for name in ("owner01", "manager01", "staff01")}
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def login(self, username, password=PASSWORD):
        response = self.client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    # ---------------- 文档与门禁 ----------------

    def test_openapi_schema_and_docs(self):
        schema = self.client.get("/openapi.json")
        self.assertEqual(schema.status_code, 200)
        paths = schema.json()["paths"]
        for path in ("/api/auth/login", "/api/tasks/today", "/api/tasks/{task_id}/status",
                     "/api/data-quality", "/api/reports/daily", "/api/chat"):
            self.assertIn(path, paths)
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_data_endpoints_require_token(self):
        for path in ("/api/bootstrap", "/api/tasks/today", "/api/data-quality", "/api/reports/daily",
                     "/api/auth/me", "/api/metrics/summary"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get("/api/bootstrap", headers=self.headers("bogus")).status_code, 401)
        self.assertEqual(self.client.post("/api/chat", json={"session_id": "s", "message": "hi"}).status_code, 401)

    def test_login_me_logout(self):
        response = self.client.get("/api/auth/me", headers=self.headers(self.tokens["manager01"]))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["user"]["role_label"], "店长")
        self.assertIn("tasks:verify", body["permissions"])

        revoked = self.client.post("/api/auth/logout", headers=self.headers(self.tokens["manager01"]))
        self.assertEqual(revoked.json()["revoked"], True)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.headers(self.tokens["manager01"])).status_code, 401)

    def test_login_lockout_returns_423(self):
        for _ in range(5):
            self.client.post("/api/auth/login", json={"username": "staff01", "password": "wrong-password-1"})
        response = self.client.post("/api/auth/login", json={"username": "staff01", "password": PASSWORD})
        self.assertEqual(response.status_code, 423)
        self.assertIn("登录失败次数过多", response.json()["detail"])

    # ---------------- 权限与隔离 ----------------

    def test_user_id_query_cannot_escalate(self):
        staff = self.headers(self.tokens["staff01"])
        response = self.client.get("/api/bootstrap?user_id=manager-001", headers=staff)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "employee")
        self.assertEqual(self.client.get("/api/reports/daily?user_id=manager-001", headers=staff).status_code, 403)
        self.assertEqual(self.client.get("/api/data-quality?user_id=manager-001", headers=staff).status_code, 403)
        tasks = self.client.get("/api/tasks/today?status=open", headers=staff).json()["tasks"]
        self.assertEqual({task["assignee_id"] for task in tasks}, {"员工小李"})

    def test_financial_fields_stripped_for_employee(self):
        summary = self.client.get("/api/metrics/summary?days=7", headers=self.headers(self.tokens["staff01"])).json()
        metrics = summary["platforms"]["meituan"]["metrics"]
        for field in ("gmv", "refunds", "refund_rate"):
            self.assertNotIn(field, metrics)
        owner_summary = self.client.get("/api/metrics/summary?days=7", headers=self.headers(self.tokens["owner01"])).json()
        self.assertIn("gmv", owner_summary["platforms"]["meituan"]["metrics"])

    def test_store_isolation(self):
        manager = self.headers(self.tokens["manager01"])
        tasks = self.client.get("/api/tasks/today?status=open", headers=manager).json()["tasks"]
        self.assertNotIn("B-store-only", [task["task_id"] for task in tasks])
        self.assertEqual(self.client.get("/api/tasks/B-store-only", headers=manager).status_code, 403)

    # ---------------- 写流程 ----------------

    def test_write_flow_permissions_and_audit(self):
        staff, manager = self.headers(self.tokens["staff01"]), self.headers(self.tokens["manager01"])
        tasks = self.client.get("/api/tasks/today?status=open", headers=staff).json()["tasks"]
        invite = next(task for task in tasks if task["task_id"].endswith("review-invite"))

        done = self.client.post(f"/api/tasks/{invite['task_id']}/status",
                                json={"session_id": "s-staff", "status": "done"}, headers=staff)
        self.assertEqual(done.status_code, 409)                     # 无证据不能完成
        accepted = self.client.post(f"/api/tasks/{invite['task_id']}/status",
                                    json={"session_id": "s-staff", "status": "accepted"}, headers=staff)
        self.assertEqual((accepted.status_code, accepted.json()["status"]), (200, "accepted"))
        evidence = self.client.post(f"/api/tasks/{invite['task_id']}/evidence",
                                    json={"session_id": "s-staff", "evidence_type": "脱敏订单号",
                                          "object_ref": "MT-1"}, headers=staff)
        self.assertEqual(evidence.status_code, 200)
        self.assertEqual(evidence.json()["evidence"]["submitted_by"], "员工小李")
        finished = self.client.post(f"/api/tasks/{invite['task_id']}/status",
                                    json={"session_id": "s-staff", "status": "done"}, headers=staff)
        self.assertEqual(finished.json()["status"], "done")
        verify = self.client.post(f"/api/tasks/{invite['task_id']}/status",
                                  json={"session_id": "s-staff", "status": "verified"}, headers=staff)
        self.assertEqual(verify.status_code, 403)                   # 店员不能自行验收
        verify = self.client.post(f"/api/tasks/{invite['task_id']}/status",
                                  json={"session_id": "s-manager", "status": "verified"}, headers=manager)
        self.assertEqual(verify.json()["status"], "verified")
        self.assertEqual(self.client.post(f"/api/tasks/{invite['task_id']}/status",
                                          json={"session_id": "s", "status": "不是状态"}, headers=manager).status_code, 400)

        events = [row["event_type"] for row in self.auth.list_audit(store_id=STORE, limit=50)]
        self.assertIn("task_status_changed", events)
        self.assertIn("task_evidence_submitted", events)

    def test_chat_and_session_hijack(self):
        manager, staff = self.headers(self.tokens["manager01"]), self.headers(self.tokens["staff01"])
        first = self.client.post("/api/chat", json={"session_id": "shared", "message": "数据齐不齐？"}, headers=manager)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["intent"], "data_quality")
        hijack = self.client.post("/api/chat", json={"session_id": "shared", "message": "hi"}, headers=staff)
        self.assertEqual(hijack.status_code, 403)

    def test_daily_report_and_pages(self):
        response = self.client.get("/api/reports/daily", headers=self.headers(self.tokens["manager01"]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("经营日报", response.json()["report"])
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for marker in ('id="loginBox"', 'id="taskCards"', 'id="reportBtn"'):
            self.assertIn(marker, page.text)

    def test_demo_mode_is_explicit(self):
        root = Path(self.temp.name)
        app = create_app(config_path=ROOT / "store_config.json", metrics_db=root / "metrics.db",
                         store_db=root / "ops.db", chat_db=root / "chat.db", auth_mode="demo")
        with TestClient(app) as client:
            body = client.get("/api/bootstrap?user_id=manager-001").json()
            self.assertEqual(body["auth_mode"], "demo")
            self.assertEqual(body["user"]["role"], "manager")


if __name__ == "__main__":
    unittest.main()
