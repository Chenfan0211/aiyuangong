# -*- coding: utf-8 -*-
"""第 9 期：记忆与进化提案接口（沉淀 → 蒸馏 → 人工确认）。"""
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
TEXT = "我喜欢早上七点开门。我是武汉狮子座店的店长。门店有两台收银机。"


class MemoryApiTests(unittest.TestCase):
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
        status, _ = self.call("GET", "/api/memory")
        self.assertEqual(status, 401)
        status, body = self.call("GET", "/api/memory", token=self.tokens["staff01"])
        self.assertEqual(status, 200, body)                 # 员工可读
        status, _ = self.call("POST", "/api/memory", {"text": TEXT}, token=self.tokens["staff01"])
        self.assertEqual(status, 403)                       # 员工不可写

    def test_absorb_and_recall(self):
        status, body = self.call("POST", "/api/memory", {"text": TEXT}, token=self.tokens["manager01"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["count"], 3)
        status, recalled = self.call("GET", "/api/memory", token=self.tokens["manager01"])
        self.assertEqual(recalled["count"], 3)
        status, facts = self.call("GET", "/api/memory?kind=fact", token=self.tokens["manager01"])
        self.assertEqual(facts["count"], 1)

    def test_preference_distills_proposal_and_decision(self):
        first, _ = self.call("POST", "/api/memory", {"text": "我喜欢早上七点开门"},
                             token=self.tokens["manager01"])
        self.assertEqual(first, 200)
        status, second = self.call("POST", "/api/memory", {"text": "我喜欢早上七点开门"},
                                   token=self.tokens["manager01"])
        self.assertEqual(status, 200, second)
        self.assertGreaterEqual(second["proposals_created"], 1)

        status, listed = self.call("GET", "/api/evolution/proposals?status=pending",
                                   token=self.tokens["manager01"])
        self.assertEqual(status, 200, listed)
        self.assertEqual(listed["count"], 1)
        proposal_id = listed["proposals"][0]["proposal_id"]
        self.assertEqual(listed["proposals"][0]["proposal_type"], "sop_step")

        status, decided = self.call("POST", f"/api/evolution/proposals/{proposal_id}/decide",
                                    {"decision": "approved", "note": "同意固化为 SOP 步骤"},
                                    token=self.tokens["manager01"])
        self.assertEqual(status, 200, decided)
        self.assertEqual(decided["status"], "approved")
        status, body = self.call("POST", f"/api/evolution/proposals/{proposal_id}/decide",
                                 {"decision": "rejected"}, token=self.tokens["manager01"])
        self.assertEqual(status, 409, body)

    def test_evolution_requires_read_permission(self):
        status, _ = self.call("GET", "/api/evolution/proposals", token=self.tokens["staff01"])
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()