# -*- coding: utf-8 -*-
"""第 3 期第 1 步：规则库底座（action_rules 转正 + 版本与状态机）。"""
import tempfile
import unittest
from pathlib import Path

from rule_store import RULE_STATUSES, RuleStore, RuleStoreError

KEY = "negative_review_sla"
CONDITION = {"sla_hours": 4}
ACTION = {"category": "reputation", "requires_approval": True}
SCOPE = {"store_id": "WH-LION-001"}


class RuleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RuleStore(Path(self.temp.name) / "rules.db")

    def tearDown(self):
        self.temp.cleanup()

    def draft(self, key=KEY, sla=4):
        return self.store.save_draft(
            key, scope=SCOPE, condition={"sla_hours": sla}, action_template=ACTION)

    def test_draft_starts_at_v1_and_increments(self):
        first = self.draft()
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["status"], "draft")
        second = self.draft(sla=6)
        self.assertEqual(second["version"], 2)
        self.assertEqual(len(self.store.list_rules(KEY)), 2)

    def test_json_columns_round_trip(self):
        record = self.draft()
        self.assertEqual(record["condition_json"], CONDITION)
        self.assertEqual(record["action_template_json"], ACTION)
        self.assertEqual(record["scope"], SCOPE)

    def test_approve_records_actor_and_time(self):
        self.draft()
        approved = self.store.decide(KEY, 1, "approved", actor_id="manager01")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approved_by"], "manager01")
        self.assertTrue(approved["approved_at"])
        self.assertEqual([item["version"] for item in self.store.approved_rules()], [1])

    def test_only_one_approved_version_per_key(self):
        """第 3 期硬约定：每个 rule_key 任何时刻只有一个 approved 版本。"""
        self.draft(sla=4)
        self.draft(sla=6)
        self.store.decide(KEY, 1, "approved", actor_id="m1")
        self.store.decide(KEY, 2, "approved", actor_id="m1")
        approved = self.store.approved_rules()
        self.assertEqual([item["version"] for item in approved], [2])
        self.assertEqual(self.store.get_rule(KEY, 1)["status"], "retired")
        self.assertEqual(self.store.get_rule(KEY, 2)["status"], "approved")

    def test_get_rule_prefers_approved_over_newer_draft(self):
        self.draft(sla=4)
        self.store.decide(KEY, 1, "approved", actor_id="m1")
        self.draft(sla=9)                                   # v2 还是草案
        self.assertEqual(self.store.get_rule(KEY)["version"], 1)
        self.assertEqual(self.store.get_rule(KEY)["status"], "approved")

    def test_paused_and_retired_are_not_in_approved_set(self):
        self.draft()
        self.store.decide(KEY, 1, "approved", actor_id="m1")
        self.store.decide(KEY, 1, "paused", actor_id="m1")
        self.assertEqual(self.store.approved_rules(), [])
        self.assertEqual(self.store.get_rule(KEY, 1)["status"], "paused")

    def test_cannot_roll_back_to_draft(self):
        self.draft()
        with self.assertRaises(RuleStoreError):
            self.store.decide(KEY, 1, "draft")

    def test_unknown_status_and_version_are_rejected(self):
        with self.assertRaises(RuleStoreError):
            self.store.decide(KEY, 1, "whatever")
        with self.assertRaises(RuleStoreError):
            self.store.decide(KEY, 9, "approved")
        with self.assertRaises(RuleStoreError):
            self.store.save_draft("  ", scope={}, condition={}, action_template={})

    def test_status_enum_is_the_documented_one(self):
        self.assertEqual(RULE_STATUSES, ("draft", "approved", "paused", "retired"))

    def test_rules_are_isolated_per_rule_key(self):
        self.draft(KEY)
        self.draft("review_invite")
        self.assertEqual(len(self.store.approved_rules()), 0)
        self.assertEqual({item["rule_key"] for item in self.store.list_rules()},
                         {KEY, "review_invite"})


if __name__ == "__main__":
    unittest.main()