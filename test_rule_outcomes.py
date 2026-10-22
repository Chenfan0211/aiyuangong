# -*- coding: utf-8 -*-
"""第 3 期第 6 步：规则效果归因（rule_outcomes）与"调参提案"。"""
import tempfile
import unittest
from pathlib import Path

from outcome_tracker import OutcomeTracker
from rule_outcomes import MIN_SAMPLES, RuleOutcomeStore
from rule_store import RuleStore
from task_store import TaskStore

STORE = "WH-LION-001"
TENANT = "LPL-001"


class RuleOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "ops.db"
        self.tasks = TaskStore(self.db, tenant_id=TENANT)
        self.outcomes = OutcomeTracker(self.db, tenant_id=TENANT)
        self.store = RuleOutcomeStore(self.db, tenant_id=TENANT)

    def tearDown(self):
        self.temp.cleanup()

    def seed_tasks(self, count=3, rule_key="package", rule_version="1", offset=0):
        tasks = [
            {"task_id": f"2026-10-01-{rule_key}-{offset + i}", "category": "package",
             "title": "复核套餐", "assignee": "运营", "priority": "high",
             "due_at": "2026-10-01T17:00:00+08:00", "target": {"package_id": f"P{offset + i}"},
             "evidence_required": [], "reason": "测试", "requires_approval": True,
             "rule_key": rule_key, "rule_version": rule_version}
            for i in range(count)
        ]
        self.tasks.save_plan({"date": "2026-10-01", "tasks": tasks}, STORE)
        return [item["task_id"] for item in tasks]

    def compute(self, **kwargs):
        return self.store.compute(STORE, task_store=self.tasks,
                                  outcome_tracker=self.outcomes, **kwargs)

    def test_insufficient_samples_produce_no_threshold_advice(self):
        self.seed_tasks(count=MIN_SAMPLES - 1)
        record = self.compute()["computed"][0]
        self.assertEqual(record["verdict"], "insufficient_data")
        self.assertIn("reason", record["suggestion"])
        self.assertNotIn("suggested", record["suggestion"])
        self.assertIsNone(record["avg_delta"])

    def test_many_rejections_suggest_raising_threshold_with_numbers(self):
        task_ids = self.seed_tasks(count=4)
        for task_id in task_ids[:3]:
            self.outcomes.record_feedback(STORE, task_id, "manager01", "rejected", reason="规则过松")
        rules = {"package": {"condition": {"conversion_floor": 0.05}}}
        record = self.compute(rules=rules)["computed"][0]
        self.assertEqual(record["verdict"], "raise_threshold")
        self.assertEqual(record["rejected_count"], 3)
        self.assertEqual(record["suggestion"]["field"], "conversion_floor")
        self.assertEqual(record["suggestion"]["current"], 0.05)
        self.assertEqual(record["suggestion"]["suggested"], 0.06)
        self.assertTrue(record["suggestion"]["requires_approval"])

    def test_acceptance_keeps_rule(self):
        task_ids = self.seed_tasks(count=MIN_SAMPLES)
        for task_id in task_ids:
            self.outcomes.record_feedback(STORE, task_id, "manager01", "accepted")
        record = self.compute()["computed"][0]
        self.assertEqual(record["verdict"], "keep")
        self.assertFalse(record["suggestion"]["requires_approval"])

    def test_versions_are_grouped_separately(self):
        self.seed_tasks(count=MIN_SAMPLES, rule_version="1")
        self.seed_tasks(count=MIN_SAMPLES, rule_version="2", offset=100)
        records = self.compute()["computed"]
        self.assertEqual(sorted(item["rule_version"] for item in records), ["1", "2"])

    def test_compute_is_idempotent_and_never_touches_rules(self):
        rules_store = RuleStore(self.root / "rules.db", tenant_id=TENANT)
        draft = rules_store.save_draft("package", scope={"store_id": STORE},
                                       condition={"conversion_floor": 0.05},
                                       action_template={"category": "package"})
        rules_store.decide("package", draft["version"], "approved")
        before = rules_store.get_rule("package")
        self.seed_tasks(count=MIN_SAMPLES)
        self.compute(rules={"package": rules_store.get_rule("package")})
        self.compute(rules={"package": rules_store.get_rule("package")})
        self.assertEqual(len(self.store.list(STORE)), 1)
        after = rules_store.get_rule("package")
        self.assertEqual(before["condition_json"], after["condition_json"])
        self.assertEqual(before["status"], after["status"])

    def test_tasks_without_rule_source_are_skipped(self):
        plan = {"date": "2026-10-01", "tasks": [
            {"task_id": "2026-10-01-data-repair", "category": "management", "title": "补齐数据",
             "assignee": "店长", "priority": "high", "due_at": "2026-10-01T12:00:00+08:00",
             "target": {}, "evidence_required": [], "reason": "缺数", "requires_approval": False}]}
        self.tasks.save_plan(plan, STORE)
        self.assertEqual(self.compute()["rule_count"], 0)


if __name__ == "__main__":
    unittest.main()