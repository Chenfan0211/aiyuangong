# -*- coding: utf-8 -*-
"""第 4 期：员工申诉闭环（一任务一次、驳回要结论、未结案阻断奖惩）。"""
import tempfile
import unittest
from pathlib import Path

from appeal import AppealError, AppealStore

STORE = "WH-LION-001"
TENANT = "LPL-001"


class AppealTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AppealStore(Path(self.temp.name) / "ops.db", tenant_id=TENANT)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, task_id="2026-10-22-escalation-OT1", reason="任务信息有误"):
        return self.store.submit(STORE, task_id, "staff01", reason)

    def test_submit_requires_reason(self):
        with self.assertRaises(AppealError):
            self.store.submit(STORE, "task-1", "staff01", "   ")

    def test_same_task_allows_only_one_open_appeal(self):
        appeal = self.submit()
        self.assertEqual(appeal["status"], "pending")
        with self.assertRaises(AppealError):
            self.submit(reason="再申诉一次")

    def test_reject_requires_note_and_clears_blocker(self):
        appeal = self.submit()
        blocker = self.store.blocker_for(STORE, appeal["task_id"])
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker["appeal_id"], appeal["appeal_id"])
        with self.assertRaises(AppealError):
            self.store.decide(appeal["appeal_id"], "rejected", "店长")
        decided = self.store.decide(appeal["appeal_id"], "rejected", "店长", note="任务属实")
        self.assertEqual(decided["status"], "rejected")
        self.assertIsNone(self.store.blocker_for(STORE, appeal["task_id"]))

    def test_accepted_appeal_also_closes_the_blocker(self):
        appeal = self.submit()
        decided = self.store.decide(appeal["appeal_id"], "accepted", "店长", note="确有误")
        self.assertEqual(decided["status"], "accepted")
        self.assertIsNone(self.store.blocker_for(STORE, appeal["task_id"]))
        with self.assertRaises(AppealError):
            self.store.decide(appeal["appeal_id"], "rejected", "店长", note="重复受理")


if __name__ == "__main__":
    unittest.main()