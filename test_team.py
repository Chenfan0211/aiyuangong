# -*- coding: utf-8 -*-
"""第 10 期：数字员工档案、团队（数字员工领导 + 人工兜底）与任务指派/认领。"""
import tempfile
import unittest
from pathlib import Path

from capabilities import build_registry
from digital_employee import DigitalEmployeeError, DigitalEmployeeStore
from team import TeamError, TeamStore

STORE = "WH-LION-001"
OTHER = "WH-LION-002"
TENANT = "LPL-001"


class DigitalEmployeeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ops.db"
        self.registry = build_registry(tenant_id=TENANT, store_id=STORE)
        self.store = DigitalEmployeeStore(self.db, tenant_id=TENANT,
                                          capability_registry=self.registry)

    def tearDown(self):
        self.temp.cleanup()

    def test_hire_with_registry_capabilities_and_idempotent(self):
        employee = self.store.hire(STORE, "ops-assistant", "运营助理",
                                   role_profile="日常巡检与数据核对",
                                   capabilities=["tool:get_data_status", "tool:get_tasks"],
                                   created_by="manager01")
        self.assertEqual(employee["status"], "active")
        self.assertEqual(employee["capabilities"],
                         ["tool:get_data_status", "tool:get_tasks"])
        again = self.store.hire(STORE, "ops-assistant", "运营助理（重复）")
        self.assertEqual(again["employee_id"], employee["employee_id"])
        self.assertEqual(len(self.store.list(STORE)), 1)

    def test_unknown_capability_is_rejected(self):
        with self.assertRaises(DigitalEmployeeError):
            self.store.hire(STORE, "bad", "越权员工", capabilities=["tool:not_exists"])

    def test_store_isolation_and_status_change(self):
        employee = self.store.hire(STORE, "a", "A")
        self.store.hire(OTHER, "b", "B")
        self.assertEqual(len(self.store.list(STORE)), 1)
        self.assertEqual(len(self.store.list(OTHER)), 1)
        paused = self.store.set_status(employee["employee_id"], "paused")
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(len(self.store.list(STORE, status="paused")), 1)


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ops.db"
        self.teams = TeamStore(self.db, tenant_id=TENANT)

    def tearDown(self):
        self.temp.cleanup()

    def team(self, key="morning", name="早班小组", **kwargs):
        return self.teams.create_team(STORE, key, name,
                                      leader_employee_id="de-ops-assistant",
                                      human_fallback="店长", **kwargs)

    def test_team_leader_is_digital_with_human_fallback(self):
        team = self.team()
        self.assertEqual(team["leader_employee_id"], "de-ops-assistant")
        self.assertEqual(team["human_fallback"], "店长")
        self.assertEqual(team["status"], "active")
        again = self.team(name="重复")
        self.assertEqual(again["team_id"], team["team_id"])

    def test_members_accept_both_types(self):
        team = self.team()
        team = self.teams.add_member(team["team_id"], "digital_employee", "de-ops-assistant",
                                     member_role="leader")
        team = self.teams.add_member(team["team_id"], "human", "staff01")
        self.assertEqual(len(team["members"]), 2)
        with self.assertRaises(TeamError):
            self.teams.add_member(team["team_id"], "robot", "x")

    def test_assign_claim_complete_flow(self):
        team = self.team()
        assigned = self.teams.assign(team["team_id"], "task-1", "复核库存", assignee_ref="staff01",
                                     assigned_by="店长")
        self.assertEqual(assigned["status"], "claimed")
        pool = self.teams.assign(team["team_id"], "task-2", "整理货架")
        self.assertEqual(pool["status"], "offered")
        self.assertIsNone(pool["assignee_ref"])
        claimed = self.teams.claim(pool["team_task_id"], "staff02")
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["assignee_ref"], "staff02")
        done = self.teams.complete(claimed["team_task_id"], actor="staff02")
        self.assertEqual(done["status"], "completed")

    def test_illegal_transitions_and_double_claim_are_rejected(self):
        team = self.team()
        pool = self.teams.assign(team["team_id"], "task-3", "盘点")
        claimed = self.teams.claim(pool["team_task_id"], "staff01")
        with self.assertRaises(TeamError):
            self.teams.claim(pool["team_task_id"], "staff02")
        self.teams.complete(claimed["team_task_id"], actor="staff01")
        with self.assertRaises(TeamError):
            self.teams.complete(claimed["team_task_id"], actor="staff01")
        with self.assertRaises(TeamError):
            self.teams.claim(claimed["team_task_id"], "staff02")

    def test_cancel_from_offered(self):
        team = self.team()
        pool = self.teams.assign(team["team_id"], "task-4", "临时任务")
        canceled = self.teams.cancel(pool["team_task_id"], reason="活动取消")
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(len(self.teams.list_tasks(team["team_id"], status="canceled")), 1)


if __name__ == "__main__":
    unittest.main()