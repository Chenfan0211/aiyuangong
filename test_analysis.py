# -*- coding: utf-8 -*-
"""周期对比与经营归因（analysis.StoreAnalysis）测试。

只用临时 SQLite 造数，不依赖仓库 data/*.db（当前为空）。
"""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from analysis import DIAGNOSIS_RULE_VERSION, RULE_VERSION, StoreAnalysis
from platform_data import PlatformMetricStore
from snapshot_builder import load_config

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
          "gmv,refunds,review_count,negative_review_count,avg_rating\n")
METRIC_ORDER = ("impressions", "detail_views", "paid_orders", "verified_orders", "gmv",
                "refunds", "review_count", "negative_review_count", "avg_rating")
FINANCIAL_KEYS = ("gmv", "refunds", "refund_rate")


class StoreAnalysisTests(unittest.TestCase):
    """周期对比 + 归因规则的合同测试。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        base = load_config(ROOT / "store_config.json")
        self.config = {**base, "rules": {**base.get("rules", {}), "fresh_days": 400}}
        self.metrics = PlatformMetricStore(self.root / "metrics.db", tenant_id=TENANT)
        self.analysis = self.build()

    def tearDown(self):
        self.temp.cleanup()

    # ---------------- 工具 ----------------

    def build(self, fresh_days=400):
        """按指定新鲜度阈值构造 StoreAnalysis（默认放宽，避免测试被 stale 干扰）。"""
        config = {**self.config, "rules": {**self.config["rules"], "fresh_days": fresh_days}}
        return StoreAnalysis(config, self.metrics, config_path=ROOT / "store_config.json")

    def day(self, days_ago):
        return (date.today() - timedelta(days=days_ago)).isoformat()

    def row(self, days_ago, impressions=100, views=20, paid=4, verified=3, gmv=300,
            refunds=0, reviews=2, negatives=0, rating=4.8):
        return {"date": self.day(days_ago), "impressions": impressions, "detail_views": views,
                "paid_orders": paid, "verified_orders": verified, "gmv": gmv, "refunds": refunds,
                "review_count": reviews, "negative_review_count": negatives, "avg_rating": rating}

    def seed(self, platform, records, name=None):
        lines = []
        for record in records:
            values = ["" if record.get(metric) is None else str(record[metric]) for metric in METRIC_ORDER]
            lines.append(",".join([record["date"], platform, STORE, *values]) + "\n")
        path = self.root / (name or f"{platform}.csv")
        path.write_text(HEADER + "".join(lines), encoding="utf-8")
        return self.metrics.import_csv(path, STORE)

    def codes(self, causes):
        return [cause["code"] for cause in causes]

    def financial_paths(self, payload, prefix=""):
        """递归找出所有财务字段路径：include_financial=False 时必须是空列表。"""
        found = []
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in FINANCIAL_KEYS:
                    found.append(f"{prefix}.{key}")
                found.extend(self.financial_paths(value, f"{prefix}.{key}"))
        elif isinstance(payload, list):
            for index, value in enumerate(payload):
                found.extend(self.financial_paths(value, f"{prefix}[{index}]"))
        return found

    # ---------------- 周期对比 ----------------

    def test_day_window_yesterday_vs_previous_day(self):
        self.seed("meituan", [self.row(3), self.row(2), self.row(1, impressions=150, views=30,
                                                                    paid=6, verified=5, gmv=450)])
        result = self.analysis.compare(anchor="yesterday", windows=("day",))
        window = result["windows"]["day"]
        self.assertEqual(result["store_id"], STORE)
        self.assertEqual(result["anchor"]["mode"], "explicit")
        self.assertEqual(window["current"], {"start": self.day(1), "end": self.day(1)})
        self.assertEqual(window["previous"], {"start": self.day(2), "end": self.day(2)})
        self.assertEqual(window["metrics"]["impressions"], 150)
        self.assertEqual(window["previous_metrics"]["impressions"], 100)
        self.assertEqual(window["change_percent"]["impressions"], 50.0)
        self.assertEqual(window["absolute_change"]["impressions"], 50)
        self.assertEqual(window["coverage"]["summary_platforms"], ["meituan"])
        self.assertTrue(window["coverage"]["summary_complete"])
        self.assertEqual([item for item in window["blocked"] if item["platform"] == "meituan"], [])

    def test_week_window_last7_vs_previous7_with_auto_anchor(self):
        self.seed("meituan", [self.row(offset) for offset in range(15, 0, -1)])
        result = self.analysis.compare(windows=("week",))
        window = result["windows"]["week"]
        self.assertEqual(result["anchor"]["mode"], "auto")
        self.assertEqual(result["anchor"]["common"], self.day(1))
        self.assertEqual(window["current"], {"start": self.day(7), "end": self.day(1)})
        self.assertEqual(window["previous"], {"start": self.day(14), "end": self.day(8)})
        self.assertEqual(window["metrics"]["impressions"], 700)
        self.assertEqual(window["previous_metrics"]["impressions"], 700)
        self.assertEqual(window["change_percent"]["impressions"], 0.0)
        self.assertEqual(window["coverage"]["summary_platforms"], ["meituan"])

    def test_today_anchor_blocks_missing_today(self):
        self.seed("meituan", [self.row(2), self.row(1)])
        result = self.analysis.compare(anchor="today", windows=("day",))
        window = result["windows"]["day"]
        self.assertEqual(window["current"], {"start": self.day(0), "end": self.day(0)})
        self.assertIsNone(window["metrics"])
        self.assertEqual(window["missing_dates"]["current"]["meituan"], [self.day(0)])
        self.assertIn("missing_days", self.codes(window["blocked"]))

    def test_platform_scope_breakdown_and_not_connected(self):
        self.seed("meituan", [self.row(offset) for offset in (1, 2, 3)])
        partial = self.analysis.compare(windows=("day",))
        self.assertEqual(partial["windows"]["day"]["coverage"]["summary_platforms"], ["meituan"])
        blocked = [item for item in partial["windows"]["day"]["blocked"] if item["platform"] == "douyin"]
        self.assertEqual([item["code"] for item in blocked], ["not_connected"])

        self.seed("douyin", [self.row(offset, impressions=80) for offset in (1, 2, 3)])
        full = self.analysis.compare(windows=("day",))
        day = full["windows"]["day"]
        self.assertEqual(sorted(day["platforms"]), ["douyin", "meituan"])
        self.assertEqual(sorted(day["coverage"]["summary_platforms"]), ["douyin", "meituan"])
        self.assertEqual(day["platforms"]["douyin"]["metrics"]["impressions"], 80)
        self.assertEqual(day["metrics"]["impressions"], 180)

        only = self.analysis.compare(windows=("day",), platform="美团")
        self.assertEqual(list(only["windows"]["day"]["platforms"]), ["meituan"])
        self.assertEqual(only["windows"]["day"]["metrics"]["impressions"], 100)

    def test_auto_anchor_differs_per_platform_and_summary_needs_common_window(self):
        self.seed("meituan", [self.row(offset) for offset in (1, 2, 3)])
        self.seed("douyin", [self.row(offset, impressions=80) for offset in (2, 3, 4)])
        result = self.analysis.compare(windows=("day",))
        self.assertEqual(result["anchor"]["platforms"]["meituan"], self.day(1))
        self.assertEqual(result["anchor"]["platforms"]["douyin"], self.day(2))
        self.assertEqual(result["anchor"]["common"], self.day(1))
        day = result["windows"]["day"]
        self.assertEqual(day["metrics"]["impressions"], 100)
        self.assertEqual(day["coverage"]["summary_platforms"], ["meituan"])
        self.assertEqual(day["missing_dates_summary"]["current"]["douyin"], [self.day(1)])
        self.assertEqual(day["platforms"]["douyin"]["current"], {"start": self.day(2), "end": self.day(2)})
        self.assertIn("missing_days", [item["code"] for item in day["blocked"] if item["platform"] == "douyin"])

    def test_missing_day_blocks_that_window_only(self):
        records = [self.row(offset) for offset in range(14, 0, -1) if offset != 10]
        self.seed("meituan", records)
        result = self.analysis.compare(windows=("day", "week"))
        week = result["windows"]["week"]
        self.assertEqual(week["missing_dates"]["previous"]["meituan"], [self.day(10)])
        self.assertEqual(week["coverage"]["summary_platforms"], [])
        self.assertIsNone(week["metrics"])
        self.assertIn("missing_days", [item["code"] for item in week["blocked"] if item["platform"] == "meituan"])
        day = result["windows"]["day"]
        self.assertEqual(day["coverage"]["summary_platforms"], ["meituan"])
        self.assertEqual(day["metrics"]["impressions"], 100)

    def test_stale_data_is_blocked_but_numbers_still_returned(self):
        self.seed("meituan", [self.row(offset) for offset in (60, 61, 62)])
        analysis = self.build(fresh_days=2)
        result = analysis.compare(windows=("day",))
        window = result["windows"]["day"]
        self.assertEqual(window["platforms"]["meituan"]["metrics"]["impressions"], 100)
        self.assertEqual(window["metrics"]["impressions"], 100)
        self.assertIn("stale", [item["code"] for item in window["blocked"] if item["platform"] == "meituan"])
        diagnosis = analysis.diagnose(result)
        self.assertEqual(diagnosis["causes"][0]["code"], "data_quality")
        self.assertTrue(all(cause["confidence"] != "high" for cause in diagnosis["causes"]))

    def test_previous_zero_keeps_absolute_change(self):
        self.seed("meituan", [self.row(2, impressions=0, views=0, paid=0, verified=0, gmv=0, refunds=0),
                              self.row(1, impressions=50, views=10, paid=2, verified=1, gmv=100)])
        window = self.analysis.compare(anchor="yesterday", windows=("day",))["windows"]["day"]
        self.assertEqual(window["previous_metrics"]["impressions"], 0)
        self.assertIsNone(window["change_percent"]["impressions"])
        self.assertEqual(window["absolute_change"]["impressions"], 50)

    def test_include_financial_false_removes_financial_fields_everywhere(self):
        self.seed("meituan", [self.row(2, refunds=1), self.row(1, refunds=2)])
        kept = self.analysis.compare(windows=("day", "week"), include_financial=True)
        self.assertIn("gmv", kept["windows"]["day"]["metrics"])
        self.assertIn("refund_rate", kept["windows"]["day"]["metrics"])
        stripped = self.analysis.compare(windows=("day", "week"), include_financial=False)
        self.assertEqual(self.financial_paths(stripped), [])
        day = stripped["windows"]["day"]
        for payload in (day["metrics"], day["previous_metrics"], day["change_percent"], day["absolute_change"],
                        day["platforms"]["meituan"]["metrics"],
                        day["platforms"]["meituan"]["change_percent"]):
            for key in FINANCIAL_KEYS:
                self.assertNotIn(key, payload)
        self.assertFalse(stripped["include_financial"])

    def test_metric_filter_and_invalid_arguments(self):
        self.seed("meituan", [self.row(2), self.row(1)])
        window = self.analysis.compare(windows=("day",), metric="impressions")["windows"]["day"]
        self.assertEqual(list(window["metrics"]), ["impressions"])
        self.assertEqual(list(window["previous_metrics"]), ["impressions"])
        self.assertEqual(list(window["change_percent"]), ["impressions"])
        self.assertEqual(list(window["absolute_change"]), ["impressions"])
        with self.assertRaises(ValueError):
            self.analysis.compare(metric="not_a_metric")
        with self.assertRaises(ValueError):
            self.analysis.compare(platform="taobao")
        with self.assertRaises(ValueError):
            self.analysis.compare(windows=("month",))

    def test_sources_cover_dates_batches_and_rule_version(self):
        self.seed("meituan", [self.row(o) for o in (1, 2, 3, 4)])
        result = self.analysis.compare(windows=("day", "week"))
        types = {item["type"] for item in result["sources"]}
        self.assertEqual(types, {"window", "metric_batch", "rule", "store_config"})
        batch = next(item for item in result["sources"] if item["type"] == "metric_batch")
        self.assertEqual(batch["source_name"], "meituan.csv")
        self.assertEqual(batch["date_range"], {"start": self.day(4), "end": self.day(1)})
        self.assertTrue(batch["label"].startswith("导入批次 #"))
        rule = next(item for item in result["sources"] if item["type"] == "rule")
        self.assertEqual(rule["version"], RULE_VERSION)
        window_source = next(item for item in result["sources"] if item["type"] == "window")
        self.assertEqual(window_source["date_range"]["current"], {"start": self.day(1), "end": self.day(1)})
        self.assertEqual(window_source["date_range"]["previous"], {"start": self.day(2), "end": self.day(2)})

    def test_no_data_returns_explicit_missing_instead_of_numbers(self):
        result = self.analysis.compare(windows=("day", "week"))
        for window in result["windows"].values():
            self.assertIsNone(window["metrics"])
            self.assertIsNone(window["previous_metrics"])
            self.assertEqual(window["change_percent"], {})
            self.assertEqual(window["absolute_change"], {})
            self.assertEqual(window["coverage"]["summary_platforms"], [])
        self.assertIn("no_data", {item["type"] for item in result["sources"]})
        self.assertEqual({item["code"] for item in result["blocked"]}, {"not_connected", "no_complete_platform"})
        diagnosis = self.analysis.diagnose(result)
        self.assertIn("data_quality", self.codes(diagnosis["causes"]))
        self.assertIn("insufficient_evidence", self.codes(diagnosis["causes"]))
        self.assertEqual(diagnosis["causes"][-1]["confidence"], "low")

    # ---------------- 经营归因 ----------------

    def diagnose_day(self, previous_row, current_row, **kwargs):
        self.seed("meituan", [previous_row, current_row])
        comparison = self.analysis.compare(anchor="yesterday", windows=("day",), platform="meituan")
        return self.analysis.diagnose(comparison)

    def assert_rule(self, diagnosis, code, metric=None):
        codes = self.codes(diagnosis["causes"])
        self.assertIn(code, codes)
        cause = diagnosis["causes"][codes.index(code)]
        self.assertIn("可能", cause["label"])
        self.assertIn(cause["confidence"], ("low", "medium", "high"))
        self.assertTrue(cause["actions"])
        if metric:
            self.evidence_of(cause, metric)
        return cause

    def evidence_of(self, cause, metric):
        for item in cause["evidence"]:
            if item.get("metric") == metric:
                return item
        raise AssertionError(f"{cause['code']} 缺少 {metric} 证据：{cause['evidence']}")

    def test_diagnose_traffic_drop(self):
        diagnosis = self.diagnose_day(
            self.row(2, impressions=1000, views=200, paid=40, verified=36),
            self.row(1, impressions=600, views=100, paid=20, verified=18))
        cause = self.assert_rule(diagnosis, "traffic", "impressions")
        evidence = self.evidence_of(cause, "impressions")
        self.assertEqual(evidence["current"], 600)
        self.assertEqual(evidence["previous"], 1000)
        self.assertEqual(evidence["change_percent"], -40.0)
        self.assertEqual(evidence["current_range"], {"start": self.day(1), "end": self.day(1)})
        self.assertEqual(evidence["previous_range"], {"start": self.day(2), "end": self.day(2)})
        self.assertEqual(cause["confidence"], "high")
        self.assertNotIn("verify_rate", [item.get("metric") for item in cause["evidence"]])

    def test_diagnose_pay_conversion_drop(self):
        diagnosis = self.diagnose_day(
            self.row(2, impressions=1000, views=200, paid=40, verified=36),
            self.row(1, impressions=1000, views=200, paid=20, verified=18))
        cause = self.assert_rule(diagnosis, "pay_conversion", "pay_rate")
        evidence = self.evidence_of(cause, "pay_rate")
        self.assertEqual(evidence["current"], 10.0)
        self.assertEqual(evidence["previous"], 20.0)
        self.assertEqual(evidence["absolute_change"], -10.0)

    def test_diagnose_fulfillment_drop(self):
        diagnosis = self.diagnose_day(
            self.row(2, impressions=1000, views=200, paid=40, verified=36),
            self.row(1, impressions=1000, views=200, paid=40, verified=30))
        cause = self.assert_rule(diagnosis, "fulfillment", "verify_rate")
        evidence = self.evidence_of(cause, "verify_rate")
        self.assertEqual(evidence["current"], 75.0)
        self.assertEqual(evidence["previous"], 90.0)
        self.assertEqual(evidence["absolute_change"], -15.0)

    def test_diagnose_refund_up(self):
        diagnosis = self.diagnose_day(
            self.row(2, impressions=1000, views=200, paid=40, verified=36, refunds=2),
            self.row(1, impressions=1000, views=200, paid=40, verified=36, refunds=6))
        cause = self.assert_rule(diagnosis, "refund", "refund_rate")
        evidence = self.evidence_of(cause, "refund_rate")
        self.assertEqual(evidence["current"], 15.0)
        self.assertEqual(evidence["previous"], 5.0)
        self.assertEqual(evidence["absolute_change"], 10.0)
        self.assertTrue([item for item in cause["evidence"] if item.get("metric") == "refunds"])

    def test_diagnose_reputation_drop(self):
        diagnosis = self.diagnose_day(
            self.row(2, impressions=1000, views=200, paid=40, verified=36,
                     reviews=10, negatives=1, rating=4.8),
            self.row(1, impressions=1000, views=200, paid=40, verified=36,
                     reviews=10, negatives=4, rating=4.2))
        cause = self.assert_rule(diagnosis, "reputation", "negative_rate")
        evidence = self.evidence_of(cause, "negative_rate")
        self.assertEqual(evidence["current"], 40.0)
        self.assertEqual(evidence["previous"], 10.0)
        rating = self.evidence_of(cause, "avg_rating")
        self.assertEqual(rating["current"], 4.2)
        self.assertEqual(rating["previous"], 4.8)

    def test_diagnose_stable(self):
        diagnosis = self.diagnose_day(
            self.row(2, impressions=1000, views=200, paid=40, verified=36),
            self.row(1, impressions=1020, views=204, paid=41, verified=37))
        codes = self.codes(diagnosis["causes"])
        self.assertIn("stable", codes)
        for code in ("traffic", "pay_conversion", "fulfillment", "refund", "reputation"):
            self.assertNotIn(code, codes)

    def test_diagnose_growth(self):
        diagnosis = self.diagnose_day(
            self.row(2, impressions=1000, views=200, paid=40, verified=36),
            self.row(1, impressions=1600, views=320, paid=64, verified=58))
        codes = self.codes(diagnosis["causes"])
        self.assertIn("growth", codes)
        self.assertNotIn("traffic", codes)

    def test_diagnose_wraps_comparison_kwargs_and_reports_rule_version(self):
        self.seed("meituan", [self.row(2), self.row(1)])
        diagnosis = self.analysis.diagnose(anchor="yesterday", windows=("day",))
        self.assertEqual(diagnosis["rule_version"], DIAGNOSIS_RULE_VERSION)
        self.assertEqual(diagnosis["store_id"], STORE)
        self.assertTrue(diagnosis["disclaimer"])
        rule = next(item for item in diagnosis["sources"] if item.get("rule_key") == "store_diagnosis")
        self.assertEqual(rule["version"], DIAGNOSIS_RULE_VERSION)

    def test_output_is_json_serializable(self):
        """对比与归因结果要能直接进 API / 消息回复，全部字段可 JSON 序列化。"""
        self.seed("meituan", [self.row(2), self.row(1)])
        comparison = self.analysis.compare(windows=("day", "week"))
        json.dumps(comparison, ensure_ascii=False)
        json.dumps(self.analysis.diagnose(comparison), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()