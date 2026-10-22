# -*- coding: utf-8 -*-
"""第 4 期：巡店模板、巡店任务与缺项复核。"""
import tempfile
import unittest
from pathlib import Path

from inspection import INSPECTION_CATEGORY, InspectionError, InspectionStore
from task_store import TaskStore

STORE = "WH-LION-001"
TENANT = "LPL-001"


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ops.db"
        self.store = InspectionStore(self.db, tenant_id=TENANT)
        self.tasks = TaskStore(self.db, tenant_id=TENANT)

    def tearDown(self):
        self.temp.cleanup()

    def items(self):
        return [
            {"item_key": "front", "title": "前场卫生", "evidence_required": ["照片"]},
            {"item_key": "storage", "title": "后厨库存", "evidence_required": ["盘点表", "照片"]},
        ]

    def active_template(self, key="daily"):
        created = self.store.save_template(STORE, key, "日巡店", self.items(), created_by="manager01")
        return self.store.activate(created["template_id"], "manager01")

    def test_template_versions_and_single_active(self):
        first = self.store.save_template(STORE, "daily", "日巡店", self.items(), created_by="manager01")
        self.assertEqual((first["version"], first["status"]), (1, "draft"))
        self.store.activate(first["template_id"], "manager01")
        second = self.store.save_template(STORE, "daily", "日巡店", self.items(), created_by="manager01")
        self.assertEqual(second["version"], 2)
        active = self.store.activate(second["template_id"], "manager01")
        self.assertEqual(active["status"], "active")
        versions = {item["version"]: item["status"] for item in self.store.list_templates(STORE, "daily")}
        self.assertEqual(versions[1], "retired")
        self.assertEqual(versions[2], "active")

    def test_fixed_fields_only(self):
        template = self.store.save_template(STORE, "daily", "日巡店", [
            {"item_key": "front", "title": "前场卫生", "evidence_required": ["照片"], "自由字段": "忽略"}])
        self.assertEqual(set(template["items"][0]),
                         {"item_key", "title", "evidence_required", "required", "assignee_role", "threshold"})

    def test_empty_items_are_rejected(self):
        with self.assertRaises(InspectionError):
            self.store.save_template(STORE, "daily", "日巡店", [])

    def test_generate_tasks_and_missing_items_drive_review(self):
        template = self.active_template()
        tasks = self.store.generate_tasks(STORE, template["template_id"], "2026-10-22")
        self.assertEqual([task["category"] for task in tasks], [INSPECTION_CATEGORY, INSPECTION_CATEGORY])
        self.tasks.save_plan({"date": "2026-10-22", "tasks": tasks}, STORE)
        task_id = tasks[1]["task_id"]
        self.tasks.add_evidence(task_id, "照片", "evidence://a.png", "staff01")
        review = self.store.review(self.tasks, task_id)
        self.assertEqual(review["missing"], ["盘点表"])
        self.assertTrue(review["requires_review"])
        self.tasks.add_evidence(task_id, "盘点表", "evidence://b.csv", "staff01")
        review = self.store.review(self.tasks, task_id)
        self.assertEqual(review["missing"], [])
        self.assertFalse(review["requires_review"])


if __name__ == "__main__":
    unittest.main()