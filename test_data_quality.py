# -*- coding: utf-8 -*-
"""批次 4：数据质量体检（缺日 / 跳变 / 批次审计 / 新鲜度 / 派生指标）的测试。"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from platform_data import PlatformMetricStore

STORE_ID = "WH-LION-001"
HEADER = (
    "date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
    "gmv,refunds,review_count,negative_review_count,avg_rating\n"
)


def row(day, platform="meituan", impressions=100, views=20, paid=4, verified=3,
        gmv=300, refunds=0, reviews=1, negatives=0, rating=4.8):
    return (f"{day},{platform},{STORE_ID},{impressions},{views},{paid},{verified},"
            f"{gmv},{refunds},{reviews},{negatives},{rating}\n")


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.store = PlatformMetricStore(self.root / "metrics.db")

    def tearDown(self):
        self.temp.cleanup()

    def load(self, name, body):
        """写入 inbox 并立刻导入（返回文件路径）。"""
        path = self.inbox / name
        path.write_bytes((HEADER + body).encode("utf-8"))
        self.store.import_csv(path, STORE_ID)
        return path

    def quality(self, **kwargs):
        return self.store.data_quality(STORE_ID, **kwargs)

    def test_missing_day_in_the_middle_is_detected(self):
        self.load("m.csv", row("2026-09-01") + row("2026-09-02") + row("2026-09-04"))
        report = self.quality(days=30)
        self.assertEqual(report["coverage"]["meituan"]["missing_dates"], ["2026-09-03"])
        self.assertEqual(report["coverage"]["meituan"]["expected_days"], 4)
        self.assertEqual(report["coverage"]["meituan"]["actual_days"], 3)
        self.assertFalse(report["coverage"]["meituan"]["complete"])
        self.assertEqual(report["summary"]["missing_day_count"], 1)
        self.assertTrue(any("缺 1 天数据" in item for item in report["summary"]["issues"]))

    def test_missing_days_are_reported_for_the_slower_platform(self):
        self.load("m.csv", row("2026-09-01") + row("2026-09-02"))
        self.load("d.csv", "".join(row(f"2026-09-0{d}", platform="douyin") for d in (1, 2, 3, 4, 5)))
        report = self.quality(days=30)
        self.assertEqual(report["window"]["end_date"], "2026-09-05")
        self.assertEqual(report["coverage"]["meituan"]["checked_from"], "2026-09-01")
        self.assertEqual(
            report["coverage"]["meituan"]["missing_dates"],
            ["2026-09-03", "2026-09-04", "2026-09-05"],
        )
        self.assertTrue(report["coverage"]["douyin"]["complete"])

    def test_empty_store_reports_actionable_issue(self):
        report = self.quality()
        self.assertFalse(report["summary"]["has_data"])
        self.assertEqual(report["summary"]["missing_day_count"], 0)
        self.assertIsNone(report["window"]["end_date"])
        self.assertTrue(any("还没有任何平台数据" in item for item in report["summary"]["issues"]))
        self.assertIn("窗口内没有任何数据", report["coverage"]["meituan"]["note"])

    def test_day_over_day_jump_respects_threshold(self):
        self.load("m.csv", row("2026-09-01", impressions=100) + row("2026-09-02", impressions=300))
        report = self.quality(jump_alert_percent=30.0)
        jumps = [item for item in report["anomalies"] if item["metric"] == "impressions"]
        self.assertEqual(len(jumps), 1)
        self.assertEqual(jumps[0]["change_percent"], 200.0)
        self.assertEqual(jumps[0]["previous_date"], "2026-09-01")
        self.assertEqual(report["summary"]["anomaly_count"], len(report["anomalies"]))
        self.assertEqual(self.quality(jump_alert_percent=250.0)["anomalies"], [])

    def test_jump_skips_unknown_and_zero_baseline(self):
        self.load("m.csv", row("2026-09-01", gmv=0) + row("2026-09-02", gmv=900))
        report = self.quality(jump_alert_percent=30.0)
        self.assertEqual([item for item in report["anomalies"] if item["metric"] == "gmv"], [])

    def test_batch_audit_lists_datasets_and_checksum_prefix(self):
        self.load("m.csv", row("2026-09-01"))
        pkg = self.inbox / "p.csv"
        pkg.write_bytes((
            "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
            f"2026-09-01,meituan,{STORE_ID},MT-1,小包,100,5,4,1200\n"
        ).encode("utf-8"))
        self.store.import_packages_csv(pkg, STORE_ID)
        report = self.quality(batch_limit=1)
        self.assertEqual(report["summary"]["batch_count"], 1)
        batch = report["batches"][0]
        self.assertEqual(len(batch["checksum_prefix"]), 12)
        self.assertEqual(batch["dataset"], "package")
        self.assertEqual(batch["dataset_label"], "套餐明细")
        self.assertEqual(batch["rows_written"], 1)

    def test_freshness_threshold_is_configurable(self):
        recent = (date.today() - timedelta(days=10)).isoformat()
        self.load("m.csv", row(recent))
        self.assertEqual(self.quality(fresh_days=2)["freshness"]["meituan"]["state"], "stale")
        self.assertEqual(self.quality(fresh_days=30)["freshness"]["meituan"]["state"], "fresh")
        self.assertEqual(self.quality(fresh_days=2)["summary"]["stale_platforms"], ["meituan"])

    def test_derived_rates_are_reported(self):
        self.load("m.csv", row("2026-09-01", paid=5, refunds=1, reviews=4, negatives=1))
        metrics = self.store.period_summary(STORE_ID, days=7)["platforms"]["meituan"]["metrics"]
        self.assertEqual(metrics["refund_rate"], 20.0)
        self.assertEqual(metrics["negative_rate"], 25.0)

    def test_derived_rates_are_none_without_denominator(self):
        self.load("m.csv", row("2026-09-01", paid=0, refunds=0, reviews=0, negatives=0))
        metrics = self.store.period_summary(STORE_ID, days=7)["platforms"]["meituan"]["metrics"]
        self.assertIsNone(metrics["refund_rate"])
        self.assertIsNone(metrics["negative_rate"])

    def test_invalid_parameters_raise(self):
        for kwargs in ({"days": 0}, {"days": 400}, {"fresh_days": -1}, {"jump_alert_percent": -1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.quality(**kwargs)


if __name__ == "__main__":
    unittest.main()
