# -*- coding: utf-8 -*-
"""批次 9：审批流、工具调用留痕与审计查询的测试。"""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from auth import AuthStore
from governance import (
    ApprovalStore,
    AuditCenter,
    ToolRunLog,
    apply_decision,
    log_tool_call,
)
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import build_snapshot, load_config
from task_store import TaskStore, TaskStoreError

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
STORE_B = "WH-LION-002"
TENANT = "LPL-001"
STORE_HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG_HEADER = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
REV_HEADER = "review_id,platform,store_id,rating,created_at,status,category,replied_at\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "ops.db"
        self.metrics_db = self.root / "metrics.db"
        self.config = load_config(ROOT / "store_config.json")
        self.tasks = TaskStore(self.db, tenant_id=TENANT)
        self.approvals = ApprovalStore(self.db, tenant_id=TENANT)
        self.tool_log = ToolRunLog(self.db, tenant_id=TENANT)
        self.audit = AuthStore(self.db, tenant_id=TENANT)
        self.center = AuditCenter(self.db, tenant_id=TENANT)
        self.today = date.today()
        self.metrics = PlatformMetricStore(self.metrics_db, tenant_id=TENANT)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, header, body):
        path = self.root / name
        path.write_bytes((header + body).encode("utf-8"))
        return path

    def seed_plan(self, day=None):
        day = day or self.today
        yesterday = day - timedelta(days=1)
        self.metrics.import_csv(self.write("m.csv", STORE_HEADER,
            f"{yesterday},meituan,{STORE},100,20,5,4,300,0,1,0,4.8\n"), STORE)
        self.metrics.import_packages_csv(self.write("p.csv", PKG_HEADER,
            f"{day},meituan,{STORE},MT-1632214010,AI全周小包2h,168,5,4,1200\n"), STORE)
        self.metrics.import_reviews_csv(self.write("r.csv", REV_HEADER,
            f"MT-1024,meituan,{STORE},2,{day} 09:15:00,open,服务体验,\n"), STORE)
        snapshot = build_snapshot(self.config, self.metrics, day, task_store=self.tasks)
        plan = generate_daily_plan(snapshot)
        self.tasks.save_plan(plan, STORE)
        return plan


class ApprovalTests(Base):
    def test_sync_from_plan_creates_one_pending_per_gated_task(self):
        plan = self.seed_plan()
        gated = [t for t in plan["tasks"] if t["requires_approval"]]
        first = self.approvals.sync_from_plan(plan, STORE)
        self.assertEqual(first["created"], len(gated))
        self.assertEqual(first["skipped"], 0)
        self.assertGreaterEqual(first["pending"], 1)
        second = self.approvals.sync_from_plan(plan, STORE)
        self.assertEqual((second["created"], second["skipped"]), (0, len(gated)))
        self.assertEqual(len(self.approvals.list(STORE, status="pending")), len(gated))
        fields = self.approvals.list(STORE, status="pending")[0]
        for key in ("approval_id", "store_id", "action_type", "payload_snapshot", "requested_by",
                    "requested_at", "status"):
            self.assertIn(key, fields)
        self.assertTrue(fields["action_type"].startswith("task:"))
        self.assertIsInstance(fields["payload_snapshot"], dict)

    def test_request_is_idempotent_while_pending(self):
        first = self.approvals.request(STORE, "pricing:change", {"package_id": "MT-1", "price": 168},
                                       requested_by="线上运营")
        second = self.approvals.request(STORE, "pricing:change", {"package_id": "MT-1", "price": 168},
                                        requested_by="线上运营")
        self.assertFalse(first["skipped"])
        self.assertTrue(second["skipped"])
        self.assertEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(len(self.approvals.list(STORE)), 1)

    def test_rejected_request_can_be_reopened(self):
        first = self.approvals.request(STORE, "pricing:change", {"price": 168}, requested_by="线上运营")
        self.approvals.decide(first["approval_id"], "rejected", "店长", "毛利不达标")
        second = self.approvals.request(STORE, "pricing:change", {"price": 168}, requested_by="线上运营")
        self.assertFalse(second["skipped"])
        self.assertNotEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(len(self.approvals.list(STORE)), 2)
        self.assertEqual(self.approvals.count_by_status(STORE), {"pending": 1, "rejected": 1})

    def test_decision_validation(self):
        approval = self.approvals.request(STORE, "pricing:change", {"price": 168}, requested_by="线上运营")
        with self.assertRaises(ValueError):
            self.approvals.decide(approval["approval_id"], "maybe", "店长")
        with self.assertRaises(ValueError):
            self.approvals.decide(approval["approval_id"], "rejected", "店长")          # 驳回必须写原因
        decided = self.approvals.decide(approval["approval_id"], "approved", "店长", "毛利可接受")
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(decided["decided_by"], "店长")
        self.assertEqual(decided["decision_reason"], "毛利可接受")
        with self.assertRaises(ValueError):
            self.approvals.decide(approval["approval_id"], "approved", "店长")          # 不能重复决策
        with self.assertRaises(ValueError):
            self.approvals.get("apr-not-exist")

    def test_apply_decision_moves_linked_task(self):
        plan = self.seed_plan()
        gated = [t for t in plan["tasks"] if t["requires_approval"]][0]
        self.assertEqual(self.tasks.get_task(gated["task_id"])["status"], "proposed")
        self.approvals.sync_from_plan(plan, STORE)
        approval = [a for a in self.approvals.list(STORE, status="pending")
                    if a["task_id"] == gated["task_id"]][0]
        result = apply_decision(self.approvals, self.tasks, approval["approval_id"], "approved", "店长", "同意")
        self.assertEqual(result["approval"]["status"], "approved")
        self.assertEqual(result["task"]["status"], "ready")
        self.assertIsNone(result["task_note"])

    def test_apply_rejection_sets_task_rejected(self):
        plan = self.seed_plan()
        gated = [t for t in plan["tasks"] if t["requires_approval"]][0]
        self.approvals.sync_from_plan(plan, STORE)
        approval = [a for a in self.approvals.list(STORE, status="pending")
                    if a["task_id"] == gated["task_id"]][0]
        result = apply_decision(self.approvals, self.tasks, approval["approval_id"], "rejected",
                                "店长", "成本数据不全")
        self.assertEqual(result["task"]["status"], "rejected")
        self.assertEqual(self.tasks.get_task(gated["task_id"])["status"], "rejected")

    def test_decision_records_note_when_task_already_moved(self):
        plan = self.seed_plan()
        gated = [t for t in plan["tasks"] if t["requires_approval"]][0]
        self.approvals.sync_from_plan(plan, STORE)
        approval = [a for a in self.approvals.list(STORE, status="pending")
                    if a["task_id"] == gated["task_id"]][0]
        self.tasks.change_status(gated["task_id"], "ready", "店长", "线下已批")   # proposed → ready
        result = apply_decision(self.approvals, self.tasks, approval["approval_id"], "approved", "店长", "同意")
        self.assertEqual(result["approval"]["status"], "approved")
        self.assertIsNone(result["task"])
        self.assertIn("任务未流转", result["task_note"])

    def test_ad_hoc_approval_without_task(self):
        approval = self.approvals.request(STORE, "pricing:change", {"package_id": "MT-1"},
                                          requested_by="线上运营", reason="想试单变量调价")
        result = apply_decision(self.approvals, self.tasks, approval["approval_id"], "approved", "店长", "同意")
        self.assertIsNone(result["task"])
        self.assertIsNone(result["task_note"])


class ToolRunTests(Base):
    def test_record_list_and_count(self):
        run_id = self.tool_log.record(STORE, "api:data_quality", actor_id="manager01",
                                      session_id="s-1", input_payload={"days": 30},
                                      output_payload={"missing_day_count": 1})
        self.assertTrue(run_id.startswith("run-"))
        rows = self.tool_log.list(STORE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_name"], "api:data_quality")
        self.assertEqual(rows[0]["mode"], "read")
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["input_redacted"], {"days": 30})
        self.assertEqual(rows[0]["output_redacted"], {"missing_day_count": 1})
        self.tool_log.record(STORE, "api:daily_report", actor_id="manager01")
        self.assertEqual(self.tool_log.count_by_tool(STORE)[0]["total"], 1)

    def test_payloads_are_redacted(self):
        self.tool_log.record(STORE, "api:chat", actor_id="manager01",
                            input_payload={"phone": "13812345678", "password": "should-be-masked"},
                            output_payload={"answer": "顾客 13900001111 反馈"})
        row = self.tool_log.list(STORE)[0]
        self.assertEqual(row["input_redacted"]["phone"], "138****5678")
        self.assertEqual(row["input_redacted"]["password"], "***")
        self.assertIn("139****1111", row["output_redacted"]["answer"])

    def test_invalid_mode_and_status_rejected(self):
        with self.assertRaises(ValueError):
            self.tool_log.record(STORE, "api:x", mode="magic")
        with self.assertRaises(ValueError):
            self.tool_log.record(STORE, "api:x", status="maybe")

    def test_log_tool_call_is_side_channel(self):
        self.assertIsNone(log_tool_call(None, STORE, "api:x"))

        class Broken:
            def record(self, **kwargs):
                raise RuntimeError("db down")

        self.assertIsNone(log_tool_call(Broken(), STORE, "api:x"))       # 留痕失败不影响主流程
        run_id = log_tool_call(self.tool_log, STORE, "api:bootstrap", actor_id="manager01")
        self.assertTrue(run_id.startswith("run-"))

    def test_scope_isolation(self):
        self.tool_log.record(STORE, "api:data_quality")
        self.tool_log.record(STORE_B, "api:data_quality")
        self.assertEqual(len(self.tool_log.list(STORE)), 1)
        self.assertEqual(len(self.tool_log.list(STORE_B)), 1)


class AuditCenterTests(Base):
    def seed_events(self):
        self.audit.record_audit(event_type="login_success", actor_type="user", actor_id="manager01",
                                tenant_id=TENANT, store_id=STORE, object_type="session", object_id="s-1")
        self.audit.record_audit(event_type="permission_denied", actor_type="user", actor_id="staff01",
                                tenant_id=TENANT, store_id=STORE, object_type="endpoint",
                                object_id="GET /api/reports/daily", after={"required_permission": "report:read"})
        self.audit.record_audit(event_type="login_failed", actor_type="anonymous", actor_id="staff01",
                                tenant_id=TENANT, store_id=STORE, object_type="session", object_id="staff01")
        self.audit.record_audit(event_type="login_success", actor_type="user", actor_id="other-store",
                                tenant_id=TENANT, store_id=STORE_B, object_type="session", object_id="s-9")

    def test_list_events_filters_and_scope(self):
        self.seed_events()
        events = self.center.list_events(STORE)
        self.assertEqual(len(events), 3)                     # 另一个门店的事件不可见
        self.assertEqual([e["store_id"] for e in events], [STORE] * 3)
        self.assertEqual(len(self.center.list_events(STORE, event_type="login_success")), 1)
        self.assertEqual(len(self.center.list_events(STORE, actor_id="staff01")), 2)
        denied = self.center.list_events(STORE, event_type="permission_denied")[0]
        self.assertEqual(denied["after"]["required_permission"], "report:read")
        self.assertIn("login_failed", self.center.event_types(STORE))
        self.assertNotIn("login_success", self.center.event_types(STORE_B)[:0])  # 仅为可读性

    def test_summary_counts(self):
        self.seed_events()
        summary = self.center.summary(STORE, days=7)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["count_by_event"]["login_success"], 1)
        self.assertEqual(summary["permission_denied"], 1)
        self.assertEqual(summary["login_failed"], 1)
        self.assertEqual(summary["top_actors"][0]["actor_id"] in ("manager01", "staff01"), True)
        with self.assertRaises(ValueError):
            self.center.summary(STORE, days=0)

    def test_json_fields_are_decoded(self):
        self.audit.record_audit(event_type="task_status_changed", actor_type="user", actor_id="manager01",
                                tenant_id=TENANT, store_id=STORE, object_type="action_task",
                                object_id="t-1", before={"status": "proposed"}, after={"status": "ready"})
        event = self.center.list_events(STORE)[0]
        self.assertEqual(event["before"], {"status": "proposed"})
        self.assertEqual(event["after"], {"status": "ready"})


if __name__ == "__main__":
    unittest.main()
