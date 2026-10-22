# -*- coding: utf-8 -*-
"""第 3 期第 3 步：表驱动规则执行器的逐字段一致性回归。

锚点：同一份快照，`rule_eval.evaluate_tasks()`（按规则表定义执行）必须与
`prototype.generate_daily_plan()`（内置硬编码规则）产出**逐字段、含顺序**完全一致。
"""
import tempfile
import unittest
from pathlib import Path

from prototype import generate_daily_plan
from datetime import date, timedelta

from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from rule_eval import RuleEvalError, evaluate_tasks
from rule_seed import BUILTIN_RULE_KEYS, seed_builtin_rules
from rule_store import RuleStore
from snapshot_builder import build_snapshot, load_config
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
CONFIG = load_config(ROOT / "store_config.json")
STORE_ID = "WH-LION-001"


def base_snapshot():
    return {
        "store": {"tenant_id": "T1", "store_id": STORE_ID, "name": "武汉狮子座店"},
        "date": "2026-09-03",
        "store_manager": "店长",
        "online_operator": "运营小李",
        "review_operator": "店员A",
        "area_manager": "区域负责人",
        "rules": dict(CONFIG.get("rules") or {}),
        "reviews": {
            "eligible_review_invites_today": 3,
            "qualified_reviews_month": 8,
            "qualified_review_target_month": 20,
            "days_remaining_in_cycle": 4,
            "open_negative_reviews": [],
        },
        "packages": [],
        "overdue_staff_tasks": [],
    }


def full_snapshot():
    """一份覆盖全部 7 条规则的快照。"""
    snapshot = base_snapshot()
    snapshot["reviews"]["open_negative_reviews"] = [
        {"review_id": "R-overdue", "age_hours": 9, "compensation_requested": True},
        {"review_id": "R-fresh", "age_hours": 1, "assignee": "店员B",
         "due_at": "2026-09-03T19:30:00+08:00"},
    ]
    snapshot["packages"] = [
        {"package_id": "P1", "name": "低转化套餐", "detail_views_7d": 100,
         "paid_orders_7d": 2, "gross_margin_rate": 0.3},
        {"package_id": "P2", "name": "正常套餐", "detail_views_7d": 100,
         "paid_orders_7d": 20, "gross_margin_rate": 0.3},
    ]
    snapshot["overdue_staff_tasks"] = [
        {"task_id": "OT1", "employee_id": "E1", "missed_count": 1},
        {"task_id": "OT2", "employee_id": "E2", "missed_count": 3},
    ]
    snapshot["ops"] = {
        "window": {"days": 7},
        "low_slots": [
            {"slot": "17:00-20:00", "orders": 12, "day_count": 7, "order_share": 0.08},
            {"slot": "21:00-23:00", "orders": 4, "day_count": 5, "order_share": 0.03},
        ],
        "inventory": {"latest_date": "2026-09-02"},
        "inventory_alerts": [
            {"item_id": "I1", "name": "一次性手套", "unit": "盒", "quantity": 2,
             "safety_stock": 10, "level": "critical"},
            {"item_id": "I2", "name": "打包盒", "unit": "个", "quantity": 50,
             "safety_stock": 100, "level": "warning"},
        ],
    }
    snapshot["competitor"] = {
        "thresholds": {"area_shift_percent": (CONFIG.get("rules") or {}).get("competitor_area_shift_percent", 10)},
        "price": {"p25": 100, "p75": 150},
        "by_area": [
            {"area_id": "A1", "sample_size": 6, "current_median": 160,
             "previous_median": 140, "change_percent": 14.3, "shifted": True},
            {"area_id": "A2", "sample_size": 5, "current_median": 150,
             "previous_median": 150, "change_percent": 0.0, "shifted": False},
        ],
        "packages": {"matches": [
            {"area_id": "A1", "store_price": 200, "competitor_price": 130,
             "competitor_package_name": "竞品双人餐"},
            {"area_id": "A1", "store_price": 90, "competitor_price": 130,
             "competitor_package_name": "竞品单人餐"},
            {"area_id": "A2", "store_price": 200, "competitor_price": 130,
             "competitor_package_name": "竞品三人餐"},
        ]},
    }
    return snapshot


class ConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RuleStore(Path(self.temp.name) / "rules.db")
        seed_builtin_rules(self.store, STORE_ID, CONFIG)
        self.rules = {key: self.store.get_rule(key) for key in BUILTIN_RULE_KEYS}

    def tearDown(self):
        self.temp.cleanup()

    def assert_same(self, snapshot, allow_degraded=False):
        builtin = generate_daily_plan(snapshot, allow_degraded=allow_degraded)["tasks"]
        table = evaluate_tasks(snapshot, self.rules, allow_degraded=allow_degraded)
        self.assertEqual(builtin, table)
        return table

    def test_empty_paths_match(self):
        snapshot = base_snapshot()
        snapshot["reviews"]["qualified_reviews_month"] = 20
        self.assertEqual(self.assert_same(snapshot), [])

    def test_review_invite_matches(self):
        tasks = self.assert_same(base_snapshot())
        self.assertEqual([item["task_id"] for item in tasks], ["2026-09-03-review-invite"])

    def test_all_seven_rules_match_field_by_field(self):
        tasks = self.assert_same(full_snapshot())
        categories = {item["category"] for item in tasks}
        self.assertEqual(categories, {"reputation", "package", "management", "operations", "competitor"})
        competitor_tasks = [item for item in tasks if item["category"] == "competitor"]
        self.assertEqual(len(competitor_tasks), 2)
        self.assertEqual(
            [item["task_id"] for item in competitor_tasks],
            ["2026-09-03-competitor-A1", "2026-09-03-competitor-A2"],
        )

    def test_inventory_aggregates_and_truncates(self):
        snapshot = base_snapshot()
        snapshot["reviews"]["qualified_reviews_month"] = 20
        alerts = [
            {"item_id": f"I{i}", "name": f"物料{i}", "unit": "件", "quantity": 1,
             "safety_stock": 10, "level": "critical" if i >= 10 else "warning"}
            for i in range(12)
        ]
        snapshot["ops"] = {"inventory": {"latest_date": "2026-09-02"}, "inventory_alerts": alerts}
        tasks = self.assert_same(snapshot)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["target"]["alert_count"], 12)
        self.assertEqual(tasks[0]["target"]["critical_count"], 2)
        self.assertEqual(tasks[0]["target"]["truncated"], 2)
        self.assertEqual(tasks[0]["priority"], "urgent")

    def test_blocked_rules_produce_data_repair(self):
        snapshot = base_snapshot()
        snapshot["_meta"] = {"rule_readiness": {
            "low_slot": {"status": "blocked", "reason": "缺时段数据"},
            "inventory": {"status": "blocked", "reason": "缺库存盘点"},
        }}
        snapshot["ops"] = full_snapshot()["ops"]
        tasks = self.assert_same(snapshot)
        repair = [item for item in tasks if item["task_id"].endswith("data-repair")]
        self.assertEqual(len(repair), 1)
        self.assertEqual(repair[0]["target"]["blocked_rules"], ["inventory", "low_slot"])
        self.assertFalse(any(item["task_id"].endswith("low-slot-17-00-20-00") for item in tasks))
        self.assertFalse(any(item["task_id"].endswith("inventory-restock") for item in tasks))

    def test_allow_degraded_matches(self):
        snapshot = base_snapshot()
        snapshot["_meta"] = {"rule_readiness": {
            "review_invite": {"status": "blocked", "reason": "缺评价数据"},
            "low_slot": {"status": "blocked", "reason": "缺时段数据"},
        }}
        snapshot["ops"] = full_snapshot()["ops"]
        tasks = self.assert_same(snapshot, allow_degraded=True)
        self.assertFalse(any(item["task_id"].endswith("data-repair") for item in tasks))
        self.assertTrue(any(item["task_id"].endswith("review-invite") for item in tasks))
        self.assertTrue(any(item["task_id"].endswith("low-slot-17-00-20-00") for item in tasks))

    def test_missing_rule_definition_is_rejected(self):
        broken = {key: value for key, value in self.rules.items() if key != "package"}
        with self.assertRaises(RuleEvalError):
            evaluate_tasks(base_snapshot(), broken)

    def test_table_threshold_drives_decision(self):
        """规则表里的阈值是判定依据：调低转化率下限后，表驱动不再产出该套餐任务。"""
        snapshot = full_snapshot()
        template = dict(self.rules["package"]["action_template_json"])
        condition = dict(self.rules["package"]["condition_json"])
        condition["conversion_floor"] = 0.01
        draft = self.store.save_draft(
            "package", scope=self.rules["package"]["scope"], condition=condition,
            action_template=template)
        approved = self.store.decide("package", draft["version"], "approved")
        rules = dict(self.rules)
        rules["package"] = approved
        table_tasks = evaluate_tasks(snapshot, rules)
        builtin_tasks = generate_daily_plan(snapshot)["tasks"]
        self.assertTrue(any(item["task_id"].endswith("package-P1") for item in builtin_tasks))
        self.assertFalse(any(item["task_id"].endswith("package-P1") for item in table_tasks))



STORE_HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG_HEADER = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
REV_HEADER = "review_id,platform,store_id,rating,created_at,status,category,replied_at\n"


def _write(root, name, header, body):
    path = root / name
    path.write_bytes((header + body).encode("utf-8"))
    return path


class BuiltSnapshotConsistencyTests(unittest.TestCase):
    """真实构建路径（build_snapshot 读取已导入数据与配置）也必须逐字段一致。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        metrics = PlatformMetricStore(root / "metrics.db")
        tasks = TaskStore(root / "ops.db")
        self.store = RuleStore(root / "rules.db")
        seed_builtin_rules(self.store, STORE_ID, CONFIG)
        self.rules = {key: self.store.get_rule(key) for key in BUILTIN_RULE_KEYS}
        today = date.today()
        yesterday = today - timedelta(days=1)
        metrics.import_csv(_write(root, "m.csv", STORE_HEADER,
            f"{yesterday},meituan,{STORE_ID},100,20,5,4,300,0,1,0,4.8\n"
            f"{today},meituan,{STORE_ID},110,22,5,4,320,0,1,0,4.8\n"), STORE_ID)
        metrics.import_csv(_write(root, "d.csv", STORE_HEADER,
            f"{yesterday},douyin,{STORE_ID},80,15,4,3,200,0,1,0,4.7\n"
            f"{today},douyin,{STORE_ID},85,16,4,3,210,0,1,0,4.7\n"), STORE_ID)
        metrics.import_packages_csv(_write(root, "p.csv", PKG_HEADER,
            f"{today},meituan,{STORE_ID},MT-1632214010,AI全周小包2h,168,5,4,1200\n"), STORE_ID)
        metrics.import_reviews_csv(_write(root, "r.csv", REV_HEADER,
            f"MT-1024,meituan,{STORE_ID},2,{today} 09:15:00,open,服务体验,\n"
            f"MT-1001,meituan,{STORE_ID},5,{today} 10:00:00,replied,环境安静,{today} 11:00:00\n"),
            STORE_ID)
        self.snapshot = build_snapshot(CONFIG, metrics, today, task_store=tasks)

    def tearDown(self):
        self.temp.cleanup()

    def test_built_snapshot_matches(self):
        builtin = generate_daily_plan(self.snapshot)["tasks"]
        table = evaluate_tasks(self.snapshot, self.rules)
        self.assertEqual(builtin, table)

    def test_prototype_injected_rules_match_builtin(self):
        """prototype 支持注入规则定义后，产出必须与内置分支一致。"""
        snapshot = full_snapshot()
        builtin = generate_daily_plan(snapshot)
        injected = generate_daily_plan(snapshot, rules=self.rules)
        self.assertEqual(builtin["tasks"], injected["tasks"])
        self.assertEqual(builtin["summary"], injected["summary"])
        self.assertEqual(builtin["mode"], injected["mode"])

    def test_prototype_injected_rules_drive_threshold(self):
        """注入的规则表阈值必须真实生效（表驱动，而非摆设）。"""
        snapshot = full_snapshot()
        condition = dict(self.rules["package"]["condition_json"])
        condition["conversion_floor"] = 0.01
        draft = self.store.save_draft(
            "package", scope=self.rules["package"]["scope"], condition=condition,
            action_template=self.rules["package"]["action_template_json"])
        approved = self.store.decide("package", draft["version"], "approved")
        rules = dict(self.rules, package=approved)
        builtin = generate_daily_plan(snapshot)["tasks"]
        injected = generate_daily_plan(snapshot, rules=rules)["tasks"]
        self.assertTrue(any(item["task_id"].endswith("package-P1") for item in builtin))
        self.assertFalse(any(item["task_id"].endswith("package-P1") for item in injected))


    def test_tasks_carry_rule_source_for_traceability(self):
        """第 6 步前提：任务必须能追溯到产生它的规则与版本。"""
        from prototype import BUILTIN_RULE_VERSION
        snapshot = full_snapshot()
        table_tasks = generate_daily_plan(snapshot, rules=self.rules)["tasks"]
        package_task = next(item for item in table_tasks if item["task_id"].endswith("package-P1"))
        self.assertEqual(package_task["rule_key"], "package")
        self.assertEqual(package_task["rule_version"], "1")
        builtin_tasks = generate_daily_plan(snapshot)["tasks"]
        builtin_package = next(item for item in builtin_tasks if item["task_id"].endswith("package-P1"))
        self.assertEqual(builtin_package["rule_key"], "package")
        self.assertEqual(builtin_package["rule_version"], BUILTIN_RULE_VERSION)
        for item in builtin_tasks:
            self.assertTrue(item["rule_key"], item["task_id"])

    def test_task_store_persists_rule_source(self):
        from task_store import TaskStore
        snapshot = full_snapshot()
        plan = generate_daily_plan(snapshot, rules=self.rules)
        store = TaskStore(Path(self.temp.name) / "persist.db")
        store.save_plan(plan, STORE_ID)
        record = store.get_task(plan["tasks"][0]["task_id"])
        self.assertTrue(record["rule_key"])
        self.assertTrue(record["rule_version"])


if __name__ == "__main__":
    unittest.main()