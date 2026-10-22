# -*- coding: utf-8 -*-
"""第 7 期：SOP 状态机、断点续跑、版本、超时与 task/sop_run 边界。"""
import tempfile
import unittest
from pathlib import Path

from governance import ApprovalStore
from sop import SopError, SopStore
from sop_runtime import SopRuntime, SopRuntimeError
from task_store import TaskStore

STORE = "WH-LION-001"
TENANT = "LPL-001"


class SopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ops.db"
        self.tasks = TaskStore(self.db, tenant_id=TENANT)
        self.approvals = ApprovalStore(self.db, tenant_id=TENANT)
        self.runtime = SopRuntime(self.db, tenant_id=TENANT, task_store=self.tasks,
                                 approval_store=self.approvals)
        self.store = self.runtime.sops

    def tearDown(self):
        self.temp.cleanup()

    def graph(self, *, human_timeout=None, timeout_exit=False):
        nodes = [
            {"node_id": "inspect", "node_type": "human", "title": "提交巡检结果",
             "evidence_required": ["巡检照片"]},
            {"node_id": "approve", "node_type": "approval", "title": "店长审批"},
            {"node_id": "done", "node_type": "end"},
        ]
        edges = [
            {"from_node": "inspect", "to_node": "approve"},
            {"from_node": "approve", "to_node": "done"},
        ]
        if human_timeout is not None:
            nodes[0]["timeout_minutes"] = human_timeout
        if timeout_exit:
            nodes.insert(2, {"node_id": "escalate", "node_type": "agent", "title": "超时升级"})
            edges = [
                {"from_node": "inspect", "to_node": "approve"},
                {"from_node": "inspect", "to_node": "escalate", "when": "timeout"},
                {"from_node": "approve", "to_node": "done"},
                {"from_node": "escalate", "to_node": "done"},
            ]
        return nodes, edges

    def build(self, *, sop_key="daily-inspection", max_hours=24, human_timeout=None,
              timeout_exit=False):
        nodes, edges = self.graph(human_timeout=human_timeout, timeout_exit=timeout_exit)
        sop = self.store.save_sop(STORE, sop_key, "日巡店", nodes, edges,
                                  start_node="inspect", created_by="manager01", max_hours=max_hours)
        return self.store.activate(sop["sop_id"], "manager01")

    # ---------------- 验收 1：全链路 ----------------

    def test_daily_inspection_end_to_end(self):
        sop = self.build()
        run = self.runtime.start(sop["sop_id"], store_id=STORE, context={"date": "2026-10-22"})
        self.assertEqual(run["status"], "waiting_human")
        human_step = next(step for step in run["steps"] if step["node_id"] == "inspect")
        self.assertTrue(human_step["task_id"])
        task = self.tasks.get_task(human_step["task_id"])
        self.assertEqual(task["category"], "sop")

        run = self.runtime.submit_human(run["run_id"], payload={"checked_items": 6}, actor="staff01")
        self.assertEqual(run["status"], "waiting_approval")
        approval_step = next(step for step in run["steps"] if step["node_id"] == "approve")
        self.assertTrue(approval_step["approval_id"])
        self.assertEqual(run["context"]["checked_items"], 6)     # 断点快照

        run = self.runtime.resume(run["run_id"], decision="approved", actor="manager01")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["context"]["checked_items"], 6)     # 恢复后上下文不丢
        self.assertEqual(sum(1 for step in run["steps"] if step["node_id"] == "done"), 1)

    def test_rejected_approval_fails_the_run(self):
        sop = self.build()
        run = self.runtime.start(sop["sop_id"], store_id=STORE)
        run = self.runtime.submit_human(run["run_id"], payload={}, actor="staff01")
        run = self.runtime.resume(run["run_id"], decision="rejected", actor="manager01")
        self.assertEqual(run["status"], "failed")
        self.assertIn("驳回", run["error"])

    # ---------------- 验收 2：非法转换被拒 ----------------

    def test_illegal_transitions_are_rejected(self):
        sop = self.build()
        run = self.runtime.start(sop["sop_id"], store_id=STORE)
        with self.assertRaises(SopRuntimeError):
            self.runtime.transition(run["run_id"], "completed")
        with self.assertRaises(SopRuntimeError):
            self.runtime.transition(run["run_id"], "not_a_status")
        self.runtime.cancel(run["run_id"], reason="手动取消")
        with self.assertRaises(SopRuntimeError):
            self.runtime.transition(run["run_id"], "running")

    def test_graph_validation_rejects_bad_definition(self):
        with self.assertRaises(SopError):
            self.store.save_sop(STORE, "bad", "坏图", [], [], start_node="x")
        with self.assertRaises(SopError):
            self.store.save_sop(STORE, "bad2", "缺出口", [{"node_id": "a", "node_type": "human"}],
                                [], start_node="a")
        with self.assertRaises(SopError):
            self.store.save_sop(STORE, "bad3", "分支缺 default",
                                [{"node_id": "a", "node_type": "condition"},
                                 {"node_id": "b", "node_type": "end"}],
                                [{"from_node": "a", "to_node": "b", "when": "x=1"}], start_node="a")

    # ---------------- 验收 3：版本 ----------------

    def test_new_version_keeps_old_run_explainable(self):
        sop_v1 = self.build()
        run = self.runtime.start(sop_v1["sop_id"], store_id=STORE)
        sop_v2 = self.build(sop_key="daily-inspection")
        self.assertEqual(sop_v2["version"], 2)
        self.assertEqual(sop_v2["status"], "active")
        self.assertEqual(self.store.get_sop(sop_v1["sop_id"])["status"], "retired")
        graph = self.store.version_graph(run["sop_id"], run["sop_version"])
        self.assertEqual(graph["version"], 1)
        self.assertEqual(graph["graph"]["start_node"], "inspect")

    # ---------------- 验收 4：超时 ----------------

    def test_sop_level_timeout_marks_run_timed_out(self):
        sop = self.build(max_hours=0.000001)
        run = self.runtime.start(sop["sop_id"], store_id=STORE)
        result = self.runtime.timeout_check(run["run_id"])
        self.assertEqual(result["timed_out"][0]["reason"], "sop_timeout")
        self.assertEqual(self.runtime.get_run(run["run_id"])["status"], "timed_out")

    def test_step_timeout_appends_exit_step(self):
        sop = self.build(human_timeout=0.000001, timeout_exit=True)
        run = self.runtime.start(sop["sop_id"], store_id=STORE)
        result = self.runtime.timeout_check(run["run_id"])
        self.assertEqual(result["timed_out"][0]["reason"], "step_timeout")
        self.assertEqual(result["timed_out"][0]["exit"], "escalate")
        final = self.runtime.get_run(run["run_id"])
        self.assertEqual(final["status"], "completed")
        statuses = [step["status"] for step in final["steps"]]
        self.assertIn("timeout", statuses)          # 自动追加的超时出口步骤

    # ---------------- 验收 5 + 边界 ----------------

    def test_task_status_change_does_not_move_the_run(self):
        sop = self.build()
        run = self.runtime.start(sop["sop_id"], store_id=STORE)
        task_id = next(step for step in run["steps"] if step["node_id"] == "inspect")["task_id"]
        self.tasks.add_evidence(task_id, "巡检照片", "evidence://inspection.png", "staff01")
        self.tasks.change_status(task_id, "accepted", "staff01")
        self.tasks.change_status(task_id, "done", "staff01")
        still = self.runtime.get_run(run["run_id"])
        self.assertEqual(still["status"], "waiting_human")   # 必须经 submit_human 才推进
        after = self.runtime.submit_human(run["run_id"], payload={"done": True}, actor="staff01")
        self.assertEqual(after["status"], "waiting_approval")

    def test_condition_branch_uses_context(self):
        nodes = [
            {"node_id": "check", "node_type": "condition"},
            {"node_id": "big", "node_type": "agent", "agent": "prototype"},
            {"node_id": "small", "node_type": "agent", "agent": "prototype"},
            {"node_id": "done", "node_type": "end"},
        ]
        edges = [
            {"from_node": "check", "to_node": "big", "when": "scale=big"},
            {"from_node": "check", "to_node": "small", "when": "default"},
            {"from_node": "big", "to_node": "done"},
            {"from_node": "small", "to_node": "done"},
        ]
        sop = self.store.save_sop(STORE, "branch", "分支测试", nodes, edges, start_node="check")
        sop = self.store.activate(sop["sop_id"])
        run = self.runtime.start(sop["sop_id"], store_id=STORE, context={"scale": "big"})
        self.assertEqual(run["status"], "completed")
        chosen = [step["node_id"] for step in run["steps"]]
        self.assertIn("big", chosen)
        self.assertNotIn("small", chosen)


if __name__ == "__main__":
    unittest.main()