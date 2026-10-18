# -*- coding: utf-8 -*-
"""批次 5：快照组装（数据→规则输入）与任务落库（状态机/证据/逾期/会话）的测试。"""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import SnapshotError, build_snapshot, load_config, parse_target_date
from task_store import TaskStore, TaskStoreError

ROOT = Path(__file__).resolve().parent
STORE_ID = "WH-LION-001"
STORE_HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PACKAGE_HEADER = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
REVIEW_HEADER = "review_id,platform,store_id,rating,created_at,status,category,replied_at\n"

CONFIG = {
    "tenant_id": "LPL-001",
    "store_id": STORE_ID,
    "name": "武汉狮子座店",
    "people": {
        "store_manager": "武汉狮子座店店长",
        "review_operator": "员工小李",
        "online_operator": "线上运营",
        "area_manager": "区域负责人",
    },
    "review_cycle": {"cycle_start": "2026-09-01", "cycle_end": "2026-09-30", "target_qualified_month": 40},
    "package_costs": [{"platform": "meituan", "package_id": "MT-1", "sale_price": 200, "variable_cost": 140}],
    "rules": {"negative_review_sla_hours": 4, "package_conversion_floor": 0.05,
              "package_min_views": 50, "package_margin_floor": 0.25},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metrics = PlatformMetricStore(self.root / "metrics.db")
        self.tasks = TaskStore(self.root / "store_ops.db")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, header, body):
        path = self.root / name
        path.write_bytes((header + body).encode("utf-8"))
        return path

    def snapshot(self, day="2026-09-03", config=None, with_tasks=True):
        return build_snapshot(config or CONFIG, self.metrics, day,
                              task_store=self.tasks if with_tasks else None)


class SnapshotTests(Base):
    def test_yesterday_verified_orders_drive_invite_capacity(self):
        self.metrics.import_csv(self.write("m.csv", STORE_HEADER,
            "2026-09-02,meituan,WH-LION-001,100,20,5,4,300,0,1,0,4.8\n"), STORE_ID)
        self.metrics.import_csv(self.write("d.csv", STORE_HEADER,
            "2026-09-02,douyin,WH-LION-001,80,15,4,3,200,0,1,0,4.7\n"), STORE_ID)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["reviews"]["completed_orders_yesterday"], 7)
        self.assertEqual(snapshot["reviews"]["eligible_review_invites_today"], 7)

    def test_daily_invite_cap_and_missing_yesterday_data(self):
        capped = json.loads(json.dumps(CONFIG))
        capped["review_cycle"]["daily_invite_cap"] = 3
        self.metrics.import_csv(self.write("m.csv", STORE_HEADER,
            "2026-09-02,meituan,WH-LION-001,100,20,5,4,300,0,1,0,4.8\n"), STORE_ID)
        self.assertEqual(self.snapshot(config=capped)["reviews"]["eligible_review_invites_today"], 3)

        empty = self.snapshot(day="2026-09-10")
        self.assertEqual(empty["reviews"]["completed_orders_yesterday"], 0)
        self.assertTrue(any("缺少 2026-09-09 的平台数据" in note for note in empty["_meta"]["notes"]))

    def test_expired_cycle_blocks_review_invite_and_creates_repair_task(self):
        config = json.loads(json.dumps(CONFIG))
        snapshot = self.snapshot(day="2026-10-18", config=config)
        self.assertEqual(snapshot["_meta"]["rule_readiness"]["review_invite"]["status"], "blocked")
        self.assertFalse(snapshot["_meta"]["rule_readiness"]["review_invite"]["reason"] == "")
        plan = generate_daily_plan(snapshot)
        self.assertFalse(any("评价邀请" in task["title"] for task in plan["tasks"]))
        self.assertTrue(any("补齐" in task["title"] or "数据" in task["title"] for task in plan["tasks"]))

    def test_missing_yesterday_data_marks_rule_degraded_without_zero_fill(self):
        snapshot = self.snapshot(day="2026-09-10")
        ready = snapshot["_meta"]["rule_readiness"]["review_invite"]
        self.assertEqual(ready["status"], "blocked")
        self.assertIn("缺少", ready["reason"])
        self.assertEqual(snapshot["reviews"]["completed_orders_yesterday"], 0)

    def test_cycle_target_and_remaining_days_come_from_config(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot["reviews"]["qualified_review_target_month"], 40)
        self.assertEqual(snapshot["reviews"]["days_remaining_in_cycle"], 28)
        self.assertEqual(snapshot["_meta"]["cycle"]["end"], "2026-09-30")

    def test_reviews_drive_qualified_count_and_negative_age(self):
        self.metrics.import_reviews_csv(self.write("r.csv", REVIEW_HEADER,
            "MT-1024,meituan,WH-LION-001,2,2026-09-03 09:15:00,open,服务体验,\n"
            "MT-1001,meituan,WH-LION-001,5,2026-09-01 18:00:00,replied,环境安静,2026-09-01 19:00:00\n"),
            STORE_ID)
        # 一个文件只能含一个平台；周期外的那条不应计入有效评价
        self.metrics.import_reviews_csv(self.write("r2.csv", REVIEW_HEADER,
            "MT-1002,douyin,WH-LION-001,4,2026-08-20 10:00:00,resolved,环境安静,2026-08-20 11:00:00\n"),
            STORE_ID)
        snapshot = self.snapshot()
        # 只有周期内(rating>=4 且已回复/已解决)的 09-01 那条计入
        self.assertEqual(snapshot["reviews"]["qualified_reviews_month"], 1)
        negatives = snapshot["reviews"]["open_negative_reviews"]
        self.assertEqual(len(negatives), 1)
        self.assertEqual(negatives[0]["review_id"], "MT-1024")
        self.assertEqual(negatives[0]["age_hours"], 14)          # 09:15 → 当日 23:59:59
        self.assertFalse(negatives[0]["compensation_requested"])  # 赔付不由系统推断

    def test_package_needs_cost_data_and_sums_over_window(self):
        self.metrics.import_packages_csv(self.write("p.csv", PACKAGE_HEADER,
            "2026-09-01,meituan,WH-LION-001,MT-1,低价套餐,100,5,4,1000\n"
            "2026-09-02,meituan,WH-LION-001,MT-1,低价套餐,68,0,0,0\n"
            "2026-08-20,meituan,WH-LION-001,MT-1,低价套餐,999,99,99,9999\n"      # 窗口外
            "2026-09-01,meituan,WH-LION-001,MT-NOCOST,无成本套餐,100,1,1,200\n"), STORE_ID)
        snapshot = self.snapshot()
        self.assertEqual([p["package_id"] for p in snapshot["packages"]], ["MT-1"])
        self.assertEqual(snapshot["packages"][0]["detail_views_7d"], 168)   # 窗口外 999 不计
        self.assertEqual(snapshot["packages"][0]["paid_orders_7d"], 5)
        self.assertEqual(snapshot["packages"][0]["gross_margin_rate"], 0.3)  # (200-140)/200
        self.assertTrue(any("缺少售价或成本" in note for note in snapshot["_meta"]["notes"]))

    def test_overdue_tasks_flow_into_snapshot(self):
        self.tasks.save_plan({
            "date": "2026-09-03",
            "tasks": [{
                "task_id": "T-1", "category": "reputation", "title": "处理差评",
                "assignee": "员工小李", "priority": "high", "due_at": "2026-09-03T18:00:00+08:00",
                "target": {}, "evidence_required": [], "reason": "已超过 SLA",
                "requires_approval": False,
            }],
        }, STORE_ID)
        self.assertEqual(self.tasks.mark_overdue(STORE_ID, moment="2026-09-04T09:00:00"), 1)
        snapshot = self.snapshot(day="2026-09-05")
        self.assertEqual(len(snapshot["overdue_staff_tasks"]), 1)
        self.assertEqual(snapshot["overdue_staff_tasks"][0]["employee_id"], "员工小李")
        self.assertEqual(snapshot["overdue_staff_tasks"][0]["missed_count"], 1)

    def test_snapshot_without_task_store_notes_the_gap(self):
        snapshot = self.snapshot(with_tasks=False)
        self.assertEqual(snapshot["overdue_staff_tasks"], [])
        self.assertTrue(any("未接入任务库" in note for note in snapshot["_meta"]["notes"]))

    def test_config_and_date_validation(self):
        with self.assertRaises(SnapshotError):
            load_config(self.root / "missing.json")
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SnapshotError):
            load_config(broken)
        with self.assertRaises(SnapshotError):
            self.snapshot(config={k: v for k, v in CONFIG.items() if k != "store_id"})
        with self.assertRaises(SnapshotError):
            parse_target_date("2026/09/03")

    def test_parse_target_date_keywords(self):
        self.assertEqual(parse_target_date("2026-09-03"), date(2026, 9, 3))
        self.assertEqual(parse_target_date(None), date.today())
        self.assertEqual(parse_target_date("today"), date.today())
        self.assertEqual(parse_target_date("yesterday"), date.today() - timedelta(days=1))


class TaskStoreTests(Base):
    def plan(self, day="2026-09-03", tasks=None):
        return {
            "date": day,
            "tasks": tasks if tasks is not None else [{
                "task_id": f"{day}-review-invite", "category": "reputation", "title": "邀请评价",
                "assignee": "员工小李", "priority": "medium", "due_at": f"{day}T21:00:00+08:00",
                "target": {"qualified_review_invites": 2}, "evidence_required": ["脱敏订单号"],
                "reason": "周期目标未达成", "requires_approval": False,
            }, {
                "task_id": f"{day}-package-MT-1", "category": "package", "title": "复核套餐",
                "assignee": "线上运营", "priority": "high", "due_at": f"{day}T17:00:00+08:00",
                "target": {"change_one_variable_only": True}, "evidence_required": ["审批记录"],
                "reason": "转化率低于阈值", "requires_approval": True,
            }],
        }

    def test_save_plan_is_idempotent_and_keeps_progress(self):
        first = self.tasks.save_plan(self.plan(), STORE_ID)
        self.assertEqual((first["created"], first["updated"]), (2, 0))
        invite = self.tasks.get_task("2026-09-03-review-invite")
        self.assertEqual(invite["status"], "ready")
        self.assertEqual(self.tasks.get_task("2026-09-03-package-MT-1")["status"], "proposed")

        self.tasks.change_status("2026-09-03-review-invite", "accepted", "员工小李")
        second = self.tasks.save_plan(self.plan(), STORE_ID)
        self.assertEqual((second["created"], second["updated"]), (0, 2))
        # 重新生成不覆盖人工进度
        self.assertEqual(self.tasks.get_task("2026-09-03-review-invite")["status"], "accepted")
        self.assertEqual(len(self.tasks.list_tasks(STORE_ID)), 2)

    def test_evidence_guard_and_state_machine(self):
        self.tasks.save_plan(self.plan(), STORE_ID)
        task_id = "2026-09-03-review-invite"
        with self.assertRaises(TaskStoreError):
            self.tasks.change_status(task_id, "done", "员工小李")          # 无证据不能完成
        self.tasks.change_status(task_id, "accepted", "员工小李")
        self.tasks.add_evidence(task_id, "脱敏订单号", "MT-20260902-0007", "员工小李")
        self.assertEqual(self.tasks.change_status(task_id, "done", "员工小李")["status"], "done")
        self.assertEqual(self.tasks.change_status(task_id, "verified", "店长")["status"], "verified")
        with self.assertRaises(TaskStoreError):
            self.tasks.change_status(task_id, "accepted", "员工小李")      # verified 是终态
        with self.assertRaises(TaskStoreError):
            self.tasks.change_status("not-exist", "accepted", "员工小李")
        with self.assertRaises(TaskStoreError):
            self.tasks.change_status(task_id, "不存在的状态", "员工小李")
        events = [(e["from_status"], e["to_status"]) for e in self.tasks.events(task_id)]
        self.assertEqual(events, [(None, "ready"), ("ready", "accepted"),
                                  ("accepted", "accepted"), ("accepted", "done"), ("done", "verified")])

    def test_evidence_requires_type_and_ref(self):
        self.tasks.save_plan(self.plan(), STORE_ID)
        with self.assertRaises(TaskStoreError):
            self.tasks.add_evidence("2026-09-03-review-invite", "", "ref", "员工小李")

    def test_mark_overdue_skips_proposed_and_closed_tasks(self):
        self.tasks.save_plan(self.plan(), STORE_ID)      # 邀请=ready，套餐=proposed(待审批)
        marked = self.tasks.mark_overdue(STORE_ID, moment="2026-09-10T00:00:00")
        self.assertEqual(marked, 1)
        overdue = self.tasks.overdue_tasks(STORE_ID)
        self.assertEqual([t["task_id"] for t in overdue], ["2026-09-03-review-invite"])
        self.assertEqual(overdue[0]["missed_count"], 1)
        # 待审批任务不算逾期：负责人无法在审批前执行，责任在审批人
        self.assertEqual(self.tasks.get_task("2026-09-03-package-MT-1")["status"], "proposed")
        # 已验收的终态不受扫描影响，且重复扫描不会重复标记
        task_id = "2026-09-03-review-invite"
        self.tasks.change_status(task_id, "accepted", "员工小李")
        self.tasks.add_evidence(task_id, "脱敏订单号", "X-1", "员工小李")
        self.tasks.change_status(task_id, "done", "员工小李")
        self.tasks.change_status(task_id, "verified", "店长")
        self.assertEqual(self.tasks.mark_overdue(STORE_ID, moment="2026-09-10T00:00:00"), 0)
        self.assertEqual(self.tasks.get_task(task_id)["status"], "verified")
        self.assertEqual(self.tasks.count_by_status(STORE_ID), {"proposed": 1, "verified": 1})

    def test_session_binding_prevents_identity_switch(self):
        bound = self.tasks.bind_session("s-1", "manager-001", "manager")
        self.assertEqual(bound["user_id"], "manager-001")
        self.assertEqual(self.tasks.session("s-1")["user_role"], "manager")
        self.assertIsNone(self.tasks.session("s-404"))
        with self.assertRaises(TaskStoreError):
            self.tasks.bind_session("s-1", "staff-001", "employee")

    def test_list_tasks_filters_and_validates(self):
        self.tasks.save_plan(self.plan(), STORE_ID)
        self.assertEqual(len(self.tasks.list_tasks(STORE_ID, status="open")), 2)
        self.assertEqual(len(self.tasks.list_tasks(STORE_ID, status="ready")), 1)
        self.assertEqual(len(self.tasks.list_tasks(STORE_ID, assignee="线上运营")), 1)
        with self.assertRaises(TaskStoreError):
            self.tasks.list_tasks(STORE_ID, status="不存在")


class EndToEndTests(Base):
    def test_data_change_alone_changes_the_task_list(self):
        """验收核心：只改数据库数据、不改任何 JSON，任务清单随之变化。"""
        self.metrics.import_csv(self.write("m.csv", STORE_HEADER,
            "2026-09-02,meituan,WH-LION-001,100,20,5,4,300,0,1,0,4.8\n"), STORE_ID)
        first_plan = generate_daily_plan(self.snapshot())
        self.tasks.save_plan(first_plan, STORE_ID)
        self.assertEqual(len(self.tasks.list_tasks(STORE_ID)), 1)     # 仅评价邀请

        # 只新增一条待处理差评，不改任何配置
        self.metrics.import_reviews_csv(self.write("r.csv", REVIEW_HEADER,
            "MT-9999,meituan,WH-LION-001,1,2026-09-03 10:00:00,open,服务体验,\n"), STORE_ID)
        second_plan = generate_daily_plan(self.snapshot())
        self.tasks.save_plan(second_plan, STORE_ID)
        titles = [t["title"] for t in self.tasks.list_tasks(STORE_ID)]
        self.assertEqual(len(titles), 2)
        self.assertTrue(any("差评" in title for title in titles))

    def test_real_config_file_is_loadable_and_matches_prototype_input(self):
        config = load_config(ROOT / "store_config.json")
        snapshot = build_snapshot(config, self.metrics, "2026-09-03", task_store=self.tasks)
        plan = generate_daily_plan(snapshot)          # 不抛异常即证明输入契约兼容
        self.assertEqual(plan["store"]["store_id"], config["store_id"])
        self.assertEqual(plan["mode"], "draft_only")
        self.assertTrue(plan["guardrails"])


if __name__ == "__main__":
    unittest.main()
