# -*- coding: utf-8 -*-
"""第 5 期：GET /api/capabilities 按身份过滤能力清单。"""
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


class CapabilitiesApiTests(unittest.TestCase):
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

    def names(self, username):
        status, body = self.call("GET", "/api/capabilities", token=self.tokens[username])
        self.assertEqual(status, 200, body)
        return body, {item["name"] for item in body["capabilities"]}

    def test_requires_auth(self):
        status, _ = self.call("GET", "/api/capabilities")
        self.assertEqual(status, 401)

    def test_capabilities_are_filtered_by_role(self):
        manager_body, manager_names = self.names("manager01")
        staff_body, staff_names = self.names("staff01")
        self.assertIn("get_metric_summary", manager_names)
        self.assertIn("get_data_status", manager_names)
        self.assertNotIn("build_daily_report", staff_names)
        self.assertLess(len(staff_names), len(manager_names))
        self.assertGreater(manager_body["count"], 0)
        self.assertGreater(staff_body["count"], 0)

    def test_payload_carries_id_permission_and_scope(self):
        owner_body, _ = self.names("owner01")
        first = owner_body["capabilities"][0]
        self.assertTrue(first["capability_id"].startswith("tool:"))
        self.assertTrue(first["permission"])
        self.assertIn("roles", first["scope"])
        self.assertTrue(first["scope"]["roles"])
        ids = [item["capability_id"] for item in owner_body["capabilities"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()