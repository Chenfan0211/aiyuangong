# -*- coding: utf-8 -*-
"""第 3 期第 4 步：规则库接口（列规则 / 建草稿 / 新版本 / 审批）与审计留痕。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from governance import AuditCenter
from rule_seed import BUILTIN_RULE_KEYS, seed_builtin_rules
from rule_store import RuleStore
from snapshot_builder import load_config

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
PASSWORD = "ShiZiZuo-2026-store"
SCOPE = {"store_id": STORE}
CONDITION = {"min_views": 50, "conversion_floor": 0.05, "margin_floor": 0.25}
ACTION = {"category": "package", "priority": "high", "due_at": "T17:00:00+08:00",
          "requires_approval": True, "evidence": ["页面快照"]}


class RulesApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = load_config(ROOT / "store_config.json")
        self.metrics_db, self.store_db, self.chat_db = root / "metrics.db", root / "ops.db", root / "chat.db"
        self.auth = AuthStore(self.store_db, tenant_id=TENANT)
        for username, display, role in (("owner01", "店主老王", "owner"),
                                        ("manager01", "武汉狮子座店店长", "manager"),
                                        ("operator01", "运营小李", "operator"),
                                        ("staff01", "员工小李", "employee")):
            self.auth.create_user(username, PASSWORD, display, role, TENANT, STORE)
        self.rules = RuleStore(self.store_db, tenant_id=TENANT)
        seed_builtin_rules(self.rules, STORE, self.config)
        self.center = AuditCenter(self.store_db, tenant_id=TENANT)
        self.client = TestClient(create_app(config_path=ROOT / "store_config.json",
                                            metrics_db=self.metrics_db, store_db=self.store_db,
                                            chat_db=self.chat_db, auth_mode="session"))
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

    def new_rule(self, key="custom_test", username="manager01"):
        # 这里传用户名而不是凭据字面量，避免安全扫描把它误判成硬编码密钥
        return self.call("POST", "/api/rules", {
            "rule_key": key, "scope": SCOPE, "condition": CONDITION, "action_template": ACTION},
            token=self.tokens[username])

    def test_list_requires_permission_and_returns_seeded_rules(self):
        status, _ = self.call("GET", "/api/rules")
        self.assertEqual(status, 401)
        status, _ = self.call("GET", "/api/rules", token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, body = self.call("GET", "/api/rules", token=self.tokens["manager01"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["count"], len(BUILTIN_RULE_KEYS))
        self.assertEqual({item["rule_key"] for item in body["rules"]}, set(BUILTIN_RULE_KEYS))
        self.assertTrue(all(item["status"] == "draft" for item in body["rules"]))

    def test_create_rule_and_new_version_are_drafts(self):
        status, created = self.new_rule()
        self.assertEqual(status, 200, created)
        self.assertEqual((created["rule_key"], created["version"], created["status"]),
                         ("custom_test", 1, "draft"))
        status, second = self.call("POST", "/api/rules/custom_test/versions",
                                   {"condition": dict(CONDITION, conversion_floor=0.08),
                                    "action_template": ACTION}, token=self.tokens["manager01"])
        self.assertEqual(status, 200, second)
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["condition_json"]["conversion_floor"], 0.08)
        self.assertEqual(second["status"], "draft")

    def test_duplicate_rule_key_and_unknown_key_are_rejected(self):
        status, body = self.new_rule(key="package")
        self.assertEqual(status, 409, body)
        status, body = self.call("POST", "/api/rules/no_such_rule/versions",
                                 {"condition": CONDITION, "action_template": ACTION},
                                 token=self.tokens["manager01"])
        self.assertEqual(status, 404, body)

    def test_operator_cannot_write_rules(self):
        status, _ = self.new_rule(key="operator_rule", username="operator01")
        self.assertEqual(status, 403)

    def test_decide_requires_privilege_and_writes_audit(self):
        status, _ = self.call("POST", "/api/rules/package/decide",
                              {"version": 1, "status": "approved"}, token=self.tokens["manager01"])
        self.assertEqual(status, 403)
        status, result = self.call("POST", "/api/rules/package/decide",
                                   {"version": 1, "status": "approved", "note": "初始版本"},
                                   token=self.tokens["owner01"])
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["approved_by"], "店主老王")
        events = [item["event_type"] for item in self.center.list_events(STORE, limit=20)]
        self.assertIn("rule_decided", events)

    def test_decide_rejects_unknown_version_and_draft_rollback(self):
        status, _ = self.call("POST", "/api/rules/package/decide",
                              {"version": 99, "status": "approved"}, token=self.tokens["owner01"])
        self.assertEqual(status, 404)
        status, body = self.call("POST", "/api/rules/package/decide",
                                 {"version": 1, "status": "draft"}, token=self.tokens["owner01"])
        self.assertEqual(status, 400, body)

    def test_approving_new_version_retires_previous(self):
        self.call("POST", "/api/rules/package/decide",
                  {"version": 1, "status": "approved"}, token=self.tokens["owner01"])
        self.call("POST", "/api/rules/package/versions",
                  {"condition": CONDITION, "action_template": ACTION}, token=self.tokens["manager01"])
        status, second = self.call("POST", "/api/rules/package/decide",
                                   {"version": 2, "status": "approved"}, token=self.tokens["owner01"])
        self.assertEqual(status, 200, second)
        versions = {item["version"]: item["status"] for item in self.rules.list_rules("package")}
        self.assertEqual(versions[1], "retired")
        self.assertEqual(versions[2], "approved")

    def test_outcomes_endpoint_permissions_and_empty_payload(self):
        status, _ = self.call("GET", "/api/rules/package/outcomes")
        self.assertEqual(status, 401)
        status, _ = self.call("GET", "/api/rules/package/outcomes", token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, body = self.call("GET", "/api/rules/package/outcomes", token=self.tokens["manager01"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["rule_key"], "package")
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["outcomes"], [])
        status, body = self.call("GET", "/api/rules/package/outcomes?window_days=7",
                                 token=self.tokens["owner01"])
        self.assertEqual(status, 200, body)


if __name__ == "__main__":
    unittest.main()