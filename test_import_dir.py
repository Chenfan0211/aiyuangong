# -*- coding: utf-8 -*-
"""批次 2：多数据集识别、批量导入、幂等与容错的测试。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from platform_data import (
    DATASET_PACKAGE,
    DATASET_REVIEW,
    DATASET_STORE,
    DataValidationError,
    PlatformMetricStore,
)

ROOT = Path(__file__).resolve().parent
IMPORTS = ROOT / "imports"

STORE_HEADER = (
    "date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
    "gmv,refunds,review_count,negative_review_count,avg_rating\n"
)
PACKAGE_HEADER = (
    "date,platform,store_id,package_id,package_name,detail_views,paid_orders,"
    "verified_orders,gmv\n"
)
REVIEW_HEADER = "review_id,platform,store_id,rating,created_at,status,category,replied_at\n"
STORE_ID = "WH-LION-001"


class DatasetDetectionTests(unittest.TestCase):
    def test_template_headers_are_recognized(self):
        self.assertIsNone(PlatformMetricStore.detect_dataset(["date", "platform", "store_id"]))
        store_cols = STORE_HEADER.strip().split(",")
        package_cols = PACKAGE_HEADER.strip().split(",")
        review_cols = REVIEW_HEADER.strip().split(",")
        self.assertEqual(PlatformMetricStore.detect_dataset(store_cols), DATASET_STORE)
        self.assertEqual(PlatformMetricStore.detect_dataset(package_cols), DATASET_PACKAGE)
        self.assertEqual(PlatformMetricStore.detect_dataset(review_cols), DATASET_REVIEW)
        self.assertIsNone(PlatformMetricStore.detect_dataset(["a", "b"]))
        self.assertIsNone(PlatformMetricStore.detect_dataset([]))

    def test_shipped_templates_match_the_importer(self):
        """仓库里的模板必须能被 import-dir 识别，防止模板与解析器脱节。"""
        for filename, expected in (
            ("平台数据导入模板.csv", DATASET_STORE),
            ("套餐明细导入模板.csv", DATASET_PACKAGE),
            ("评价明细导入模板.csv", DATASET_REVIEW),
        ):
            header = PlatformMetricStore._read_header(IMPORTS / filename)
            self.assertEqual(PlatformMetricStore.detect_dataset(header), expected, filename)


class BatchImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.store = PlatformMetricStore(self.root / "metrics.db")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, header, body, folder=None):
        path = (folder or self.inbox) / name
        path.write_bytes((header + body).encode("utf-8"))
        return path

    def pending_reviews(self):
        with self.store._connection() as conn:
            return [dict(row) for row in conn.execute("select * from reviews order by review_id")]

    def test_package_import_is_idempotent(self):
        path = self.write("pkg.csv", PACKAGE_HEADER,
                          "2026-08-31,meituan,WH-LION-001,MT-1,AI全周小包2h,168,5,4,1200\n")
        first = self.store.import_packages_csv(path, STORE_ID)
        second = self.store.import_packages_csv(path, STORE_ID)
        self.assertEqual(first.rows_written, 1)
        self.assertEqual(first.dataset, DATASET_PACKAGE)
        self.assertEqual(second.rows_written, 0)
        self.assertEqual(second.dataset, DATASET_PACKAGE)
        with self.store._connection() as conn:
            row = conn.execute("select * from package_metrics").fetchone()
        self.assertEqual((row["package_id"], row["package_name"], row["detail_views"]), ("MT-1", "AI全周小包2h", 168))

    def test_package_requires_package_id(self):
        path = self.write("pkg.csv", PACKAGE_HEADER, "2026-08-31,meituan,WH-LION-001,,名字,168,5,4,1200\n")
        with self.assertRaises(DataValidationError):
            self.store.import_packages_csv(path, STORE_ID)

    def test_review_import_then_status_update_by_new_batch(self):
        first = self.write("rev1.csv", REVIEW_HEADER,
                           "R1,meituan,WH-LION-001,2,2026-09-03 09:15:00,open,服务体验,\n")
        result = self.store.import_reviews_csv(first, STORE_ID)
        self.assertEqual((result.rows_written, result.dataset), (1, DATASET_REVIEW))
        self.assertEqual(result.min_date, "2026-09-03")
        self.assertEqual(self.pending_reviews()[0]["status"], "open")

        # 内容不同 => 新批次，应更新同一条评价（不是新增）
        second = self.write("rev2.csv", REVIEW_HEADER,
                            "R1,meituan,WH-LION-001,2,2026-09-03 09:15:00,replied,服务体验,2026-09-03 10:00:00\n")
        self.store.import_reviews_csv(second, STORE_ID)
        rows = self.pending_reviews()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "replied")
        self.assertEqual(rows[0]["replied_at"], "2026-09-03T10:00:00")

    def test_review_validation(self):
        cases = {
            "bad_status.csv": "R1,meituan,WH-LION-001,4,2026-09-03 09:15:00,已回复,\n",
            "bad_rating.csv": "R1,meituan,WH-LION-001,5.1,2026-09-03 09:15:00,open,\n",
            "bad_time.csv": "R1,meituan,WH-LION-001,4,2026年9月3日,open,\n",
            "bad_store.csv": "R1,meituan,OTHER-001,4,2026-09-03 09:15:00,open,\n",
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                path = self.write(name, REVIEW_HEADER, body)
                with self.assertRaises(DataValidationError):
                    self.store.import_reviews_csv(path, STORE_ID)

    def test_import_directory_continues_after_bad_files(self):
        self.write("01_store.csv", STORE_HEADER,
                   "2026-09-01,meituan,WH-LION-001,100,20,4,3,300,0,1,0,4.8\n")
        self.write("02_package.csv", PACKAGE_HEADER,
                   "2026-09-01,meituan,WH-LION-001,MT-1,AI全周小包2h,168,5,4,1200\n")
        self.write("03_review.csv", REVIEW_HEADER,
                   "R1,douyin,WH-LION-001,2,2026-09-01 09:15:00,open,服务体验,\n")
        self.write("04_unknown.csv", "foo,bar\n", "1,2\n")
        self.write("05_broken.csv", STORE_HEADER, "2026-09-01,meituan,WH-LION-001,坏数据,20,4,3,300,0,1,0,4.8\n")
        (self.inbox / "ignore.txt").write_text("not a csv", encoding="utf-8")

        summary = self.store.import_directory(self.inbox, STORE_ID)
        self.assertEqual(summary["file_count"], 5)
        self.assertEqual(summary["success_count"], 3)
        self.assertEqual(summary["failed_count"], 2)
        statuses = {item["file"]: item["status"] for item in summary["files"]}
        self.assertEqual(statuses["01_store.csv"], "success")
        self.assertEqual(statuses["04_unknown.csv"], "failed")
        self.assertEqual(statuses["05_broken.csv"], "failed")
        # 前两个成功文件必须真的入库
        self.assertTrue(self.store.status(STORE_ID)["has_data"])
        self.assertEqual(len(self.pending_reviews()), 1)

    def test_import_directory_is_idempotent_and_missing_dir_raises(self):
        self.write("store.csv", STORE_HEADER, "2026-09-01,meituan,WH-LION-001,100,20,4,3,300,0,1,0,4.8\n")
        first = self.store.import_directory(self.inbox, STORE_ID)
        second = self.store.import_directory(self.inbox, STORE_ID)
        self.assertEqual((first["success_count"], first["skipped_count"]), (1, 0))
        self.assertEqual((second["success_count"], second["skipped_count"]), (0, 1))
        with self.assertRaises(DataValidationError):
            self.store.import_directory(self.root / "not-exist", STORE_ID)

    def test_legacy_database_is_migrated(self):
        """改造前创建的库必须能直接打开：补 dataset 列、建新表。"""
        legacy_path = self.root / "legacy.db"
        legacy = sqlite3.connect(str(legacy_path))
        legacy.executescript(
            """
            create table platform_metrics (
              store_id text not null, metric_date text not null, platform text not null,
              impressions integer, detail_views integer, paid_orders integer, verified_orders integer,
              gmv real, refunds integer, review_count integer, negative_review_count integer,
              avg_rating real, source_type text not null, source_name text not null,
              imported_at text not null, primary key (store_id, metric_date, platform)
            );
            create table metric_import_batches (
              id integer primary key autoincrement, store_id text not null, platform text not null,
              source_name text not null, checksum text not null, rows_read integer not null,
              rows_written integer not null, min_date text not null, max_date text not null,
              status text not null, imported_at text not null
            );
            create unique index idx_metric_batch_checksum on metric_import_batches(store_id, checksum);
            """
        )
        legacy.commit()
        legacy.close()

        store = PlatformMetricStore(legacy_path)
        with store._connection() as conn:
            batch_columns = {row["name"] for row in conn.execute("pragma table_info(metric_import_batches)")}
            tables = {row["name"] for row in conn.execute("select name from sqlite_master where type='table'")}
        self.assertIn("dataset", batch_columns)
        self.assertTrue({"package_metrics", "reviews"} <= tables)

        path = self.write("store.csv", STORE_HEADER, "2026-09-01,meituan,WH-LION-001,100,20,4,3,300,0,1,0,4.8\n",
                          folder=self.root)
        store.import_csv(path, STORE_ID)
        with store._connection() as conn:
            self.assertEqual(conn.execute("select dataset from metric_import_batches").fetchone()["dataset"], DATASET_STORE)


if __name__ == "__main__":
    unittest.main()
