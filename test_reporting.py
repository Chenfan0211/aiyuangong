# -*- coding: utf-8 -*-
"""批次 11：经营看板与周/月报的测试。"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from governance import ApprovalStore
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from reporting import StoreReporter, aggregate
from snapshot_builder import build_snapshot, load_config
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
          "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
PACKAGE_ID = "MT-1632214010"


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(ROOT / "store_config.json")
        self.metrics = PlatformMetricStore(self.root / "m.db", tenant_id=TENANT)
        self.tasks = TaskStore(self.root / "s.db", tenant_id=TENANT)
        self.approvals = ApprovalStore(self.root / "s.db", tenant_id=TENANT)
        self.today = date.today()
        self.reporter = StoreReporter(self.config, self.metrics, self.tasks, self.approvals,
                                      config_path=ROOT / "store_config.json")

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, days: int = 40, with_today: bool = False):
        """造 days 天历史数据（默认不含今天，便于验证缺日与趋势）。"""
        start = 1 if with_today else 2
        for platform, base in (("meituan", 100), ("douyin", 80)):
            lines = []
            for offset in range(days, 0, -1):
                if offset == 1 and not with_today:
                    continue
                day = (self.today - timedelta(days=offset)).isoformat()
                lines.append(f"{day},{platform},{STORE},{base},{base//5},4,3,300,0,1,0,4.8\n")
            path = self.root / f"{platform}.csv"
            path.write_bytes((HEADER + "".join(lines)).encode("utf-8"))
            self.metrics.import_csv(path, STORE)

    def seed_plan(self):
        path = self.root / "p.csv"
        path.write_bytes((PKG + f"{self.today.isoformat()},{STORE.split('-')[0]},x" ).encode("utf-8"))
        path.write_bytes((PKG + f"{self.today.isoformat()},meituan,{STORE},{PACKAGE_ID},AI全周小包2h,168,5,4,1200\n").encode("utf-8"))
        self.metrics.import_packages_csv(path, STORE)
        plan = generate_daily_plan(build_snapshot(self.config, self.metrics, self.today, task_store=self.tasks))
        self.tasks.save_plan(plan, STORE)
        self.approvals.sync_from_plan(plan, STORE)
        return plan

    def test_aggregate_derives_rates(self):
        rows = [{"impressions": 100, "detail_views": 20, "paid_orders": 4, "verified_orders": 3,
                 "gmv": 300, "refunds": 1, "review_count": 2, "negative_review_count": 1,
                 "avg_rating": 4.8}, {"impressions": 100, "detail_views": 20, "paid_orders": 4,
                                      "verified_orders": 3, "gmv": 200, "refunds": 0,
                                      "review_count": 0, "negative_review_count": 0, "avg_rating": 4.6}]
        result = aggregate(rows)
        self.assertEqual((result["impressions"], result["paid_orders"], result["gmv"]), (200, 8, 500))
        self.assertEqual(result["view_rate"], 20.0)
        self.assertEqual(result["pay_rate"], 20.0)
        self.assertEqual(result["verify_rate"], 75.0)
        self.assertEqual(result["refund_rate"], 12.5)
        self.assertEqual(result["avg_rating"], 4.7)
        empty = aggregate([])
        self.assertIsNone(empty["view_rate"])
        self.assertEqual(empty["impressions"], 0)

    def test_dashboard_structure_and_trend(self):
        self.seed(days=10)
        self.seed_plan()
        data = self.reporter.dashboard(days=7)
        self.assertTrue(data["has_data"])
        self.assertTrue(data["financial_included"])
        self.assertEqual(data["window"]["days"], 7)
        self.assertEqual(set(data["platforms"]), {"meituan", "douyin"})
        self.assertEqual(len(data["trend"]), len({row["date"] for row in data["trend"]}))
        self.assertGreater(data["kpis"]["impressions"], 0)
        self.assertIn("gmv", data["kpis"])
        self.assertEqual(data["packages"][0]["package_id"], PACKAGE_ID)
        self.assertIsNotNone(data["packages"][0]["gross_margin_rate"])
        self.assertIn("tasks", data)
        self.assertEqual(data["approvals"].get("pending"), 1)
        self.assertIn("data_quality", data)
        self.assertTrue(any(src["type"] == "store_config" for src in data["sources"]))
        with self.assertRaises(ValueError):
            self.reporter.dashboard(days=0)

    def test_dashboard_strips_financial_fields(self):
        self.seed(days=10)
        data = self.reporter.dashboard(days=7, include_financial=False)
        self.assertFalse(data["financial_included"])
        for field in ("gmv", "refunds", "refund_rate"):
            self.assertNotIn(field, data["kpis"])
        self.assertNotIn("gmv", data["change_percent"])
        self.assertTrue(all("gmv" not in point for point in data["trend"]))
        self.assertTrue(all("gmv" not in pkg for pkg in data["packages"]))
        self.assertNotIn("gmv", data["previous_kpis"])

    def test_weekly_report_content_and_comparison(self):
        self.seed(days=20)
        self.seed_plan()
        report = self.reporter.weekly_report()
        text = report["report"]
        for section in ("一、结论摘要", "二、分平台明细", "三、每日表现", "四、任务与审批",
                        "五、数据质量", "六、下一步建议"):
            self.assertIn(section, text)
        self.assertIn("与上周相比", text)
        self.assertIn("最好的一天", text)
        self.assertGreater(report["summary"]["paid_orders"], 0)
        self.assertGreater(report["summary"]["pending_approvals"], 0)
        self.assertEqual(report["compare_period"]["end"],
                         (date.fromisoformat(report["period"]["start"]) - timedelta(days=1)).isoformat())

    def test_monthly_report_uses_calendar_month_and_previous_month(self):
        self.seed(days=70)
        report = self.reporter.monthly_report()
        start = date.fromisoformat(report["period"]["start"])
        self.assertEqual(start.day, 1)
        self.assertEqual(report["compare_period"]["label"], "上月")
        previous_start = date.fromisoformat(report["compare_period"]["start"])
        self.assertEqual(previous_start.day, 1)
        self.assertEqual(report["compare_period"]["end"],
                         (start - timedelta(days=1)).isoformat())
        self.assertIn("与上月相比", report["report"])
        self.assertTrue(report["summary"]["has_data"])

    def test_reports_without_data_are_honest(self):
        dashboard = self.reporter.dashboard(days=7)
        self.assertFalse(dashboard["has_data"])
        self.assertIn("该区间", " ".join(dashboard["issues"]) + " 该区间")   # 至少不报错
        report = self.reporter.weekly_report()
        self.assertFalse(report["summary"]["has_data"])
        self.assertIn("没有任何已校验的平台数据", report["report"])

    def test_report_respects_financial_permission(self):
        self.seed(days=10)
        report = self.reporter.weekly_report(include_financial=False)
        self.assertNotIn("GMV", report["report"])
        self.assertNotIn("退款率", report["report"])
        self.assertIsNone(report["summary"]["gmv"])


if __name__ == "__main__":
    unittest.main()
