import tempfile
import unittest
from pathlib import Path

from platform_data import DataValidationError, PlatformMetricStore


HEADER = "date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,gmv,refunds,review_count,negative_review_count,avg_rating\n"


class PlatformMetricStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = PlatformMetricStore(self.root / "metrics.db")

    def tearDown(self):
        self.temp.cleanup()

    def write_csv(self, body: str, name: str = "report.csv") -> Path:
        path = self.root / name
        path.write_text(HEADER + body, encoding="utf-8")
        return path

    def test_import_and_summary(self):
        path = self.write_csv(
            "2026-08-19,meituan,WH-LION-001,100,20,4,3,300,0,1,0,4.8\n"
            "2026-08-26,meituan,WH-LION-001,120,24,5,4,400,1,2,1,4.7\n"
        )
        result = self.store.import_csv(path, "WH-LION-001")
        self.assertEqual(result.rows_written, 2)
        summary = self.store.period_summary("WH-LION-001", 7)
        self.assertEqual(summary["platforms"]["meituan"]["metrics"]["impressions"], 120)
        self.assertEqual(summary["platforms"]["meituan"]["change_percent"]["impressions"], 20.0)

    def test_same_file_is_idempotent(self):
        path = self.write_csv("2026-08-26,douyin,WH-LION-001,100,20,4,3,300,0,1,0,4.8\n")
        first = self.store.import_csv(path, "WH-LION-001")
        second = self.store.import_csv(path, "WH-LION-001")
        self.assertEqual(first.batch_id, second.batch_id)
        self.assertEqual(second.rows_written, 0)

    def test_rejects_wrong_store(self):
        path = self.write_csv("2026-08-26,douyin,OTHER,100,20,4,3,300,0,1,0,4.8\n")
        with self.assertRaises(DataValidationError):
            self.store.import_csv(path, "WH-LION-001")

    def test_rejects_invalid_rating(self):
        path = self.write_csv("2026-08-26,douyin,WH-LION-001,100,20,4,3,300,0,1,0,5.1\n")
        with self.assertRaises(DataValidationError):
            self.store.import_csv(path, "WH-LION-001")


if __name__ == "__main__":
    unittest.main()
