# -*- coding: utf-8 -*-
"""第 3 期第 2 步：内置规则种子（导出为数据，不改运行时行为）。"""
import tempfile
import unittest
from pathlib import Path

from rule_seed import (BUILTIN_RULE_KEYS, builtin_definitions, resolve_runtime_rules,
                       seed_builtin_rules)
from rule_store import RuleStore
from snapshot_builder import load_config

ROOT = Path(__file__).resolve().parent
CONFIG = load_config(ROOT / "store_config.json")
STORE_ID = "WH-LION-001"


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RuleStore(Path(self.temp.name) / "rules.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_seven_rules_seeded_as_drafts(self):
        result = seed_builtin_rules(self.store, STORE_ID, CONFIG)
        self.assertEqual(result["total"], 7)
        self.assertEqual(set(result["created"]), set(BUILTIN_RULE_KEYS))
        records = self.store.list_rules()
        self.assertEqual({item["rule_key"] for item in records}, set(BUILTIN_RULE_KEYS))
        self.assertTrue(all(item["status"] == "draft" for item in records))
        self.assertTrue(all(item["version"] == 1 for item in records))

    def test_thresholds_come_from_store_config(self):
        seed_builtin_rules(self.store, STORE_ID, CONFIG)
        rules = CONFIG["rules"]
        self.assertEqual(self.store.get_rule("negative_review")["condition_json"]["sla_hours"],
                         int(rules["negative_review_sla_hours"]))
        package = self.store.get_rule("package")["condition_json"]
        self.assertEqual(package["min_views"], int(rules["package_min_views"]))
        self.assertEqual(package["conversion_floor"], float(rules["package_conversion_floor"]))
        self.assertEqual(package["margin_floor"], float(rules["package_margin_floor"]))
        self.assertEqual(self.store.get_rule("low_slot")["condition_json"]["slot_min_days"],
                         int(rules["slot_min_days"]))
        self.assertEqual(
            self.store.get_rule("competitor")["condition_json"]["area_shift_percent"],
            float(rules["competitor_area_shift_percent"]))

    def test_only_high_risk_rules_require_approval(self):
        seed_builtin_rules(self.store, STORE_ID, CONFIG)
        for key in ("package", "escalation", "low_slot", "inventory", "competitor"):
            with self.subTest(rule=key):
                self.assertTrue(self.store.get_rule(key)["action_template_json"]["requires_approval"])
        self.assertFalse(self.store.get_rule("review_invite")["action_template_json"]["requires_approval"])
        # 差评是否赔付由运行时决定，规则本身不预设"必审批"
        self.assertFalse(self.store.get_rule("negative_review")["action_template_json"]["requires_approval"])

    def test_seeding_is_idempotent(self):
        seed_builtin_rules(self.store, STORE_ID, CONFIG)
        again = seed_builtin_rules(self.store, STORE_ID, CONFIG)
        self.assertEqual(again["created"], [])
        self.assertEqual(set(again["skipped"]), set(BUILTIN_RULE_KEYS))
        self.assertEqual(len(self.store.list_rules()), 7)      # 不会产生第二版

    def test_approved_rule_is_visible_after_seeding(self):
        seed_builtin_rules(self.store, STORE_ID, CONFIG)
        self.assertEqual(self.store.approved_rules(), [])
        self.store.decide("package", 1, "approved", actor_id="manager01")
        approved = self.store.approved_rules()
        self.assertEqual([item["rule_key"] for item in approved], ["package"])
        self.assertEqual(approved[0]["approved_by"], "manager01")

    def test_definitions_are_pure_data(self):
        """种子只产出数据（scope/condition/action_template），不含任何可执行内容。"""
        for item in builtin_definitions(STORE_ID, CONFIG):
            with self.subTest(rule=item["rule_key"]):
                self.assertEqual(set(item), {"rule_key", "scope", "condition", "action_template"})
                self.assertEqual(item["scope"]["store_id"], STORE_ID)
                self.assertIsInstance(item["condition"], dict)
                self.assertIsInstance(item["action_template"], dict)


    def test_seed_can_approve_builtin_rules(self):
        """生产接线需要"内置规则即生效"这一路径，但必须显式选择。"""
        result = seed_builtin_rules(self.store, STORE_ID, CONFIG, approve=True)
        self.assertEqual(len(result["approved"]), len(BUILTIN_RULE_KEYS))
        records = self.store.list_rules()
        self.assertTrue(all(item["status"] == "approved" for item in records))

    def test_resolve_runtime_rules_requires_all_seven_approved(self):
        seed_builtin_rules(self.store, STORE_ID, CONFIG)
        rules, missing = resolve_runtime_rules(self.store)
        self.assertIsNone(rules)
        self.assertEqual(set(missing), set(BUILTIN_RULE_KEYS))
        for key in BUILTIN_RULE_KEYS:
            version = self.store.get_rule(key)["version"]
            self.store.decide(key, version, "approved")
        rules, missing = resolve_runtime_rules(self.store)
        self.assertEqual(set(rules), set(BUILTIN_RULE_KEYS))
        self.assertEqual(missing, [])


    def test_approve_upgrades_existing_drafts(self):
        """先落草稿、后补审批也要能生效（运维上分两步做很常见）。"""
        seed_builtin_rules(self.store, STORE_ID, CONFIG)
        result = seed_builtin_rules(self.store, STORE_ID, CONFIG, approve=True)
        self.assertEqual(len(result["approved"]), len(BUILTIN_RULE_KEYS))
        self.assertTrue(all(item["status"] == "approved" for item in self.store.list_rules()))


if __name__ == "__main__":
    unittest.main()