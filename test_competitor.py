# -*- coding: utf-8 -*-
"""第 2 期 B 批：竞品数据导入（九类统一收件箱、商圈完整性、合规必填、幂等与容错）。"""
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from competitor import (
    DATASET_COMPETITOR_OFFER,
    DATASET_COMPETITOR_STORE,
    CompetitorStore,
)
from platform_data import (
    DATASET_PACKAGE,
    DATASET_REVIEW,
    DATASET_STORE,
    DataValidationError,
    PlatformMetricStore,
)
from store_ops_data import (
    DATASET_INVENTORY,
    DATASET_TIMESLOT,
    DATASET_TRADE_AREA,
    DATASET_TRAFFIC,
    StoreOpsDataStore,
    import_inbox,
)

ROOT = Path(__file__).resolve().parent
IMPORTS = ROOT / "imports"
STORE_ID = "WH-LION-001"
PASSWORD = "ShiZiZuo-2026-store"

PLATFORM_HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                   "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PACKAGE_HEADER = ("date,platform,store_id,package_id,package_name,detail_views,paid_orders,"
                  "verified_orders,gmv\n")
REVIEW_HEADER = "review_id,platform,store_id,rating,created_at,status,category,replied_at\n"
TRAFFIC_HEADER = "date,store_id,tables_opened,table_capacity,covers,revenue\n"
TIMESLOT_HEADER = "date,store_id,slot,covers,orders,revenue\n"
INVENTORY_HEADER = "date,store_id,item_id,name,unit,quantity,safety_stock\n"
AREA_HEADER = "store_id,area_id,area_name,area_type,note\n"
CSTORE_HEADER = "store_id,competitor_id,competitor_name,area_id,note\n"
OFFER_HEADER = ("date,store_id,competitor_id,package_name,price,activity,activity_note,"
                "source_method,source_note,sampled_by\n")


class HeaderOwnershipTests(unittest.TestCase):
    """九类数据集必须双向互不抢占：新不抢旧、旧不抢新。"""

    HEADERS = {
        "platform_store": PLATFORM_HEADER,
        "platform_package": PACKAGE_HEADER,
        "platform_review": REVIEW_HEADER,
        "ops_traffic": TRAFFIC_HEADER,
        "ops_timeslot": TIMESLOT_HEADER,
        "ops_inventory": INVENTORY_HEADER,
        "ops_trade_area": AREA_HEADER,
        "competitor_store": CSTORE_HEADER,
        "competitor_offer": OFFER_HEADER,
    }
    EXPECTED = {
        "platform_store": ("platform", DATASET_STORE),
        "platform_package": ("platform", DATASET_PACKAGE),
        "platform_review": ("platform", DATASET_REVIEW),
        "ops_traffic": ("ops", DATASET_TRAFFIC),
        "ops_timeslot": ("ops", DATASET_TIMESLOT),
        "ops_inventory": ("ops", DATASET_INVENTORY),
        "ops_trade_area": ("ops", DATASET_TRADE_AREA),
        "competitor_store": ("competitor", DATASET_COMPETITOR_STORE),
        "competitor_offer": ("competitor", DATASET_COMPETITOR_OFFER),
    }

    def test_each_header_is_owned_by_exactly_one_detector(self):
        for name, header in self.HEADERS.items():
            columns = header.strip().split(",")
            owner, dataset = self.EXPECTED[name]
            with self.subTest(dataset=name):
                detected = (
                    ("platform", PlatformMetricStore.detect_dataset(columns)),
                    ("ops", StoreOpsDataStore.detect_dataset(columns)),
                    ("competitor", CompetitorStore.detect_dataset(columns)),
                )
                by_owner = {key: value for key, value in detected if value is not None}
                self.assertEqual(by_owner, {owner: dataset}, f"{name} 被多个识别器抢占：{detected}")

    def test_shipped_templates_match_the_importer(self):
        for filename, owner, expected in (
            ("平台数据导入模板.csv", "platform", DATASET_STORE),
            ("套餐明细导入模板.csv", "platform", DATASET_PACKAGE),
            ("评价明细导入模板.csv", "platform", DATASET_REVIEW),
            ("开台与客流导入模板.csv", "ops", DATASET_TRAFFIC),
            ("时段分布导入模板.csv", "ops", DATASET_TIMESLOT),
            ("库存与物料导入模板.csv", "ops", DATASET_INVENTORY),
            ("商圈导入模板.csv", "ops", DATASET_TRADE_AREA),
            ("竞品门店导入模板.csv", "competitor", DATASET_COMPETITOR_STORE),
            ("竞品采样导入模板.csv", "competitor", DATASET_COMPETITOR_OFFER),
        ):
            header = CompetitorStore._read_header(IMPORTS / filename)
            detector = {"platform": PlatformMetricStore.detect_dataset,
                        "ops": StoreOpsDataStore.detect_dataset,
                        "competitor": CompetitorStore.detect_dataset}[owner]
            self.assertEqual(detector(header), expected, filename)


class CompetitorCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.ops = StoreOpsDataStore(self.root / "ops.db")
        self.competitor = CompetitorStore(self.root / "ops.db", ops_store=self.ops)
        self.platform = PlatformMetricStore(self.root / "metrics.db")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, header, body, folder=None):
        path = (folder or self.inbox) / name
        path.write_bytes((header + body).encode("utf-8"))
        return path

    def seed_area(self, area_id="A01"):
        return self.ops.import_trade_area_csv(
            self.write("z_area.csv", AREA_HEADER, f"{STORE_ID},{area_id},光谷步行街,商圈,\n"), STORE_ID)

    def seed_stores(self):
        self.seed_area()
        return self.competitor.import_competitor_store_csv(
            self.write("competitor_store.csv", CSTORE_HEADER,
                       "WH-LION-001,CMP-001,同商圈A店,A01,\nWH-LION-001,CMP-002,同商圈B店,A01,\n"),
            STORE_ID)


class CompetitorStoreImportTests(CompetitorCase):
    def test_import_and_idempotency(self):
        self.seed_area()
        path = self.write("c.csv", CSTORE_HEADER, "WH-LION-001,CMP-001,A店,A01,备注\n")
        first = self.competitor.import_competitor_store_csv(path, STORE_ID)
        self.assertEqual(first.rows_written, 1)
        self.assertEqual(first.dataset, DATASET_COMPETITOR_STORE)
        second = self.competitor.import_competitor_store_csv(path, STORE_ID)
        self.assertEqual(second.rows_written, 0)
        self.assertEqual(second.batch_id, first.batch_id)

    def test_reimport_same_competitor_from_new_file_updates_in_place(self):
        self.seed_area()
        self.competitor.import_competitor_store_csv(
            self.write("a.csv", CSTORE_HEADER, "WH-LION-001,CMP-001,A店,A01,\n"), STORE_ID)
        self.competitor.import_competitor_store_csv(
            self.write("b.csv", CSTORE_HEADER, "WH-LION-001,CMP-001,A店改名,A01,\n"), STORE_ID)
        rows = self.competitor.read_stores(STORE_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["competitor_name"], "A店改名")

    def test_area_must_exist_in_store_trade_areas(self):
        self.seed_area("A01")
        path = self.write("c.csv", CSTORE_HEADER, "WH-LION-001,CMP-001,A店,A99,\n")
        with self.assertRaises(DataValidationError) as ctx:
            self.competitor.import_competitor_store_csv(path, STORE_ID)
        self.assertIn("不在本店商圈清单中", str(ctx.exception))

    def test_missing_trade_areas_gives_actionable_message(self):
        empty = CompetitorStore(self.root / "empty.db")
        path = self.write("c.csv", CSTORE_HEADER, "WH-LION-001,CMP-001,A店,A01,\n")
        with self.assertRaises(DataValidationError) as ctx:
            empty.import_competitor_store_csv(path, STORE_ID)
        self.assertIn("import-trade-area", str(ctx.exception))

    def test_validation_errors(self):
        self.seed_area()
        cases = (
            ("门店不匹配", "OTHER,CMP-1,A店,A01,\n", "门店编码不匹配"),
            ("competitor_id 为空", "WH-LION-001,,A店,A01,\n", "competitor_id 不能为空"),
            ("competitor_name 为空", "WH-LION-001,CMP-1,,A01,\n", "competitor_name 不能为空"),
            ("area_id 为空", "WH-LION-001,CMP-1,A店,,\n", "area_id 不能为空"),
            ("文件内重复", "WH-LION-001,CMP-1,A店,A01,\nWH-LION-001,CMP-1,A店2,A01,\n", "重复记录"),
        )
        for tag, body, fragment in cases:
            with self.subTest(case=tag):
                path = self.write(f"bad_{tag}.csv", CSTORE_HEADER, body)
                with self.assertRaises(DataValidationError) as ctx:
                    self.competitor.import_competitor_store_csv(path, STORE_ID)
                self.assertIn(fragment, str(ctx.exception))

    def test_missing_required_column_is_reported(self):
        self.seed_area()
        path = self.write("c.csv", "store_id,competitor_id,competitor_name\n", "WH-LION-001,C1,A\n")
        with self.assertRaises(DataValidationError) as ctx:
            self.competitor.import_competitor_store_csv(path, STORE_ID)
        self.assertIn("CSV 缺少字段", str(ctx.exception))


class CompetitorOfferImportTests(CompetitorCase):
    def offer_body(self, **over):
        row = {
            "date": "2026-10-19", "store_id": STORE_ID, "competitor_id": "CMP-001",
            "package_name": "双人套餐", "price": "168", "activity": "满100减20",
            "activity_note": "平台活动页可见", "source_method": "平台前台",
            "source_note": "公开页面人工查看", "sampled_by": "员工小李",
        }
        row.update(over)
        return ",".join(row[key] for key in (
            "date", "store_id", "competitor_id", "package_name", "price",
            "activity", "activity_note", "source_method", "source_note", "sampled_by")) + "\n"

    def test_import_and_idempotency(self):
        path = self.write("o.csv", OFFER_HEADER, self.offer_body())
        first = self.competitor.import_competitor_offer_csv(path, STORE_ID)
        self.assertEqual(first.rows_written, 1)
        self.assertEqual(first.min_date, "2026-10-19")
        self.assertEqual(first.dataset, DATASET_COMPETITOR_OFFER)
        second = self.competitor.import_competitor_offer_csv(path, STORE_ID)
        self.assertEqual(second.rows_written, 0)

    def test_history_is_kept_across_sample_dates(self):
        """不同采样日期必须各留一行，这是后续算环比的前提。"""
        self.competitor.import_competitor_offer_csv(
            self.write("d1.csv", OFFER_HEADER, self.offer_body(date="2026-10-12", price="158")), STORE_ID)
        self.competitor.import_competitor_offer_csv(
            self.write("d2.csv", OFFER_HEADER, self.offer_body(date="2026-10-19", price="168")), STORE_ID)
        rows = self.competitor.read_offers(STORE_ID, "2026-10-01", "2026-10-31")
        self.assertEqual([(row["sample_date"], row["price"]) for row in rows],
                         [("2026-10-12", 158.0), ("2026-10-19", 168.0)])

    def test_window_read_excludes_out_of_range(self):
        self.competitor.import_competitor_offer_csv(
            self.write("o.csv", OFFER_HEADER, self.offer_body(date="2026-09-01")), STORE_ID)
        self.assertEqual(self.competitor.read_offers(STORE_ID, "2026-10-01", "2026-10-31"), [])

    def test_compliance_fields_are_required(self):
        for field in ("sampled_by", "source_method", "source_note"):
            with self.subTest(field=field):
                path = self.write(f"o_{field}.csv", OFFER_HEADER, self.offer_body(**{field: ""}))
                with self.assertRaises(DataValidationError) as ctx:
                    self.competitor.import_competitor_offer_csv(path, STORE_ID)
                self.assertIn(f"{field} 不能为空", str(ctx.exception))

    def test_optional_fields_may_be_empty(self):
        path = self.write("o.csv", OFFER_HEADER, self.offer_body(activity="", activity_note=""))
        self.assertEqual(self.competitor.import_competitor_offer_csv(path, STORE_ID).rows_written, 1)
        row = self.competitor.read_offers(STORE_ID, "2026-10-01", "2026-10-31")[0]
        self.assertIsNone(row["activity"])
        self.assertIsNone(row["activity_note"])

    def test_price_and_date_validation(self):
        cases = (
            ("price 为空", {"price": ""}, "price 不能为空"),
            ("price 非数字", {"price": "abc"}, "price 不是数字"),
            ("price 为负", {"price": "-1"}, "price 不能为负数"),
            ("日期非法", {"date": "2026/10/19"}, "必须是 YYYY-MM-DD"),
            ("套餐名为空", {"package_name": ""}, "package_name 不能为空"),
        )
        for tag, over, fragment in cases:
            with self.subTest(case=tag):
                path = self.write(f"bad_{tag}.csv", OFFER_HEADER, self.offer_body(**over))
                with self.assertRaises(DataValidationError) as ctx:
                    self.competitor.import_competitor_offer_csv(path, STORE_ID)
                self.assertIn(fragment, str(ctx.exception))

    def test_duplicate_row_in_file_is_rejected(self):
        path = self.write("dup.csv", OFFER_HEADER, self.offer_body() + self.offer_body(price="150"))
        with self.assertRaises(DataValidationError) as ctx:
            self.competitor.import_competitor_offer_csv(path, STORE_ID)
        self.assertIn("重复记录", str(ctx.exception))


class UnifiedInboxTests(CompetitorCase):
    """九类数据集混放同一个收件箱：一次导入全部落库。"""

    def seed_nine_files(self):
        self.write("01_platform.csv", PLATFORM_HEADER,
                   "2026-10-19,meituan,WH-LION-001,1000,200,20,18,2400,1,10,1,4.6\n")
        self.write("02_package.csv", PACKAGE_HEADER,
                   "2026-10-19,meituan,WH-LION-001,MT-1,套餐A,100,5,4,600\n")
        self.write("03_review.csv", REVIEW_HEADER,
                   "R-1,meituan,WH-LION-001,5,2026-10-19 12:00:00,open,,\n")
        self.write("04_traffic.csv", TRAFFIC_HEADER, "2026-10-19,WH-LION-001,42,60,168,12600\n")
        self.write("05_timeslot.csv", TIMESLOT_HEADER, "2026-10-19,WH-LION-001,11:00-14:00,90,36,9800\n")
        self.write("06_inventory.csv", INVENTORY_HEADER, "2026-10-19,WH-LION-001,M1,手套,箱,2,10\n")
        self.write("07_area.csv", AREA_HEADER, "WH-LION-001,A01,光谷步行街,商圈,\n")
        self.write("08_competitor_store.csv", CSTORE_HEADER, "WH-LION-001,CMP-001,A店,A01,\n")
        self.write("09_competitor_offer.csv", OFFER_HEADER,
                   "2026-10-19,WH-LION-001,CMP-001,双人套餐,168,,,平台前台,公开页面,小李\n")

    def test_all_nine_datasets_import_in_one_run(self):
        self.seed_nine_files()
        payload = import_inbox(self.inbox, STORE_ID, self.platform, self.ops, self.competitor)
        self.assertEqual(payload["file_count"], 9)
        self.assertEqual(payload["success_count"], 9)
        self.assertEqual(payload["failed_count"], 0)
        self.assertEqual(len(self.platform.read_daily_metrics(STORE_ID, "2026-10-19", "2026-10-19")), 1)
        self.assertEqual(len(self.ops.read_traffic(STORE_ID, "2026-10-19", "2026-10-19")), 1)
        self.assertEqual(len(self.competitor.read_stores(STORE_ID)), 1)
        self.assertEqual(len(self.competitor.read_offers(STORE_ID, "2026-10-01", "2026-10-31")), 1)

    def test_second_run_is_all_skipped(self):
        self.seed_nine_files()
        import_inbox(self.inbox, STORE_ID, self.platform, self.ops, self.competitor)
        payload = import_inbox(self.inbox, STORE_ID, self.platform, self.ops, self.competitor)
        self.assertEqual(payload["skipped_count"], 9)
        self.assertEqual(sum(item["rows_written"] for item in payload["files"]), 0)

    def test_competitor_store_before_area_still_succeeds(self):
        """竞品门店要校验商圈，但商圈文件未必排在前面——挂起重试后仍应成功。"""
        self.write("a_competitor_store.csv", CSTORE_HEADER, "WH-LION-001,CMP-001,A店,A01,\n")
        self.write("z_area.csv", AREA_HEADER, "WH-LION-001,A01,光谷步行街,商圈,\n")
        payload = import_inbox(self.inbox, STORE_ID, self.platform, self.ops, self.competitor)
        self.assertEqual(payload["success_count"], 2, payload["files"])
        self.assertEqual([item["file"] for item in payload["files"]],
                         ["a_competitor_store.csv", "z_area.csv"])
        self.assertEqual(len(self.competitor.read_stores(STORE_ID)), 1)

    def test_bad_area_still_fails_with_clear_reason(self):
        self.write("a_competitor_store.csv", CSTORE_HEADER, "WH-LION-001,CMP-001,A店,A99,\n")
        self.write("z_area.csv", AREA_HEADER, "WH-LION-001,A01,光谷步行街,商圈,\n")
        payload = import_inbox(self.inbox, STORE_ID, self.platform, self.ops, self.competitor)
        self.assertEqual(payload["success_count"], 1)
        self.assertEqual(payload["failed_count"], 1)
        failed = [item for item in payload["files"] if item["status"] == "failed"]
        self.assertIn("不在本店商圈清单中", failed[0]["error"])

    def test_one_bad_file_does_not_stop_the_others(self):
        self.seed_nine_files()
        (self.inbox / "zz_unknown.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        (self.inbox / "zz_bad.csv").write_text(
            "date,store_id,competitor_id,package_name,price,sampled_by,source_method,source_note\n"
            "2026/10/19,WH-LION-001,C1,X,10,小李,平台前台,来源\n", encoding="utf-8")
        payload = import_inbox(self.inbox, STORE_ID, self.platform, self.ops, self.competitor)
        self.assertEqual(payload["success_count"], 9)
        self.assertEqual(payload["failed_count"], 2)
        self.assertEqual({item["file"] for item in payload["files"] if item["status"] == "failed"},
                         {"zz_unknown.csv", "zz_bad.csv"})

    def test_competitor_import_shares_the_ops_ledger(self):
        """竞品数据与门店侧四类共用 ops_import_batches，不另开第二本账。"""
        self.seed_nine_files()
        import_inbox(self.inbox, STORE_ID, self.platform, self.ops, self.competitor)
        with self.ops._connection() as conn:
            rows = conn.execute(
                "select dataset, count(*) as n from ops_import_batches group by dataset order by dataset"
            ).fetchall()
        datasets = {row["dataset"]: row["n"] for row in rows}
        self.assertEqual(datasets.get(DATASET_COMPETITOR_STORE), 1)
        self.assertEqual(datasets.get(DATASET_COMPETITOR_OFFER), 1)
        self.assertEqual(len(datasets), 6)          # 门店侧四类 + 竞品两类


class OpsDataApiCompetitorTests(unittest.TestCase):
    """收件箱接口必须能把竞品文件一起导入（AppContext 已接入 CompetitorStore）。"""

    def setUp(self):
        old = os.environ.pop("STORE_AGENT_MFA_ENFORCE", None)
        self.addCleanup(self._restore_env, "STORE_AGENT_MFA_ENFORCE", old)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        config = self.root / "store_config.json"
        config.write_text((ROOT / "store_config.json").read_text(encoding="utf-8"), encoding="utf-8")
        AuthStore(self.root / "ops.db", tenant_id="LPL-001").create_user(
            "owner01", PASSWORD, "店主", "owner", "LPL-001", STORE_ID)
        self.app = create_app(config_path=config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                              evidence_dir=self.root / "evidence", import_inbox=self.inbox)
        self.client = TestClient(self.app)
        response = self.client.post("/api/auth/login",
                                    json={"username": "owner01", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.token = response.json()["token"]
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

    def test_competitor_files_import_through_the_api(self):
        (self.inbox / "area.csv").write_text(
            AREA_HEADER + "WH-LION-001,A01,光谷步行街,商圈,\n", encoding="utf-8")
        (self.inbox / "store.csv").write_text(
            CSTORE_HEADER + "WH-LION-001,CMP-001,A店,A01,\n", encoding="utf-8")
        (self.inbox / "offer.csv").write_text(
            OFFER_HEADER + "2026-10-19,WH-LION-001,CMP-001,双人套餐,168,,,平台前台,公开页面,小李\n",
            encoding="utf-8")
        response = self.client.post("/api/ops-data/import",
                                    headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["success_count"], 3, body["files"])
        self.assertEqual(body["failed_count"], 0)
        datasets = {item["dataset"] for item in body["files"]}
        self.assertEqual(datasets, {"ops_trade_area", DATASET_COMPETITOR_STORE, DATASET_COMPETITOR_OFFER})


class MatchGateTests(CompetitorCase):
    """匹配是硬闸门：只有 confirmed 参与对比，pending / rejected 一律不参与。"""

    def setUp(self):
        super().setUp()
        self.seed_stores()
        self.competitor.import_competitor_offer_csv(
            self.write("offer.csv", OFFER_HEADER,
                       "2026-10-19,WH-LION-001,CMP-001,双人套餐,168,,,平台前台,公开页面,小李\n"
                       "2026-10-19,WH-LION-001,CMP-002,单人套餐,68,,,到店,实地,小王\n"),
            STORE_ID)

    def match(self, **over):
        payload = {"platform": "meituan", "package_id": "MT-1",
                   "competitor_id": "CMP-001", "competitor_package_name": "双人套餐"}
        payload.update(over)
        return self.competitor.upsert_match(STORE_ID, actor_id="u-manager", **payload)

    def test_confirm_marks_actor_and_time(self):
        result = self.match()
        self.assertTrue(result["created"])
        self.assertIsNone(result["previous_status"])
        record = result["record"]
        self.assertEqual(record["status"], "confirmed")
        self.assertEqual(record["confirmed_by"], "u-manager")
        self.assertTrue(record["confirmed_at"])
        self.assertEqual([row["package_id"] for row in self.competitor.confirmed_matches(STORE_ID)], ["MT-1"])

    def test_pending_and_rejected_never_enter_the_gate(self):
        self.match()
        self.match(package_id="MT-2", competitor_id="CMP-002", competitor_package_name="单人套餐",
                   status="pending")
        self.match(package_id="MT-3", status="rejected")
        self.assertEqual({row["package_id"] for row in self.competitor.confirmed_matches(STORE_ID)},
                         {"MT-1"})
        all_matches = self.competitor.list_matches(STORE_ID)
        self.assertEqual(len(all_matches), 3)
        self.assertEqual({row["package_id"] for row in all_matches if row["status"] == "pending"}, {"MT-2"})
        self.assertEqual({row["package_id"] for row in all_matches if row["status"] == "rejected"}, {"MT-3"})

    def test_pending_does_not_set_confirmed_by_or_at(self):
        record = self.match(status="pending")["record"]
        self.assertIsNone(record["confirmed_by"])
        self.assertIsNone(record["confirmed_at"])

    def test_update_reports_previous_status(self):
        self.match()
        again = self.match(status="rejected")
        self.assertFalse(again["created"])
        self.assertEqual(again["previous_status"], "confirmed")
        self.assertEqual(again["record"]["status"], "rejected")
        self.assertEqual(self.competitor.confirmed_matches(STORE_ID), [])
        self.assertEqual(len(self.competitor.list_matches(STORE_ID)), 1)   # 仍是同一行

    def test_status_filter_and_invalid_status(self):
        self.match()
        self.match(status="pending", competitor_id="CMP-002", competitor_package_name="单人套餐")
        self.assertEqual(len(self.competitor.list_matches(STORE_ID, status="pending")), 1)
        with self.assertRaises(ValueError):
            self.competitor.list_matches(STORE_ID, status="不存在")

    def test_unregistered_competitor_is_rejected(self):
        with self.assertRaises(DataValidationError) as ctx:
            self.match(competitor_id="CMP-999")
        self.assertIn("尚未登记", str(ctx.exception))
        self.assertIn("import-competitor-store", str(ctx.exception))

    def test_competitor_package_without_sample_is_rejected(self):
        with self.assertRaises(DataValidationError) as ctx:
            self.match(competitor_package_name="不存在的套餐")
        self.assertIn("没有名为", str(ctx.exception))
        self.assertIn("import-competitor-offer", str(ctx.exception))

    def test_field_and_enum_validation(self):
        for over, fragment in (
            ({"platform": "taobao"}, "platform 只支持"),
            ({"platform": ""}, "platform 只支持"),
            ({"status": "maybe"}, "status 只支持"),
            ({"package_id": "  "}, "package_id 不能为空"),
            ({"competitor_package_name": ""}, "competitor_package_name 不能为空"),
        ):
            with self.subTest(**over):
                with self.assertRaises(DataValidationError) as ctx:
                    self.match(**over)
                self.assertIn(fragment, str(ctx.exception))

    def test_unmatched_packages_list_shrinks_after_confirmation(self):
        self.assertEqual(
            {row["package_name"] for row in self.competitor.unmatched_competitor_packages(STORE_ID)},
            {"双人套餐", "单人套餐"})
        self.match()                      # 确认 双人套餐
        self.assertEqual(
            {row["package_name"] for row in self.competitor.unmatched_competitor_packages(STORE_ID)},
            {"单人套餐"})
        self.match(competitor_id="CMP-002", competitor_package_name="单人套餐", status="pending")
        # pending 不算已匹配，仍应出现在"尚未匹配"清单里
        self.assertEqual(
            {row["package_name"] for row in self.competitor.unmatched_competitor_packages(STORE_ID)},
            {"单人套餐"})


class MatchApiTests(unittest.TestCase):
    """接口层：权限闸门（店长级以上）+ 审计留痕。"""

    ROLES = (("owner01", "owner"), ("manager01", "manager"), ("area01", "area_manager"),
             ("operator01", "operator"), ("staff01", "employee"))

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
        self.user_ids = {}
        for username, role in self.ROLES:
            created = auth.create_user(username, PASSWORD, username, role, "LPL-001", STORE_ID)
            self.user_ids[username] = created["user_id"]
        self.auth = auth
        self.app = create_app(config_path=config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                              evidence_dir=self.root / "evidence", import_inbox=self.inbox)
        self.client = TestClient(self.app)
        self.tokens = {username: self._login(username) for username, _ in self.ROLES}
        self.client.cookies.clear()
        self.seed_competitor_data()

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
        response = self.client.post("/api/auth/login",
                                    json={"username": username, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def _headers(self, username):
        return {"Authorization": "Bearer " + self.tokens[username]}

    def seed_competitor_data(self):
        """先经导入接口把商圈 / 竞品门店 / 竞品采样落库，再做匹配。"""
        (self.inbox / "area.csv").write_text(
            AREA_HEADER + "WH-LION-001,A01,光谷步行街,商圈,\n", encoding="utf-8")
        (self.inbox / "store.csv").write_text(
            CSTORE_HEADER + "WH-LION-001,CMP-001,A店,A01,\n", encoding="utf-8")
        (self.inbox / "offer.csv").write_text(
            OFFER_HEADER + "2026-10-19,WH-LION-001,CMP-001,双人套餐,168,,,平台前台,公开页面,小李\n",
            encoding="utf-8")
        response = self.client.post("/api/ops-data/import", headers=self._headers("owner01"))
        self.assertEqual(response.json()["success_count"], 3, response.text)

    def match_body(self, **over):
        body = {"platform": "meituan", "package_id": "MT-1", "competitor_id": "CMP-001",
                "competitor_package_name": "双人套餐"}
        body.update(over)
        return body

    def audit_events(self):
        return [row["event_type"] for row in self.auth.list_audit(store_id=STORE_ID, limit=50)]

    def test_read_requires_authentication(self):
        self.assertEqual(self.client.get("/api/competitor/matches").status_code, 401)
        self.assertEqual(self.client.post("/api/competitor/matches", json=self.match_body()).status_code, 401)

    def test_post_is_limited_to_manager_and_above(self):
        for username in ("operator01", "staff01"):
            with self.subTest(role=username):
                response = self.client.post("/api/competitor/matches", json=self.match_body(),
                                            headers=self._headers(username))
                self.assertEqual(response.status_code, 403, response.text)
        for username in ("manager01", "area01", "owner01"):
            with self.subTest(role=username):
                response = self.client.post("/api/competitor/matches", json=self.match_body(),
                                            headers=self._headers(username))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["match"]["status"], "confirmed")

    def test_confirm_writes_audit_with_actor(self):
        self.client.post("/api/competitor/matches", json=self.match_body(),
                         headers=self._headers("manager01"))
        rows = [row for row in self.auth.list_audit(store_id=STORE_ID, limit=50)
                if row["event_type"] == "competitor_match_confirmed"]
        self.assertEqual(len(rows), 1)
        # 审计里记的是内部 user_id（与登录返回的 user.user_id 同一标识），不是登录名
        self.assertEqual(rows[0]["actor_id"], self.user_ids["manager01"])
        self.assertIn("MT-1", rows[0]["object_id"])

    def test_reject_writes_audit_and_records_previous_status(self):
        self.client.post("/api/competitor/matches", json=self.match_body(),
                         headers=self._headers("manager01"))
        response = self.client.post("/api/competitor/matches",
                                    json=self.match_body(status="rejected"),
                                    headers=self._headers("manager01"))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["created"])
        self.assertEqual(body["previous_status"], "confirmed")
        self.assertEqual(body["match"]["status"], "rejected")
        self.assertIn("competitor_match_rejected", self.audit_events())

    def test_pending_is_stored_but_not_confirmed(self):
        body = self.client.post("/api/competitor/matches", json=self.match_body(status="pending"),
                                headers=self._headers("manager01")).json()
        self.assertEqual(body["match"]["status"], "pending")
        self.assertIsNone(body["match"]["confirmed_at"])
        listed = self.client.get("/api/competitor/matches", headers=self._headers("staff01")).json()
        self.assertEqual(listed["counts"]["pending"], 1)
        self.assertEqual(listed["counts"]["confirmed"], 0)

    def test_get_lists_counts_and_unmatched(self):
        self.client.post("/api/competitor/matches", json=self.match_body(),
                         headers=self._headers("manager01"))
        body = self.client.get("/api/competitor/matches", headers=self._headers("staff01")).json()
        self.assertEqual(body["counts"]["confirmed"], 1)
        self.assertEqual(len(body["matches"]), 1)
        self.assertEqual(body["unmatched_competitor_packages"], [])
        self.assertIn("只有 confirmed", body["note"])

    def test_get_status_filter(self):
        self.client.post("/api/competitor/matches", json=self.match_body(status="rejected"),
                         headers=self._headers("manager01"))
        body = self.client.get("/api/competitor/matches?status=confirmed",
                               headers=self._headers("manager01")).json()
        self.assertEqual(body["matches"], [])
        self.assertEqual(body["counts"]["rejected"], 1)
        denied = self.client.get("/api/competitor/matches?status=bogus",
                                 headers=self._headers("manager01"))
        self.assertEqual(denied.status_code, 400)

    def test_invalid_match_returns_400_with_reason(self):
        response = self.client.post("/api/competitor/matches",
                                    json=self.match_body(competitor_id="CMP-999"),
                                    headers=self._headers("manager01"))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("尚未登记", response.json()["detail"])

if __name__ == "__main__":
    unittest.main()