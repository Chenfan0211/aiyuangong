# -*- coding: utf-8 -*-
"""第 10 期：数字员工与团队接口（登记 → 建团队 → 派任务/挂池）。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
PASSWORD = "ShiZiZuo-2026-store"


class TeamApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_db = root / "ops.db"
        auth = AuthStore(self.store_db, tenant_id=TENANT)
        for username, display, role in (("owner01", "店主老王", "owner"),
                                        ("manager01", "武汉狮子座店店长", "manager"),
                                        ("staff01", "员工小李", "employee")):
            auth.create_user(username, PASSWORD, display, role, TENANT, STORE)
        self.client = TestClient(create_app(config_path=ROOT / "store_config.json",
                                            metrics_db=root / "metrics.db", store_db=self.store_db,
                                            chat_db=root / "chat.db", auth_mode="session"))
        self.tokens = {}
        for name in ("owner01", "manager01", "staff01"):
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

    def test_permissions(self):
        status, _ = self.call("GET", "/api/digital-employees")
        self.assertEqual(status, 401)
        status, _ = self.call("GET", "/api/digital-employees", token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/api/teams", {"team_key": "a", "name": "A"},
                              token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, body = self.call("GET", "/api/teams", token=self.tokens["manager01"])
        self.assertEqual(status, 200, body)

    def test_hire_team_and_assign_chain(self):
        status, employee = self.call("POST", "/api/digital-employees",
                                     {"employee_key": "ops-assistant", "name": "运营助理",
                                      "role_profile": "日常巡检",
                                      "capabilities": ["tool:get_data_status"]},
                                     token=self.tokens["manager01"])
        self.assertEqual(status, 200, employee)
        self.assertEqual(employee["capabilities"], ["tool:get_data_status"])

        status, body = self.call("POST", "/api/digital-employees",
                                 {"employee_key": "bad", "name": "越权员工",
                                  "capabilities": ["tool:not_exists"]},
                                 token=self.tokens["manager01"])
        self.assertEqual(status, 400, body)

        status, team = self.call("POST", "/api/teams",
                                 {"team_key": "morning", "name": "早班小组",
                                  "leader_employee_id": employee["employee_id"],
                                  "human_fallback": "店长"},
                                 token=self.tokens["manager01"])
        self.assertEqual(status, 200, team)
        self.assertEqual(team["leader_employee_id"], employee["employee_id"])
        self.assertEqual(team["human_fallback"], "店长")

        status, pool = self.call("POST", f"/api/teams/{team['team_id']}/tasks",
                                 {"task_id": "task-pool", "title": "整理货架"},
                                 token=self.tokens["manager01"])
        self.assertEqual(status, 200, pool)
        self.assertEqual(pool["status"], "offered")
        self.assertIsNone(pool["assignee_ref"])

        status, assigned = self.call("POST", f"/api/teams/{team['team_id']}/tasks",
                                     {"task_id": "task-assigned", "title": "复核库存",
                                      "assignee_ref": "staff01"},
                                     token=self.tokens["manager01"])
        self.assertEqual(status, 200, assigned)
        self.assertEqual(assigned["status"], "claimed")

        status, listed = self.call("GET", "/api/teams", token=self.tokens["owner01"])
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["teams"][0]["team_id"], team["team_id"])

    def test_assign_to_unknown_team_is_rejected(self):
        status, body = self.call("POST", "/api/teams/team-none/tasks",
                                 {"task_id": "t", "title": "x"}, token=self.tokens["manager01"])
        self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()