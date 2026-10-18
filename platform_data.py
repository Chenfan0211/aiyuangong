#!/usr/bin/env python3
"""门店平台经营数据的统一指标库。

先支持官方报表 CSV 导入；拿到美团/抖音正式授权后，连接器也写入同一张表。
所有经营结论只能来自已落库数据，不用演示快照补齐缺失值。
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from db import Connection, Database


PLATFORMS = {"meituan": "美团", "douyin": "抖音"}
METRICS = (
    "impressions",
    "detail_views",
    "paid_orders",
    "verified_orders",
    "gmv",
    "refunds",
    "review_count",
    "negative_review_count",
    "avg_rating",
)
REQUIRED_COLUMNS = ("date", "platform", "store_id", *METRICS)
COUNT_METRICS = set(METRICS) - {"gmv", "avg_rating"}

DATASET_STORE = "store_daily"
DATASET_PACKAGE = "package"
DATASET_REVIEW = "review"
DATASET_LABELS = {
    DATASET_STORE: "平台日指标",
    DATASET_PACKAGE: "套餐明细",
    DATASET_REVIEW: "评价明细",
}
PACKAGE_METRICS = ("detail_views", "paid_orders", "verified_orders", "gmv")
PACKAGE_REQUIRED_COLUMNS = ("date", "platform", "store_id", "package_id", "package_name", *PACKAGE_METRICS)
REVIEW_REQUIRED_COLUMNS = ("review_id", "platform", "store_id", "rating", "created_at")
REVIEW_STATUSES = ("open", "replied", "resolved")


class DataValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ImportResult:
    batch_id: int
    platform: str
    rows_read: int
    rows_written: int
    min_date: str
    max_date: str
    checksum: str
    dataset: str = DATASET_STORE


class PlatformMetricStore:
    def __init__(self, db_target: Any, tenant_id: str = ""):
        """`db_target` 可以是 SQLite 文件路径，也可以是 postgresql:// DSN（批次 8b 双引擎）。"""
        self.db = Database(db_target, tenant_id=tenant_id)
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connection(self):
        """统一走 Database：提交/回滚后必定关闭连接（详见 db.py）。"""
        return self.db.connection()

    def _init_schema(self) -> None:
        if self.db.dialect == "postgresql":
            with self._connection() as conn:
                self.db.apply_schema(conn)
            return
        with self._connection() as conn:
            conn.executescript(
                """
                create table if not exists platform_metrics (
  tenant_id text not null default '',
                  store_id text not null,
                  metric_date text not null,
                  platform text not null check (platform in ('meituan','douyin')),
                  impressions integer,
                  detail_views integer,
                  paid_orders integer,
                  verified_orders integer,
                  gmv real,
                  refunds integer,
                  review_count integer,
                  negative_review_count integer,
                  avg_rating real,
                  source_type text not null,
                  source_name text not null,
                  imported_at text not null,
                  primary key (store_id, metric_date, platform)
                );
                create table if not exists metric_import_batches (
  tenant_id text not null default '',
                  id integer primary key autoincrement,
                  store_id text not null,
                  platform text not null,
                  source_name text not null,
                  checksum text not null,
                  rows_read integer not null,
                  rows_written integer not null,
                  min_date text not null,
                  max_date text not null,
                  status text not null,
                  imported_at text not null
                );
                create unique index if not exists idx_metric_batch_checksum
                  on metric_import_batches(store_id, checksum);

                create table if not exists package_metrics (
  tenant_id text not null default '',
                  store_id text not null,
                  metric_date text not null,
                  platform text not null check (platform in ('meituan','douyin')),
                  package_id text not null,
                  package_name text not null,
                  detail_views integer,
                  paid_orders integer,
                  verified_orders integer,
                  gmv real,
                  source_name text not null,
                  imported_at text not null,
                  primary key (store_id, metric_date, platform, package_id)
                );
                create index if not exists idx_package_metrics_store_date
                  on package_metrics(store_id, metric_date, platform);

                create table if not exists reviews (
  tenant_id text not null default '',
                  store_id text not null,
                  platform text not null check (platform in ('meituan','douyin')),
                  review_id text not null,
                  rating real,
                  created_at text not null,
                  status text not null default 'open' check (status in ('open','replied','resolved')),
                  category text,
                  replied_at text,
                  source_name text not null,
                  imported_at text not null,
                  primary key (store_id, platform, review_id)
                );
                create index if not exists idx_reviews_store_status
                  on reviews(store_id, status, created_at);
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """幂等迁移：SQLite 旧库补列与租户唯一索引（PostgreSQL 侧由 schema.sql 保证）。"""
        with self._connection() as conn:
            self.db.add_column_if_missing(
                conn, "metric_import_batches", "dataset",
                "dataset text not null default 'store_daily'",
            )
            self.db.ensure_tenant_column(
                conn, "platform_metrics",
                ("ux_platform_metrics_runtime", ("tenant_id", "store_id", "metric_date", "platform")))
            self.db.ensure_tenant_column(
                conn, "metric_import_batches",
                ("ux_metric_batch_runtime", ("tenant_id", "store_id", "checksum")))
            self.db.ensure_tenant_column(
                conn, "package_metrics",
                ("ux_package_metrics_runtime", ("tenant_id", "store_id", "metric_date", "platform", "package_id")))
            self.db.ensure_tenant_column(
                conn, "reviews", ("ux_reviews_runtime", ("tenant_id", "store_id", "platform", "review_id")))

    @staticmethod
    def _parse_date(raw: str, row_number: int) -> str:
        try:
            return date.fromisoformat(raw.strip()).isoformat()
        except ValueError as exc:
            raise DataValidationError(f"第 {row_number} 行 date 必须是 YYYY-MM-DD") from exc

    @staticmethod
    def _parse_number(raw: str, metric: str, row_number: int) -> int | float | None:
        value = raw.strip()
        if value == "":
            return None
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise DataValidationError(f"第 {row_number} 行 {metric} 不是数字") from exc
        if number < 0:
            raise DataValidationError(f"第 {row_number} 行 {metric} 不能为负数")
        if metric in COUNT_METRICS:
            if number != number.to_integral_value():
                raise DataValidationError(f"第 {row_number} 行 {metric} 必须是整数")
            return int(number)
        if metric == "avg_rating" and number > 5:
            raise DataValidationError(f"第 {row_number} 行 avg_rating 不能超过 5")
        return float(number)

    def import_csv(self, path: Path, expected_store_id: str) -> ImportResult:
        raw_bytes = path.read_bytes()
        checksum = hashlib.sha256(raw_bytes).hexdigest()
        with self._connection() as conn:
            prior = conn.execute(
                "select * from metric_import_batches where store_id=? and checksum=?",
                (expected_store_id, checksum),
            ).fetchone()
        if prior:
            return ImportResult(
                batch_id=prior["id"], platform=prior["platform"],
                rows_read=prior["rows_read"], rows_written=0,
                min_date=prior["min_date"], max_date=prior["max_date"], checksum=checksum,
            )

        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DataValidationError("CSV 必须是 UTF-8 编码") from exc
        reader = csv.DictReader(text.splitlines())
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise DataValidationError("CSV 缺少字段：" + "、".join(missing))

        rows: list[dict[str, Any]] = []
        platforms: set[str] = set()
        seen_keys: set[tuple[str, str]] = set()
        for row_number, raw in enumerate(reader, 2):
            metric_date = self._parse_date(raw["date"], row_number)
            platform = raw["platform"].strip().lower()
            store_id = raw["store_id"].strip()
            if platform not in PLATFORMS:
                raise DataValidationError(f"第 {row_number} 行 platform 只支持 meituan 或 douyin")
            if store_id != expected_store_id:
                raise DataValidationError(f"第 {row_number} 行门店编码不匹配：{store_id}")
            key = (metric_date, platform)
            if key in seen_keys:
                raise DataValidationError(f"CSV 内存在重复记录：{platform} {metric_date}")
            seen_keys.add(key)
            platforms.add(platform)
            parsed = {metric: self._parse_number(raw[metric], metric, row_number) for metric in METRICS}
            if all(value is None for value in parsed.values()):
                raise DataValidationError(f"第 {row_number} 行所有指标均为空")
            rows.append({"store_id": store_id, "metric_date": metric_date, "platform": platform, **parsed})
        if not rows:
            raise DataValidationError("CSV 没有数据行")
        if len(platforms) != 1:
            raise DataValidationError("一个导入文件只能包含一个平台")

        now = datetime.now().isoformat(timespec="seconds")
        source_name = path.name
        columns = ",".join(METRICS)
        placeholders = ",".join("?" for _ in METRICS)
        updates = ",".join(f"{metric}=excluded.{metric}" for metric in METRICS)
        platform = next(iter(platforms))
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    f"""insert into platform_metrics
                    (store_id,metric_date,platform,{columns},source_type,source_name,imported_at)
                    values (?,?,?,{placeholders},?,?,?)
                    on conflict(store_id,metric_date,platform) do update set
                    {updates},source_type=excluded.source_type,
                    source_name=excluded.source_name,imported_at=excluded.imported_at""",
                    (
                        row["store_id"], row["metric_date"], row["platform"],
                        *(row[metric] for metric in METRICS), "official_export", source_name, now,
                    ),
                )
            batch_id = self.db.insert_returning_id(
                conn,
                """insert into metric_import_batches
                (store_id,platform,source_name,checksum,rows_read,rows_written,min_date,max_date,status,imported_at)
                values (?,?,?,?,?,?,?,?,?,?)""",
                (
                    expected_store_id, platform, source_name, checksum, len(rows), len(rows),
                    min(row["metric_date"] for row in rows), max(row["metric_date"] for row in rows),
                    "success", now,
                ),
            )
        return ImportResult(
            batch_id=batch_id, platform=platform, rows_read=len(rows), rows_written=len(rows),
            min_date=min(row["metric_date"] for row in rows),
            max_date=max(row["metric_date"] for row in rows), checksum=checksum,
        )

    @staticmethod
    def _read_header(path: Path) -> list[str]:
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DataValidationError("CSV 必须是 UTF-8 编码") from exc
        rows = list(csv.reader(text.splitlines()))
        return [column.strip() for column in rows[0]] if rows else []

    @staticmethod
    def detect_dataset(fieldnames: Iterable[str] | None) -> str | None:
        """按表头识别数据集类型；无法识别时返回 None。"""
        columns = {str(name).strip() for name in (fieldnames or []) if str(name).strip()}
        if set(PACKAGE_REQUIRED_COLUMNS) <= columns:
            return DATASET_PACKAGE
        if set(REVIEW_REQUIRED_COLUMNS) <= columns:
            return DATASET_REVIEW
        if set(REQUIRED_COLUMNS) <= columns:
            return DATASET_STORE
        return None

    @staticmethod
    def _reader(path: Path, required: tuple[str, ...]) -> csv.DictReader:
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DataValidationError("CSV 必须是 UTF-8 编码") from exc
        reader = csv.DictReader(text.splitlines())
        missing = [column for column in required if column not in (reader.fieldnames or [])]
        if missing:
            raise DataValidationError("CSV 缺少字段：" + "、".join(missing))
        return reader

    @staticmethod
    def _parse_datetime(raw: str, field: str, row_number: int) -> str:
        try:
            return datetime.fromisoformat(raw.strip().replace(" ", "T")).isoformat(timespec="seconds")
        except ValueError as exc:
            raise DataValidationError(
                f"第 {row_number} 行 {field} 必须是 YYYY-MM-DD HH:MM:SS 或 ISO 时间"
            ) from exc

    def _find_batch(self, store_id: str, checksum: str) -> sqlite3.Row | None:
        with self._connection() as conn:
            return conn.execute(
                "select * from metric_import_batches where store_id=? and checksum=?",
                (store_id, checksum),
            ).fetchone()

    @staticmethod
    def _repeat_result(prior: sqlite3.Row, checksum: str) -> ImportResult:
        """同一文件重复导入：不重复写业务表，只回报既有批次。"""
        return ImportResult(
            batch_id=prior["id"],
            platform=prior["platform"],
            rows_read=prior["rows_read"],
            rows_written=0,
            min_date=prior["min_date"],
            max_date=prior["max_date"],
            checksum=checksum,
            dataset=prior["dataset"],
        )

    def _insert_batch(
        self,
        conn: Connection,
        store_id: str,
        platform: str,
        source_name: str,
        checksum: str,
        dates: list[str],
        dataset: str,
        now: str,
    ) -> int:
        return self.db.insert_returning_id(
            conn,
            """insert into metric_import_batches
            (store_id,platform,source_name,checksum,rows_read,rows_written,min_date,max_date,status,imported_at,dataset)
            values (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                store_id, platform, source_name, checksum, len(dates), len(dates),
                min(dates), max(dates), "success", now, dataset,
            ),
        )

    def import_packages_csv(self, path: Path, expected_store_id: str) -> ImportResult:
        """导入套餐/商品维度明细，按 (门店,日期,平台,套餐) 幂等更新。"""
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        prior = self._find_batch(expected_store_id, checksum)
        if prior:
            return self._repeat_result(prior, checksum)

        reader = self._reader(path, PACKAGE_REQUIRED_COLUMNS)
        rows: list[dict[str, Any]] = []
        platforms: set[str] = set()
        seen: set[tuple[str, str, str]] = set()
        for row_number, raw in enumerate(reader, 2):
            metric_date = self._parse_date(raw["date"], row_number)
            platform = raw["platform"].strip().lower()
            store_id = raw["store_id"].strip()
            package_id = raw["package_id"].strip()
            if platform not in PLATFORMS:
                raise DataValidationError(f"第 {row_number} 行 platform 只支持 meituan 或 douyin")
            if store_id != expected_store_id:
                raise DataValidationError(f"第 {row_number} 行门店编码不匹配：{store_id}")
            if not package_id:
                raise DataValidationError(f"第 {row_number} 行 package_id 不能为空")
            key = (metric_date, platform, package_id)
            if key in seen:
                raise DataValidationError(f"CSV 内存在重复记录：{platform} {metric_date} {package_id}")
            seen.add(key)
            platforms.add(platform)
            parsed = {metric: self._parse_number(raw[metric], metric, row_number) for metric in PACKAGE_METRICS}
            if all(value is None for value in parsed.values()):
                raise DataValidationError(f"第 {row_number} 行所有指标均为空")
            rows.append({
                "store_id": store_id, "metric_date": metric_date, "platform": platform,
                "package_id": package_id,
                "package_name": (raw.get("package_name") or "").strip() or package_id,
                **parsed,
            })
        if not rows:
            raise DataValidationError("CSV 没有数据行")
        if len(platforms) != 1:
            raise DataValidationError("一个导入文件只能包含一个平台")

        now = datetime.now().isoformat(timespec="seconds")
        platform = next(iter(platforms))
        columns = ",".join(PACKAGE_METRICS)
        placeholders = ",".join("?" for _ in PACKAGE_METRICS)
        updates = ",".join(f"{metric}=excluded.{metric}" for metric in PACKAGE_METRICS)
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    f"""insert into package_metrics
                    (store_id,metric_date,platform,package_id,package_name,{columns},source_name,imported_at)
                    values (?,?,?,?,?,{placeholders},?,?)
                    on conflict(store_id,metric_date,platform,package_id) do update set
                    {updates},package_name=excluded.package_name,
                    source_name=excluded.source_name,imported_at=excluded.imported_at""",
                    (
                        row["store_id"], row["metric_date"], row["platform"], row["package_id"],
                        row["package_name"], *(row[metric] for metric in PACKAGE_METRICS),
                        path.name, now,
                    ),
                )
            batch_id = self._insert_batch(
                conn, expected_store_id, platform, path.name, checksum,
                [row["metric_date"] for row in rows], DATASET_PACKAGE, now,
            )
        return ImportResult(
            batch_id=batch_id, platform=platform, rows_read=len(rows), rows_written=len(rows),
            min_date=min(row["metric_date"] for row in rows),
            max_date=max(row["metric_date"] for row in rows),
            checksum=checksum, dataset=DATASET_PACKAGE,
        )

    def import_reviews_csv(self, path: Path, expected_store_id: str) -> ImportResult:
        """导入单条评价明细（差评 SLA 任务的数据前提）。"""
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        prior = self._find_batch(expected_store_id, checksum)
        if prior:
            return self._repeat_result(prior, checksum)

        reader = self._reader(path, REVIEW_REQUIRED_COLUMNS)
        rows: list[dict[str, Any]] = []
        platforms: set[str] = set()
        seen: set[str] = set()
        for row_number, raw in enumerate(reader, 2):
            platform = raw["platform"].strip().lower()
            store_id = raw["store_id"].strip()
            review_id = raw["review_id"].strip()
            if platform not in PLATFORMS:
                raise DataValidationError(f"第 {row_number} 行 platform 只支持 meituan 或 douyin")
            if store_id != expected_store_id:
                raise DataValidationError(f"第 {row_number} 行门店编码不匹配：{store_id}")
            if not review_id:
                raise DataValidationError(f"第 {row_number} 行 review_id 不能为空")
            if review_id in seen:
                raise DataValidationError(f"CSV 内存在重复记录：review_id {review_id}")
            seen.add(review_id)
            status = (raw.get("status") or "").strip().lower() or "open"
            if status not in REVIEW_STATUSES:
                raise DataValidationError(
                    f"第 {row_number} 行 status 只支持 {'/'.join(REVIEW_STATUSES)}"
                )
            replied_at = (raw.get("replied_at") or "").strip()
            platforms.add(platform)
            rows.append({
                "store_id": store_id, "platform": platform, "review_id": review_id,
                "rating": self._parse_number(raw["rating"], "avg_rating", row_number),
                "created_at": self._parse_datetime(raw["created_at"], "created_at", row_number),
                "status": status,
                "category": (raw.get("category") or "").strip() or None,
                "replied_at": self._parse_datetime(replied_at, "replied_at", row_number) if replied_at else None,
            })
        if not rows:
            raise DataValidationError("CSV 没有数据行")
        if len(platforms) != 1:
            raise DataValidationError("一个导入文件只能包含一个平台")

        now = datetime.now().isoformat(timespec="seconds")
        platform = next(iter(platforms))
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    """insert into reviews
                    (store_id,platform,review_id,rating,created_at,status,category,replied_at,source_name,imported_at)
                    values (?,?,?,?,?,?,?,?,?,?)
                    on conflict(store_id,platform,review_id) do update set
                    rating=excluded.rating,created_at=excluded.created_at,status=excluded.status,
                    category=excluded.category,replied_at=excluded.replied_at,
                    source_name=excluded.source_name,imported_at=excluded.imported_at""",
                    (
                        row["store_id"], row["platform"], row["review_id"], row["rating"],
                        row["created_at"], row["status"], row["category"], row["replied_at"],
                        path.name, now,
                    ),
                )
            batch_id = self._insert_batch(
                conn, expected_store_id, platform, path.name, checksum,
                [row["created_at"][:10] for row in rows], DATASET_REVIEW, now,
            )
        return ImportResult(
            batch_id=batch_id, platform=platform, rows_read=len(rows), rows_written=len(rows),
            min_date=min(row["created_at"][:10] for row in rows),
            max_date=max(row["created_at"][:10] for row in rows),
            checksum=checksum, dataset=DATASET_REVIEW,
        )

    def import_directory(self, inbox: Path, expected_store_id: str) -> dict[str, Any]:
        """扫描目录批量导入：逐文件识别数据集，单个文件失败不影响其它文件。"""
        inbox = Path(inbox)
        if not inbox.is_dir():
            raise DataValidationError(f"导入目录不存在：{inbox}")
        files = sorted(
            item for item in inbox.iterdir()
            if item.is_file() and item.suffix.lower() == ".csv" and not item.name.startswith(".")
        )
        results: list[dict[str, Any]] = []
        for path in files:
            try:
                dataset = self.detect_dataset(self._read_header(path))
                if dataset is None:
                    raise DataValidationError(
                        "无法识别数据集：表头与《平台数据导入模板》《套餐明细导入模板》《评价明细导入模板》都不匹配"
                    )
                if dataset == DATASET_PACKAGE:
                    result = self.import_packages_csv(path, expected_store_id)
                elif dataset == DATASET_REVIEW:
                    result = self.import_reviews_csv(path, expected_store_id)
                else:
                    result = self.import_csv(path, expected_store_id)
                results.append({
                    "file": path.name,
                    "dataset": dataset,
                    "dataset_label": DATASET_LABELS[dataset],
                    "status": "success" if result.rows_written else "skipped",
                    "rows_read": result.rows_read,
                    "rows_written": result.rows_written,
                    "min_date": result.min_date,
                    "max_date": result.max_date,
                    "error": None,
                })
            except (DataValidationError, ValueError, OSError) as exc:
                results.append({
                    "file": path.name,
                    "dataset": None,
                    "dataset_label": None,
                    "status": "failed",
                    "rows_read": 0,
                    "rows_written": 0,
                    "min_date": None,
                    "max_date": None,
                    "error": str(exc),
                })
        return {
            "inbox": str(inbox),
            "store_id": expected_store_id,
            "file_count": len(files),
            "success_count": sum(item["status"] == "success" for item in results),
            "skipped_count": sum(item["status"] == "skipped" for item in results),
            "failed_count": sum(item["status"] == "failed" for item in results),
            "files": results,
        }

    def read_daily_metrics(self, store_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """读取区间内的平台日指标原始行。"""
        with self._connection() as conn:
            rows = conn.execute(
                """select * from platform_metrics where store_id=? and metric_date between ? and ?
                order by metric_date, platform""",
                (store_id, start_date, end_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_package_metrics(self, store_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """读取区间内的套餐维度明细原始行。"""
        with self._connection() as conn:
            rows = conn.execute(
                """select * from package_metrics where store_id=? and metric_date between ? and ?
                order by metric_date, platform, package_id""",
                (store_id, start_date, end_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_reviews(
        self,
        store_id: str,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取评价明细；since / until 按 created_at 的日期部分过滤。"""
        sql = "select * from reviews where store_id=?"
        params: list[Any] = [store_id]
        if since:
            # cast 到文本再截取：SQLite 存 ISO 字符串，PostgreSQL 存 timestamptz，两边都适用
            sql += " and substr(cast(created_at as text),1,10) >= ?"
            params.append(since)
        if until:
            sql += " and substr(cast(created_at as text),1,10) <= ?"
            params.append(until)
        sql += " order by created_at, review_id"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def status(self, store_id: str, fresh_days: int = 2) -> dict[str, Any]:
        """平台数据新鲜度；age_days <= fresh_days 视为 fresh。"""
        with self._connection() as conn:
            rows = conn.execute(
                """select platform, min(metric_date) min_date, max(metric_date) max_date,
                count(*) day_count, max(imported_at) imported_at
                from platform_metrics where store_id=? group by platform order by platform""",
                (store_id,),
            ).fetchall()
        by_platform: dict[str, Any] = {}
        today = date.today()
        for platform in PLATFORMS:
            match = next((row for row in rows if row["platform"] == platform), None)
            if not match:
                by_platform[platform] = {
                    "label": PLATFORMS[platform], "state": "not_connected", "day_count": 0,
                    "min_date": None, "max_date": None, "age_days": None, "imported_at": None,
                }
                continue
            latest = date.fromisoformat(match["max_date"])
            age_days = (today - latest).days
            state = "fresh" if age_days <= fresh_days else "stale"
            by_platform[platform] = {
                "label": PLATFORMS[platform], "state": state, "day_count": match["day_count"],
                "min_date": match["min_date"], "max_date": match["max_date"],
                "age_days": age_days, "imported_at": match["imported_at"],
            }
        return {"store_id": store_id, "platforms": by_platform, "has_data": bool(rows)}

    @staticmethod
    def _sum(rows: Iterable[sqlite3.Row], metric: str) -> float | int | None:
        values = [row[metric] for row in rows if row[metric] is not None]
        return sum(values) if values else None

    def period_summary(self, store_id: str, days: int = 7) -> dict[str, Any]:
        if days < 1 or days > 90:
            raise ValueError("统计周期必须在 1 到 90 天之间")
        status = self.status(store_id)
        latest_dates = [
            value["max_date"] for value in status["platforms"].values() if value["max_date"]
        ]
        if not latest_dates:
            return {"store_id": store_id, "days": days, "has_data": False, "platforms": {}}
        end_date = max(date.fromisoformat(value) for value in latest_dates)
        start_date = end_date - timedelta(days=days - 1)
        previous_start = start_date - timedelta(days=days)
        previous_end = start_date - timedelta(days=1)
        with self._connection() as conn:
            rows = conn.execute(
                """select * from platform_metrics where store_id=? and metric_date between ? and ?
                order by platform,metric_date""",
                (store_id, previous_start.isoformat(), end_date.isoformat()),
            ).fetchall()
        result: dict[str, Any] = {}
        for platform in PLATFORMS:
            current = [row for row in rows if row["platform"] == platform and row["metric_date"] >= start_date.isoformat()]
            previous = [row for row in rows if row["platform"] == platform and row["metric_date"] <= previous_end.isoformat()]
            if not current:
                continue
            totals = {metric: self._sum(current, metric) for metric in METRICS if metric != "avg_rating"}
            ratings = [row["avg_rating"] for row in current if row["avg_rating"] is not None]
            totals["avg_rating"] = round(sum(ratings) / len(ratings), 2) if ratings else None
            previous_totals = {metric: self._sum(previous, metric) for metric in ("impressions", "detail_views", "paid_orders", "verified_orders", "gmv")}
            changes = {}
            for metric, value in previous_totals.items():
                current_value = totals[metric]
                changes[metric] = (
                    round((current_value - value) / value * 100, 1)
                    if current_value is not None and value not in (None, 0) else None
                )
            totals["view_rate"] = (
                round(totals["detail_views"] / totals["impressions"] * 100, 2)
                if totals["detail_views"] is not None and totals["impressions"] else None
            )
            totals["pay_rate"] = (
                round(totals["paid_orders"] / totals["detail_views"] * 100, 2)
                if totals["paid_orders"] is not None and totals["detail_views"] else None
            )
            totals["verify_rate"] = (
                round(totals["verified_orders"] / totals["paid_orders"] * 100, 2)
                if totals["verified_orders"] is not None and totals["paid_orders"] else None
            )
            totals["refund_rate"] = (
                round(totals["refunds"] / totals["paid_orders"] * 100, 2)
                if totals["refunds"] is not None and totals["paid_orders"] else None
            )
            totals["negative_rate"] = (
                round(totals["negative_review_count"] / totals["review_count"] * 100, 2)
                if totals["negative_review_count"] is not None and totals["review_count"] else None
            )
            result[platform] = {
                "label": PLATFORMS[platform], "row_count": len(current),
                "metrics": totals, "change_percent": changes,
                "latest_date": max(row["metric_date"] for row in current),
                "source_names": sorted({row["source_name"] for row in current}),
            }
        return {
            "store_id": store_id, "days": days, "has_data": bool(result),
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            "platforms": result,
        }

    ANOMALY_METRICS = ("impressions", "detail_views", "paid_orders", "verified_orders", "gmv", "refunds")

    def data_quality(
        self,
        store_id: str,
        days: int = 30,
        fresh_days: int = 2,
        jump_alert_percent: float = 30.0,
        batch_limit: int = 20,
    ) -> dict[str, Any]:
        """数据质量体检：缺日检测、日环比跳变、批次审计与新鲜度。

        缺日口径：窗口终点取"全店最新数据日期"，每个平台从"该平台首条数据日期"检查到窗口终点。
        因此平台上线前的日期不会被误报为缺数，但中间的断档与末尾停更会被检出（停更同时体现在 freshness）。
        跳变口径：同一平台相邻两条数据的日环比，绝对值 >= jump_alert_percent 即告警；
        只检查漏斗与金额类指标，回避评价条数/评分这类小样本指标的正常噪声。
        """
        if days < 1 or days > 365:
            raise ValueError("体检周期必须在 1 到 365 天之间")
        if fresh_days < 0:
            raise ValueError("fresh_days 不能为负数")
        if jump_alert_percent < 0:
            raise ValueError("jump_alert_percent 不能为负数")

        freshness = self.status(store_id, fresh_days=fresh_days)
        with self._connection() as conn:
            rows = conn.execute(
                "select * from platform_metrics where store_id=? order by platform, metric_date",
                (store_id,),
            ).fetchall()
            batches = conn.execute(
                """select id,dataset,platform,source_name,checksum,rows_read,rows_written,
                min_date,max_date,status,imported_at
                from metric_import_batches where store_id=? order by id desc limit ?""",
                (store_id, max(1, min(batch_limit, 200))),
            ).fetchall()

        batch_items = [
            {
                "id": row["id"],
                "dataset": row["dataset"],
                "dataset_label": DATASET_LABELS.get(row["dataset"], row["dataset"]),
                "platform": row["platform"],
                "source_name": row["source_name"],
                "checksum_prefix": str(row["checksum"])[:12],
                "rows_read": row["rows_read"],
                "rows_written": row["rows_written"],
                "min_date": row["min_date"],
                "max_date": row["max_date"],
                "status": row["status"],
                "imported_at": row["imported_at"],
            }
            for row in batches
        ]

        latest_overall = max((date.fromisoformat(row["metric_date"]) for row in rows), default=None)
        window_start = latest_overall - timedelta(days=days - 1) if latest_overall else None
        coverage: dict[str, Any] = {}
        anomalies: list[dict[str, Any]] = []

        for platform in PLATFORMS:
            platform_rows = [
                row for row in rows
                if row["platform"] == platform
                and (window_start is None or date.fromisoformat(row["metric_date"]) >= window_start)
            ]
            item: dict[str, Any] = {
                "expected_days": 0,
                "actual_days": len(platform_rows),
                "missing_dates": [],
                "complete": None,
                "checked_from": None,
                "note": None,
            }
            if not platform_rows:
                item["complete"] = False
                item["note"] = "窗口内没有任何数据，无法判断缺日（见 freshness）"
                coverage[platform] = item
                continue

            present = {row["metric_date"] for row in platform_rows}
            first_day = min(date.fromisoformat(row["metric_date"]) for row in platform_rows)
            checked_from = max(first_day, window_start)
            expected = [
                (checked_from + timedelta(days=offset)).isoformat()
                for offset in range((latest_overall - checked_from).days + 1)
            ]
            item["checked_from"] = checked_from.isoformat()
            item["expected_days"] = len(expected)
            item["missing_dates"] = [value for value in expected if value not in present]
            item["complete"] = not item["missing_dates"]
            coverage[platform] = item

            ordered = sorted(platform_rows, key=lambda row: row["metric_date"])
            for previous, current in zip(ordered, ordered[1:]):
                for metric in self.ANOMALY_METRICS:
                    current_value = current[metric]
                    previous_value = previous[metric]
                    if current_value is None or previous_value in (None, 0):
                        continue
                    change = round((current_value - previous_value) / previous_value * 100, 1)
                    if abs(change) >= jump_alert_percent:
                        anomalies.append({
                            "platform": platform,
                            "platform_label": PLATFORMS[platform],
                            "metric": metric,
                            "date": current["metric_date"],
                            "previous_date": previous["metric_date"],
                            "value": current_value,
                            "previous_value": previous_value,
                            "change_percent": change,
                        })

        issues: list[str] = []
        if not rows:
            issues.append("该门店还没有任何平台数据：请把导出的报表放进 imports/inbox/ 后执行 import-dir")
        for platform, item in coverage.items():
            label = PLATFORMS[platform]
            if item["missing_dates"]:
                preview = "、".join(item["missing_dates"][:5])
                suffix = f" 等共 {len(item['missing_dates'])} 天" if len(item["missing_dates"]) > 5 else ""
                issues.append(f"{label} 缺 {len(item['missing_dates'])} 天数据：{preview}{suffix}")
            elif item["note"]:
                issues.append(f"{label}：{item['note']}")
        for key, item in freshness["platforms"].items():
            if item["state"] == "stale":
                issues.append(
                    f"{item['label']} 数据已 {item['age_days']} 天未更新（超过 {fresh_days} 天），请重新导出并导入"
                )
            elif item["state"] == "not_connected":
                issues.append(f"{item['label']} 尚未接入任何数据")
        if anomalies:
            issues.append(
                f"发现 {len(anomalies)} 处日环比跳变（阈值 ±{jump_alert_percent}%），"
                "请先确认是真实波动还是导入错误"
            )

        return {
            "store_id": store_id,
            "days": days,
            "evaluated_at": datetime.now().isoformat(timespec="seconds"),
            "parameters": {
                "fresh_days": fresh_days,
                "jump_alert_percent": jump_alert_percent,
                "checked_metrics": list(self.ANOMALY_METRICS),
            },
            "window": {
                "start_date": window_start.isoformat() if window_start else None,
                "end_date": latest_overall.isoformat() if latest_overall else None,
                "basis": "窗口终点取全店最新数据日期；各平台缺日检查从该平台首条数据日期开始",
            },
            "freshness": freshness["platforms"],
            "coverage": coverage,
            "anomalies": anomalies,
            "batches": batch_items,
            "summary": {
                "has_data": bool(rows),
                "missing_day_count": sum(len(item["missing_dates"]) for item in coverage.values()),
                "anomaly_count": len(anomalies),
                "stale_platforms": [k for k, v in freshness["platforms"].items() if v["state"] == "stale"],
                "batch_count": len(batch_items),
                "issues": issues,
            },
        }
