# -*- coding: utf-8 -*-
"""第 4 期：巡店模板/巡店任务/缺项复核与申诉闭环接口。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from governance import ApprovalStore
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
PASSWORD = "ShiZiZuo-2026-store"
ITEMS = [
    {"item_key": "front", "title": "前场卫生", "evidence_required": ["照片"]},
    {"item_key": "storage", "title": "后厨库存", "evidence_required": ["盘点表", "照片"]},
]


class InspectionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_db = root / "ops.db"
        self.auth = AuthStore(self.store_db, tenant_id=TENANT)
        for username, display, role in (("owner01", "店主老王", "owner"),
                                        ("manager01", "武汉狮子座店店长", "manager"),
                                        ("operator01", "运营小李", "operator"),
                                        ("staff01", "员工小李", "employee")):
            self.auth.create_user(username, PASSWORD, display, role, TENANT, STORE)
        self.tasks = TaskStore(self.store_db, tenant_id=TENANT)
        self.approvals = ApprovalStore(self.store_db, tenant_id=TENANT)
        self.client = TestClient(create_app(config_path=ROOT / "store_config.json",
                                            metrics_db=root / "metrics.db", store_db=self.store_db,
                                            chat_db=root / "chat.db", auth_mode="session"))
        self.tokens = {}
        for name in ("owner01", "manager01", "operator01", "staff01"):
            status, data = self.call("POST", "/api/auth/login", {"username": name, "password": PASSWORD})
            self.assertEqual(status, 200, data)
            self.tokens[name] = data["token"]
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def call(self, method, path, payload=None, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client.request(method, path, json=payload, headers=headers)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def create_template(self, username="manager01"):
        return self.call("POST", "/api/inspection-templates", {
            "template_key": "daily", "name": "日巡店", "items": ITEMS, "activate": True},
            token=self.tokens[username])

    def run_inspection(self, day="2026-10-22", username="manager01"):
        return self.call("POST", "/api/inspections",
                         {"template_key": "daily", "day": day, "assignee": "店长"},
                         token=self.tokens[username])

    def test_template_requires_manage_permission(self):
        status, _ = self.call("GET", "/api/inspection-templates")
        self.assertEqual(status, 401)
        status, _ = self.call("POST", "/api/inspection-templates",
                              {"template_key": "daily", "name": "日巡店", "items": ITEMS})
        self.assertEqual(status, 401)
        status, _ = self.call("POST", "/api/inspection-templates",
                              {"template_key": "daily", "name": "日巡店", "items": ITEMS},
                              token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, created = self.create_template()
        self.assertEqual(status, 200, created)
        self.assertEqual((created["version"], created["status"]), (1, "active"))
        self.assertEqual(len(created["items"]), 2)
        status, listed = self.call("GET", "/api/inspection-templates", token=self.tokens["staff01"])
        self.assertEqual(status, 200, listed)
        self.assertEqual(listed["count"], 1)

    def test_inspection_tasks_and_missing_items(self):
        self.create_template()
        status, run = self.run_inspection()
        self.assertEqual(status, 200, run)
        self.assertEqual(run["created"], 2)
        task_id = next(item for item in run["task_ids"] if item.endswith("storage"))
        status, review = self.call("GET", f"/api/inspections/{task_id}", token=self.tokens["operator01"])
        self.assertEqual(status, 200, review)
        self.assertEqual(review["task"]["category"], "inspection")
        self.assertEqual(review["missing"], ["盘点表", "照片"])
        self.assertTrue(review["requires_review"])
        self.tasks.add_evidence(task_id, "照片", "evidence://a.png", "staff01")
        status, review = self.call("GET", f"/api/inspections/{task_id}", token=self.tokens["manager01"])
        self.assertEqual(review["missing"], ["盘点表"])
        self.tasks.add_evidence(task_id, "盘点表", "evidence://b.csv", "staff01")
        status, review = self.call("GET", f"/api/inspections/{task_id}", token=self.tokens["manager01"])
        self.assertEqual(review["missing"], [])
        self.assertFalse(review["requires_review"])

    def test_unknown_inspection_task_is_404(self):
        status, _ = self.call("GET", "/api/inspections/no-such-task", token=self.tokens["manager01"])
        self.assertEqual(status, 404)

    def test_open_appeal_blocks_approval_until_decided(self):
        plan = {"date": "2026-10-22", "tasks": [
            {"task_id": "2026-10-22-escalation-OT1", "category": "management", "title": "复核逾期任务",
             "assignee": "区域负责人", "priority": "high", "due_at": "2026-10-22T19:00:00+08:00",
             "target": {"source_task_id": "OT1"}, "evidence_required": ["原任务要求"], "reason": "逾期",
             "requires_approval": True, "rule_key": "escalation", "rule_version": "1"}]}
        self.tasks.save_plan(plan, STORE)
        approval = self.approvals.request(STORE, "disciplinary:draft", {"note": "内部警告草稿"},
                                          requested_by="店长", task_id="2026-10-22-escalation-OT1")

        status, appeal = self.call("POST", "/api/appeals",
                                   {"task_id": "2026-10-22-escalation-OT1", "reason": "任务信息有误"},
                                   token=self.tokens["staff01"])
        self.assertEqual(status, 200, appeal)
        status, body = self.call("POST", "/api/appeals",
                                 {"task_id": "2026-10-22-escalation-OT1", "reason": "再提一次"},
                                 token=self.tokens["staff01"])
        self.assertEqual(status, 409, body)

        status, body = self.call("POST", f"/api/approvals/{approval['approval_id']}/decide",
                                 {"decision": "approved", "reason": "照批"}, token=self.tokens["manager01"])
        self.assertEqual(status, 409, body)
        self.assertIn("未结案申诉", str(body))

        status, decided = self.call("POST", f"/api/appeals/{appeal['appeal_id']}/decide",
                                    {"decision": "accepted", "note": "确有误"}, token=self.tokens["manager01"])
        self.assertEqual(status, 200, decided)
        status, result = self.call("POST", f"/api/approvals/{approval['approval_id']}/decide",
                                   {"decision": "approved", "reason": "申诉已结案"},
                                   token=self.tokens["manager01"])
        self.assertEqual(status, 200, result)
        self.assertEqual(result["approval"]["status"], "approved")

    def test_staff_cannot_decide_appeal(self):
        status, _ = self.call("POST", "/api/appeals/apl-x/decide", {"decision": "accepted"},
                              token=self.tokens["staff01"])
        self.assertEqual(status, 403)

    def test_dashboard_distinguishes_inspection_tasks(self):
        self.create_template()
        self.run_inspection()
        status, board = self.call("GET", "/api/dashboard", token=self.tokens["manager01"])
        self.assertEqual(status, 200, board)
        self.assertGreaterEqual(board["tasks"]["inspection_count"], 2)
        self.assertGreaterEqual(board["tasks"]["count_by_category"].get("inspection", 0), 2)


if __name__ == "__main__":
    unittest.main()