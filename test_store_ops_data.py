# -*- coding: utf-8 -*-
"""第 1 期：门店侧经营数据（开台/客流、时段、库存、商圈）的导入、汇总与规则测试。"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from competitor import CompetitorStore
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from reporting import StoreReporter
from snapshot_builder import build_snapshot
from store_ops_data import (
    DATASET_INVENTORY,
    DATASET_TIMESLOT,
    DATASET_TRADE_AREA,
    DATASET_TRAFFIC,
    StoreOpsDataStore,
    import_inbox,
)
from platform_data import DATASET_STORE, DataValidationError
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
IMPORTS = ROOT / "imports"
STORE_ID = "WH-LION-001"
PASSWORD = "ShiZiZuo-2026-store"
# 与"今天"相关的日期一律相对化：硬编码日期会随真实时间流逝变成陈旧数据（stale），
# 让与新鲜度有关的用例在任意一天都能稳定复现。
LATEST = date.today() - timedelta(days=1)     # 最近一天的门店侧数据（保证"新鲜"）
LATEST_ISO = LATEST.isoformat()
DAY = LATEST + timedelta(days=1)              # 报表/快照锚点
WEEK_START = LATEST - timedelta(days=5)       # 7 天窗口，覆盖 LATEST

TRAFFIC_HEADER = "date,store_id,tables_opened,table_capacity,covers,revenue\n"
TIMESLOT_HEADER = "date,store_id,slot,covers,orders,revenue\n"
INVENTORY_HEADER = "date,store_id,item_id,name,unit,quantity,safety_stock\n"
AREA_HEADER = "store_id,area_id,area_name,area_type,note\n"


class DatasetDetectionTests(unittest.TestCase):
    def test_headers_are_recognized(self):
        self.assertEqual(StoreOpsDataStore.detect_dataset(TRAFFIC_HEADER.strip().split(",")), DATASET_TRAFFIC)
        self.assertEqual(StoreOpsDataStore.detect_dataset(TIMESLOT_HEADER.strip().split(",")), DATASET_TIMESLOT)
        self.assertEqual(StoreOpsDataStore.detect_dataset(INVENTORY_HEADER.strip().split(",")), DATASET_INVENTORY)
        self.assertEqual(StoreOpsDataStore.detect_dataset(
            ["store_id", "area_id", "area_name"]), DATASET_TRADE_AREA)
        self.assertIsNone(StoreOpsDataStore.detect_dataset(["date", "store_id"]))
        self.assertIsNone(StoreOpsDataStore.detect_dataset([]))

    def test_platform_headers_are_not_mistaken_for_ops(self):
        """平台侧表头不能被门店侧识别器抢走（两套识别器要能共存）。"""
        store_cols = (
            "date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
            "gmv,refunds,review_count,negative_review_count,avg_rating"
        ).split(",")
        self.assertIsNone(StoreOpsDataStore.detect_dataset(store_cols))

    def test_shipped_templates_match_the_importer(self):
        for filename, expected in (
            ("开台与客流导入模板.csv", DATASET_TRAFFIC),
            ("时段分布导入模板.csv", DATASET_TIMESLOT),
            ("库存与物料导入模板.csv", DATASET_INVENTORY),
            ("商圈导入模板.csv", DATASET_TRADE_AREA),
        ):
            header = StoreOpsDataStore._read_header(IMPORTS / filename)
            self.assertEqual(StoreOpsDataStore.detect_dataset(header), expected, filename)


class StoreOpsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.store = StoreOpsDataStore(self.root / "ops.db")
        self.competitor = CompetitorStore(self.root / "ops.db", ops_store=self.store)
        self.platform = PlatformMetricStore(self.root / "metrics.db")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, header, body, folder=None):
        path = (folder or self.inbox) / name
        path.write_bytes((header + body).encode("utf-8"))
        return path

    def seed_ops(self):
        """写四类门店侧数据；日期取"最近一天"，避免用例随真实日期变陈旧。"""
        self.write("traffic.csv", TRAFFIC_HEADER,
                   f"{LATEST_ISO},WH-LION-001,42,60,168,12600.50\n")
        self.write("timeslot.csv", TIMESLOT_HEADER,
                   f"{LATEST_ISO},WH-LION-001,11:00-14:00,90,36,9800\n"
                   f"{LATEST_ISO},WH-LION-001,17:00-20:00,78,30,2800\n")
        self.write("inventory.csv", INVENTORY_HEADER,
                   f"{LATEST_ISO},WH-LION-001,M001,一次性手套,箱,2,10\n"
                   f"{LATEST_ISO},WH-LION-001,M002,打包盒,箱,30,20\n")
        self.write("area.csv", AREA_HEADER, "WH-LION-001,A01,光谷步行街,商圈,主力客源\n")


class ImportTests(StoreOpsCase):
    def test_traffic_import_is_idempotent(self):
        path = self.write("t.csv", TRAFFIC_HEADER, "2026-10-19,WH-LION-001,42,60,168,12600.50\n")
        first = self.store.import_traffic_csv(path, STORE_ID)
        self.assertEqual(first.rows_written, 1)
        self.assertEqual(first.dataset, DATASET_TRAFFIC)
        second = self.store.import_traffic_csv(path, STORE_ID)
        self.assertEqual(second.rows_written, 0)          # 同一文件哈希去重
        self.assertEqual(second.batch_id, first.batch_id)

    def test_same_date_from_a_new_file_updates_in_place(self):
        self.store.import_traffic_csv(
            self.write("t1.csv", TRAFFIC_HEADER, "2026-10-19,WH-LION-001,42,60,168,12600.50\n"), STORE_ID)
        self.store.import_traffic_csv(
            self.write("t2.csv", TRAFFIC_HEADER, "2026-10-19,WH-LION-001,45,60,180,13000\n"), STORE_ID)
        rows = self.store.read_traffic(STORE_ID, "2026-10-19", "2026-10-19")
        self.assertEqual(len(rows), 1)                    # 按 (门店,日期) 幂等更新，不新增行
        self.assertEqual(rows[0]["tables_opened"], 45)

    def test_inventory_import_derives_alerts(self):
        self.write("inv.csv", INVENTORY_HEADER,
                   "2026-10-19,WH-LION-001,M001,手套,箱,2,10\n"
                   "2026-10-19,WH-LION-001,M002,打包盒,箱,8,10\n"
                   "2026-10-19,WH-LION-001,M003,纸杯,箱,30,10\n")
        self.store.import_inventory_csv(self.inbox / "inv.csv", STORE_ID)
        alerts = {item["item_id"]: item["level"] for item in self.store.read_alerts(STORE_ID)}
        self.assertEqual(alerts, {"M001": "critical", "M002": "warning"})   # M003 不预警

    def test_inventory_reimport_recalculates_alerts(self):
        self.store.import_inventory_csv(
            self.write("i1.csv", INVENTORY_HEADER, "2026-10-19,WH-LION-001,M001,手套,箱,2,10\n"), STORE_ID)
        self.assertEqual(len(self.store.read_alerts(STORE_ID)), 1)
        self.store.import_inventory_csv(
            self.write("i2.csv", INVENTORY_HEADER, "2026-10-19,WH-LION-001,M001,手套,箱,50,10\n"), STORE_ID)
        self.assertEqual(self.store.read_alerts(STORE_ID), [])              # 补货后预警消失

    def test_alert_level_boundaries(self):
        level = StoreOpsDataStore.alert_level
        self.assertEqual(level(0, 10), "critical")
        self.assertEqual(level(4.9, 10), "critical")
        self.assertEqual(level(5, 10), "warning")
        self.assertEqual(level(9.9, 10), "warning")
        self.assertIsNone(level(10, 10))
        self.assertIsNone(level(1, 0))          # 没设安全库存阈值就不预警
        self.assertIsNone(level(None, 10))

    def test_trade_area_import_uses_import_date_for_batch(self):
        result = self.store.import_trade_area_csv(
            self.write("a.csv", AREA_HEADER, "WH-LION-001,A01,光谷步行街,商圈,主力客源\n"), STORE_ID)
        self.assertEqual(result.dataset, DATASET_TRADE_AREA)
        self.assertEqual(result.min_date, date.today().isoformat())
        self.assertEqual([row["area_id"] for row in self.store.read_trade_areas(STORE_ID)], ["A01"])


class ValidationTests(StoreOpsCase):
    def expect_error(self, importer, name, header, body, fragment=""):
        path = self.write(name, header, body)
        with self.assertRaises(DataValidationError) as ctx:
            importer(path, STORE_ID)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_store_mismatch_is_rejected(self):
        self.expect_error(self.store.import_traffic_csv, "x.csv", TRAFFIC_HEADER,
                          "2026-10-19,OTHER,1,2,3,4\n", "门店编码不匹配")

    def test_duplicate_day_is_rejected(self):
        self.expect_error(self.store.import_traffic_csv, "x.csv", TRAFFIC_HEADER,
                          "2026-10-19,WH-LION-001,1,2,3,4\n2026-10-19,WH-LION-001,1,2,3,4\n", "重复记录")

    def test_tables_opened_cannot_exceed_capacity(self):
        self.expect_error(self.store.import_traffic_csv, "x.csv", TRAFFIC_HEADER,
                          "2026-10-19,WH-LION-001,99,60,1,1\n", "不能大于")

    def test_duplicate_slot_is_rejected(self):
        self.expect_error(self.store.import_timeslot_csv, "x.csv", TIMESLOT_HEADER,
                          "2026-10-19,WH-LION-001,午市,1,1,1\n2026-10-19,WH-LION-001,午市,1,1,1\n", "重复记录")

    def test_quantity_is_required(self):
        self.expect_error(self.store.import_inventory_csv, "x.csv", INVENTORY_HEADER,
                          "2026-10-19,WH-LION-001,M9,x,箱,,5\n", "quantity 不能为空")

    def test_empty_item_name_is_rejected(self):
        self.expect_error(self.store.import_inventory_csv, "x.csv", INVENTORY_HEADER,
                          "2026-10-19,WH-LION-001,M9,,箱,1,5\n", "name 不能为空")

    def test_bad_date_and_negative_number_are_rejected(self):
        self.expect_error(self.store.import_traffic_csv, "x.csv", TRAFFIC_HEADER,
                          "2026/10/19,WH-LION-001,1,2,3,4\n", "必须是 YYYY-MM-DD")
        self.expect_error(self.store.import_traffic_csv, "y.csv", TRAFFIC_HEADER,
                          "2026-10-19,WH-LION-001,-1,2,3,4\n", "不能为负数")

    def test_all_empty_row_is_rejected(self):
        self.expect_error(self.store.import_traffic_csv, "x.csv", TRAFFIC_HEADER,
                          "2026-10-19,WH-LION-001,,,,\n", "所有指标均为空")

    def test_missing_required_column_is_reported(self):
        self.expect_error(self.store.import_traffic_csv, "x.csv", "date,store_id,tables_opened\n",
                          "2026-10-19,WH-LION-001,1\n", "CSV 缺少字段")


class InboxTests(StoreOpsCase):
    def test_mixed_inbox_imports_both_sources(self):
        self.seed_ops()
        self.write("platform.csv",
                   "date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                   "gmv,refunds,review_count,negative_review_count,avg_rating\n",
                   f"{LATEST_ISO},meituan,WH-LION-001,1000,200,20,18,2400,1,10,1,4.6\n")
        payload = import_inbox(self.inbox, STORE_ID, self.platform, self.store, self.competitor)
        self.assertEqual(payload["file_count"], 5)
        self.assertEqual(payload["success_count"], 5)
        self.assertEqual(payload["failed_count"], 0)
        # 平台文件进了平台库，门店侧文件进了门店侧库
        self.assertEqual(len(self.platform.read_daily_metrics(STORE_ID, LATEST_ISO, LATEST_ISO)), 1)
        self.assertEqual(len(self.store.read_traffic(STORE_ID, LATEST_ISO, LATEST_ISO)), 1)

    def test_second_run_is_all_skipped(self):
        self.seed_ops()
        import_inbox(self.inbox, STORE_ID, self.platform, self.store, self.competitor)
        payload = import_inbox(self.inbox, STORE_ID, self.platform, self.store, self.competitor)
        self.assertEqual(payload["skipped_count"], 4)
        self.assertEqual(sum(item["rows_written"] for item in payload["files"]), 0)

    def test_one_bad_file_does_not_stop_the_others(self):
        self.seed_ops()
        (self.inbox / "unknown.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        (self.inbox / "bad.csv").write_text(
            "date,store_id,tables_opened,table_capacity,covers,revenue\n2026-10-20,OTHER,1,2,3,4\n",
            encoding="utf-8")
        payload = import_inbox(self.inbox, STORE_ID, self.platform, self.store, self.competitor)
        self.assertEqual(payload["success_count"], 4)
        self.assertEqual(payload["failed_count"], 2)
        failed = {item["file"] for item in payload["files"] if item["status"] == "failed"}
        self.assertEqual(failed, {"unknown.csv", "bad.csv"})
        for item in payload["files"]:
            if item["status"] == "failed":
                self.assertTrue(item["error"])

    def test_missing_inbox_raises(self):
        with self.assertRaises(DataValidationError):
            import_inbox(self.root / "nope", STORE_ID, self.platform, self.store, self.competitor)


class SummaryStatusTests(StoreOpsCase):
    def test_summary_aggregates_traffic_and_slots(self):
        self.seed_ops()
        import_inbox(self.inbox, STORE_ID, self.platform, self.store, self.competitor)
        summary = self.store.summary(STORE_ID, days=7, today=DAY)
        self.assertTrue(summary.has_data)
        self.assertEqual(summary.traffic["tables_opened"], 42)
        self.assertEqual(summary.traffic["covers"], 168)
        self.assertEqual(summary.traffic["turnover_rate"], 0.7)
        shares = {item["slot"]: item["order_share"] for item in summary.time_slots}
        self.assertEqual(shares["11:00-14:00"], 0.5455)
        self.assertEqual(shares["17:00-20:00"], 0.4545)
        self.assertEqual([item["item_id"] for item in summary.alerts], ["M001"])
        self.assertTrue(summary.inventory["has_data"])
        self.assertEqual(summary.inventory["latest_date"], LATEST_ISO)

    def test_summary_excludes_later_stock_when_anchored_earlier(self):
        """按历史日期复现快照时，不应看到该日期之后的盘点预警。"""
        self.store.import_inventory_csv(
            self.write("future.csv", INVENTORY_HEADER,
                       f"{(LATEST + timedelta(days=5)).isoformat()},WH-LION-001,M1,手套,箱,1,10\n"), STORE_ID)
        summary = self.store.summary(STORE_ID, days=7, today=LATEST)
        self.assertEqual(summary.alerts, [])
        self.assertFalse(summary.inventory["has_data"])

    def test_empty_store_summary_has_no_data(self):
        summary = self.store.summary(STORE_ID, days=7, today=DAY)
        self.assertFalse(summary.has_data)
        self.assertFalse(summary.traffic["has_data"])
        self.assertEqual(summary.time_slots, [])
        self.assertIsNone(summary.traffic["turnover_rate"])

    def test_summary_rejects_bad_window(self):
        for days in (0, 91):
            with self.subTest(days=days):
                with self.assertRaises(ValueError):
                    self.store.summary(STORE_ID, days=days)

    def test_status_reports_per_dataset_state(self):
        self.seed_ops()
        import_inbox(self.inbox, STORE_ID, self.platform, self.store, self.competitor)
        state = self.store.status(STORE_ID)
        for dataset in (DATASET_TRAFFIC, DATASET_TIMESLOT, DATASET_INVENTORY, DATASET_TRADE_AREA):
            self.assertEqual(state["sources"][dataset]["state"], "fresh", dataset)
        empty = StoreOpsDataStore(self.root / "empty.db").status(STORE_ID)
        self.assertFalse(empty["has_data"])
        self.assertEqual(empty["sources"][DATASET_TRAFFIC]["state"], "not_connected")

    def test_read_alerts_filters_status_and_until(self):
        self.store.import_inventory_csv(
            self.write("i.csv", INVENTORY_HEADER, "2026-10-19,WH-LION-001,M1,手套,箱,1,10\n"), STORE_ID)
        self.assertEqual(len(self.store.read_alerts(STORE_ID)), 1)
        self.assertEqual(self.store.read_alerts(STORE_ID, until="2026-10-18"), [])
        with self.assertRaises(ValueError):
            self.store.read_alerts(STORE_ID, status="不存在")


class SnapshotAndRuleTests(StoreOpsCase):
    def config(self, **overrides):
        import json
        config = json.loads((ROOT / "store_config.json").read_text(encoding="utf-8"))
        config.update(overrides)
        return config

    def test_snapshot_without_ops_store_marks_rules_degraded(self):
        snapshot = build_snapshot(self.config(), self.platform, DAY, task_store=TaskStore(self.root / "ops.db"))
        readiness = snapshot["_meta"]["rule_readiness"]
        self.assertEqual(readiness["low_slot"]["status"], "degraded")
        self.assertEqual(readiness["inventory"]["status"], "degraded")
        self.assertEqual(snapshot["ops"]["low_slots"], [])
        plan = generate_daily_plan(snapshot)
        self.assertFalse([t for t in plan["tasks"] if t["category"] == "operations"])

    def test_snapshot_picks_low_slots_by_sample_days_and_share(self):
        for offset in range(3):
            day = (LATEST - timedelta(days=offset)).isoformat()
            self.write(f"slot{offset}.csv", TIMESLOT_HEADER,
                       f"{day},WH-LION-001,11:00-14:00,90,36,9800\n"
                       f"{day},WH-LION-001,17:00-20:00,78,3,500\n")
            self.store.import_timeslot_csv(self.inbox / f"slot{offset}.csv", STORE_ID)
        snapshot = build_snapshot(self.config(), self.platform, DAY,
                                  task_store=TaskStore(self.root / "ops.db"), ops_store=self.store)
        low = [item["slot"] for item in snapshot["ops"]["low_slots"]]
        self.assertEqual(low, ["17:00-20:00"])                 # 样本 3 天且占比低于 15%
        self.assertEqual(snapshot["_meta"]["rule_readiness"]["low_slot"]["status"], "ready")

    def test_snapshot_skips_slots_with_too_few_sample_days(self):
        self.write("slot.csv", TIMESLOT_HEADER,
                   f"{LATEST_ISO},WH-LION-001,17:00-20:00,78,3,500\n")
        self.store.import_timeslot_csv(self.inbox / "slot.csv", STORE_ID)
        snapshot = build_snapshot(self.config(), self.platform, DAY,
                                  task_store=TaskStore(self.root / "ops.db"), ops_store=self.store)
        self.assertEqual(snapshot["ops"]["low_slots"], [])
        reason = snapshot["_meta"]["rule_readiness"]["low_slot"]["reason"]
        self.assertIn("样本天数不足", reason)

    def test_plan_generates_low_slot_and_restock_drafts_with_approval(self):
        snapshot = {
            "store": {"store_id": STORE_ID}, "date": "2026-10-20", "rules": {},
            "reviews": {}, "packages": [], "overdue_staff_tasks": [],
            "ops": {
                "window": {"days": 7},
                "thresholds": {"slot_min_days": 3, "low_slot_share_floor": 0.15},
                "time_slots": [], "low_slots": [
                    {"slot": "17:00-20:00", "orders": 9, "order_share": 0.05, "day_count": 5}],
                "inventory": {"has_data": True, "latest_date": "2026-10-19"},
                "inventory_alerts": [
                    {"item_id": "M001", "name": "一次性手套", "unit": "箱", "quantity": 2,
                     "safety_stock": 10, "level": "critical", "status": "open",
                     "metric_date": "2026-10-19"}],
            },
        }
        plan = generate_daily_plan(snapshot)
        ops_tasks = {item["task_id"]: item for item in plan["tasks"] if item["category"] == "operations"}
        self.assertIn("2026-10-20-low-slot-17-00-20-00", ops_tasks)
        self.assertIn("2026-10-20-inventory-restock", ops_tasks)
        for task in ops_tasks.values():
            self.assertTrue(task["requires_approval"])
            self.assertEqual(task["status"], "proposed")       # 只出草稿，不直接派单
        restock = ops_tasks["2026-10-20-inventory-restock"]
        self.assertEqual(restock["priority"], "urgent")        # 有 critical 就提级
        self.assertEqual(restock["target"]["critical_count"], 1)

    def test_plan_generates_no_ops_task_without_data(self):
        snapshot = {
            "store": {"store_id": STORE_ID}, "date": "2026-10-20", "rules": {},
            "reviews": {}, "packages": [], "overdue_staff_tasks": [],
            "ops": {"window": {"days": 7}, "time_slots": [], "low_slots": [],
                    "inventory": {"has_data": False}, "inventory_alerts": []},
        }
        plan = generate_daily_plan(snapshot)
        self.assertFalse([item for item in plan["tasks"] if item["category"] == "operations"])


class ReportingCardTests(StoreOpsCase):
    def reporter(self, config, ops_store=None):
        return StoreReporter(config, self.platform, TaskStore(self.root / "ops.db"), ops_store=ops_store)

    def test_cards_disabled_by_default(self):
        config = {"store_id": STORE_ID, "name": "测试店"}
        cards = self.reporter(config, self.store).ops_cards(WEEK_START, DAY)
        self.assertFalse(cards["time_slots"]["enabled"])
        self.assertFalse(cards["inventory_alerts"]["enabled"])
        self.assertEqual(cards["time_slots"]["state"], "disabled")
        self.assertEqual(cards["inventory_alerts"]["items"], [])

    def test_enabled_cards_return_data(self):
        self.seed_ops()
        import_inbox(self.inbox, STORE_ID, self.platform, self.store, self.competitor)
        config = {"store_id": STORE_ID, "ops_cards": {"time_slots": True, "inventory_alerts": True}}
        cards = self.reporter(config, self.store).ops_cards(WEEK_START, DAY)
        self.assertEqual(cards["time_slots"]["state"], "ok")
        self.assertEqual(len(cards["time_slots"]["items"]), 2)
        self.assertEqual(cards["inventory_alerts"]["state"], "ok")
        self.assertEqual(cards["inventory_alerts"]["items"][0]["item_id"], "M001")

    def test_enabled_but_no_data_reports_no_data(self):
        config = {"store_id": STORE_ID, "ops_cards": {"time_slots": True, "inventory_alerts": True}}
        cards = self.reporter(config, self.store).ops_cards(WEEK_START, DAY)
        self.assertEqual(cards["time_slots"]["state"], "no_data")
        self.assertEqual(cards["inventory_alerts"]["state"], "no_data")

    def test_enabled_without_ops_store_reports_not_connected(self):
        config = {"store_id": STORE_ID, "ops_cards": {"time_slots": True}}
        cards = self.reporter(config, None).ops_cards(WEEK_START, DAY)
        self.assertEqual(cards["time_slots"]["state"], "not_connected")

    def test_financial_fields_are_stripped_without_permission(self):
        self.seed_ops()
        import_inbox(self.inbox, STORE_ID, self.platform, self.store, self.competitor)
        config = {"store_id": STORE_ID, "ops_cards": {"time_slots": True}}
        cards = self.reporter(config, self.store).ops_cards(WEEK_START, DAY, include_financial=False)
        for item in cards["time_slots"]["items"]:
            self.assertNotIn("revenue", item)


class OpsApiTests(unittest.TestCase):
    def setUp(self):
        old = os.environ.pop("STORE_AGENT_MFA_ENFORCE", None)
        self.addCleanup(self._restore_env, "STORE_AGENT_MFA_ENFORCE", old)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        config = self.root / "store_config.json"
        config.write_text((ROOT / "store_config.json").read_text(encoding="utf-8"), encoding="utf-8")
        auth = AuthStore(self.root / "ops.db", tenant_id="LPL-001")
        for username, role in (("owner01", "owner"), ("staff01", "employee")):
            auth.create_user(username, PASSWORD, username, role, "LPL-001", STORE_ID)
        self.app = create_app(config_path=config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                              evidence_dir=self.root / "evidence", import_inbox=self.inbox)
        self.client = TestClient(self.app)
        self.owner = self._login("owner01")
        self.staff = self._login("staff01")
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def _restore_env(name, old):
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old

    def _login(self, username):
        response = self.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    @staticmethod
    def _headers(token):
        return {"Authorization": "Bearer " + token}

    def test_requires_authentication(self):
        self.assertEqual(self.client.get("/api/ops-data/status").status_code, 401)
        self.assertEqual(self.client.post("/api/ops-data/import").status_code, 401)

    def test_status_and_summary_are_readable_by_employee(self):
        status = self.client.get("/api/ops-data/status", headers=self._headers(self.staff))
        self.assertEqual(status.status_code, 200, status.text)
        self.assertFalse(status.json()["has_data"])
        summary = self.client.get("/api/ops-data/summary?days=7", headers=self._headers(self.staff))
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertIn("traffic", summary.json())

    def test_import_requires_ops_import_permission(self):
        (self.inbox / "t.csv").write_text(
            "date,store_id,tables_opened,table_capacity,covers,revenue\n2026-10-19,WH-LION-001,42,60,168,12600\n",
            encoding="utf-8")
        denied = self.client.post("/api/ops-data/import", headers=self._headers(self.staff))
        self.assertEqual(denied.status_code, 403, denied.text)
        allowed = self.client.post("/api/ops-data/import", headers=self._headers(self.owner))
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["success_count"], 1)
        again = self.client.post("/api/ops-data/import", headers=self._headers(self.owner))
        self.assertEqual(again.json()["skipped_count"], 1)

    def test_dashboard_exposes_card_fields(self):
        body = self.client.get("/api/dashboard", headers=self._headers(self.owner)).json()
        self.assertIn("time_slots", body)
        self.assertIn("inventory_alerts", body)
        self.assertFalse(body["time_slots"]["enabled"])          # 默认关闭


if __name__ == "__main__":
    unittest.main()