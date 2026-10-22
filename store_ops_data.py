#!/usr/bin/env python3
"""门店侧经营数据（开台/客流、时段、库存、商圈）的统一指标库。

与 `platform_data.py` 的分工：
- `platform_data.py` 管**平台侧**数据（美团/抖音的曝光、访问、支付、核销、评价）。
- 本模块管**门店侧**数据（自己店里的开台、客流、时段、库存、商圈）。

两者的共同约定（保持一致，便于统一排查）：
- 数据来源只有一种：人工导出 CSV → `import-dir` 导入。不做自动采集。
- 按文件 SHA-256 批次哈希去重，重复导入 `rows_written = 0`。
- 门店编码必须与 `--store-id` 一致；一个文件只描述一个门店。
- 所有经营结论只能来自已落库数据，缺数时明确说缺数，不补齐、不编造。

批次账本为门店侧库内的 `ops_import_batches`（与平台侧的 `metric_import_batches` 分开），
因为两个库本来就是分开的：平台指标在 `platform_metrics.db`，门店侧数据在 `store_ops.db`。
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
from platform_data import (
    DATASET_LABELS as PLATFORM_DATASET_LABELS,
    DATASET_PACKAGE,
    DATASET_REVIEW,
    DATASET_STORE,
    DataValidationError,
    ImportResult,
    PlatformMetricStore,
)


# ImportResult.platform 的取值：门店侧数据没有平台维度，统一记为 store
OPS_PLATFORM = "store"

DATASET_TRAFFIC = "ops_traffic"
DATASET_TIMESLOT = "ops_timeslot"
DATASET_INVENTORY = "ops_inventory"
DATASET_TRADE_AREA = "ops_trade_area"

OPS_DATASET_LABELS = {
    DATASET_TRAFFIC: "门店开台与客流",
    DATASET_TIMESLOT: "门店时段分布",
    DATASET_INVENTORY: "门店库存与物料",
    DATASET_TRADE_AREA: "门店商圈",
}

# 开台与客流
TRAFFIC_METRICS = ("tables_opened", "table_capacity", "covers", "revenue")
TRAFFIC_REQUIRED_COLUMNS = ("date", "store_id", *TRAFFIC_METRICS)

# 时段分布
TIMESLOT_METRICS = ("covers", "orders", "revenue")
TIMESLOT_REQUIRED_COLUMNS = ("date", "store_id", "slot", *TIMESLOT_METRICS)

# 库存与物料
INVENTORY_METRICS = ("quantity", "safety_stock")
INVENTORY_REQUIRED_COLUMNS = ("date", "store_id", "item_id", "name", "unit", *INVENTORY_METRICS)

# 商圈（维度表，没有自然日期，批次日期取导入日）
TRADE_AREA_REQUIRED_COLUMNS = ("store_id", "area_id", "area_name")

OPS_COUNT_METRICS = frozenset({"tables_opened", "table_capacity", "covers", "orders"})
# 库存预警分级：剩余量 / 安全库存
INVENTORY_CRITICAL_RATIO = 0.5
INVENTORY_WARNING_RATIO = 1.0
ALERT_LEVELS = ("warning", "critical")
ALERT_STATUSES = ("open", "acknowledged", "resolved")
SLOT_MAX_LENGTH = 40


@dataclass(frozen=True)
class OpsSummary:
    """门店侧数据的一次汇总结果（用于看板与规则输入）。"""

    store_id: str
    days: int
    has_data: bool
    traffic: dict[str, Any]
    time_slots: list[dict[str, Any]]
    inventory: dict[str, Any]
    alerts: list[dict[str, Any]]


class StoreOpsDataStore:
    """门店侧数据的导入与查询入口（SQLite / PostgreSQL 双引擎）。"""

    def __init__(self, db_target: Any, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connection(self) -> AbstractContextManager[Connection]:
        return self.db.connection()

    def _init_schema(self) -> None:
        if self.db.dialect == "postgresql":
            with self._connection() as conn:
                self.db.apply_schema(conn)
            return
        with self._connection() as conn:
            conn.executescript(
                """
                create table if not exists ops_import_batches (
  tenant_id text not null default '',
                  id integer primary key autoincrement,
                  store_id text not null,
                  source_name text not null,
                  checksum text not null,
                  rows_read integer not null,
                  rows_written integer not null,
                  min_date text not null,
                  max_date text not null,
                  status text not null,
                  imported_at text not null,
                  dataset text not null
                );
                create unique index if not exists idx_ops_batch_checksum
                  on ops_import_batches(store_id, checksum);

                create table if not exists store_traffic_daily (
  tenant_id text not null default '',
                  store_id text not null,
                  metric_date text not null,
                  tables_opened integer,
                  table_capacity integer,
                  covers integer,
                  revenue real,
                  source_name text not null,
                  imported_at text not null,
                  primary key (store_id, metric_date)
                );
                create index if not exists idx_store_traffic_date
                  on store_traffic_daily(store_id, metric_date);

                create table if not exists time_slot_metrics (
  tenant_id text not null default '',
                  store_id text not null,
                  metric_date text not null,
                  slot text not null,
                  covers integer,
                  orders integer,
                  revenue real,
                  source_name text not null,
                  imported_at text not null,
                  primary key (store_id, metric_date, slot)
                );
                create index if not exists idx_time_slot_date
                  on time_slot_metrics(store_id, metric_date, slot);

                create table if not exists inventory_items (
  tenant_id text not null default '',
                  store_id text not null,
                  metric_date text not null,
                  item_id text not null,
                  name text not null,
                  unit text not null,
                  quantity real,
                  safety_stock real,
                  source_name text not null,
                  imported_at text not null,
                  primary key (store_id, metric_date, item_id)
                );

                create table if not exists inventory_alerts (
  tenant_id text not null default '',
                  store_id text not null,
                  metric_date text not null,
                  item_id text not null,
                  name text not null,
                  unit text not null,
                  quantity real,
                  safety_stock real,
                  level text not null check (level in ('warning','critical')),
                  status text not null default 'open' check (status in ('open','acknowledged','resolved')),
                  created_at text not null,
                  primary key (store_id, metric_date, item_id)
                );
                create index if not exists idx_inventory_alerts_open
                  on inventory_alerts(store_id, status, metric_date);

                create table if not exists trade_areas (
  tenant_id text not null default '',
                  store_id text not null,
                  area_id text not null,
                  area_name text not null,
                  area_type text,
                  note text,
                  source_name text not null,
                  imported_at text not null,
                  primary key (store_id, area_id)
                );
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """幂等迁移：SQLite 旧库补租户列与唯一索引（PostgreSQL 侧由 schema.sql 保证）。"""
        with self._connection() as conn:
            self.db.ensure_tenant_column(
                conn, "ops_import_batches",
                ("ux_ops_batch_runtime", ("tenant_id", "store_id", "checksum")))
            self.db.ensure_tenant_column(
                conn, "store_traffic_daily",
                ("ux_store_traffic_runtime", ("tenant_id", "store_id", "metric_date")))
            self.db.ensure_tenant_column(
                conn, "time_slot_metrics",
                ("ux_time_slot_runtime", ("tenant_id", "store_id", "metric_date", "slot")))
            self.db.ensure_tenant_column(
                conn, "inventory_items",
                ("ux_inventory_items_runtime", ("tenant_id", "store_id", "metric_date", "item_id")))
            self.db.ensure_tenant_column(
                conn, "inventory_alerts",
                ("ux_inventory_alerts_runtime", ("tenant_id", "store_id", "metric_date", "item_id")))
            self.db.ensure_tenant_column(
                conn, "trade_areas",
                ("ux_trade_areas_runtime", ("tenant_id", "store_id", "area_id")))

    # ---------------- 解析与校验 ----------------

    @staticmethod
    def _parse_date(raw: str, row_number: int) -> str:
        try:
            return date.fromisoformat((raw or "").strip()).isoformat()
        except ValueError as exc:
            raise DataValidationError(f"第 {row_number} 行 date 必须是 YYYY-MM-DD") from exc

    @staticmethod
    def _parse_number(raw: Any, metric: str, row_number: int) -> int | float | None:
        value = ("" if raw is None else str(raw)).strip()
        if value == "":
            return None
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise DataValidationError(f"第 {row_number} 行 {metric} 不是数字") from exc
        if number < 0:
            raise DataValidationError(f"第 {row_number} 行 {metric} 不能为负数")
        if metric in OPS_COUNT_METRICS:
            if number != number.to_integral_value():
                raise DataValidationError(f"第 {row_number} 行 {metric} 必须是整数")
            return int(number)
        return float(number)

    @staticmethod
    def _require_text(raw: Any, field: str, row_number: int, max_length: int = 200) -> str:
        value = ("" if raw is None else str(raw)).strip()
        if not value:
            raise DataValidationError(f"第 {row_number} 行 {field} 不能为空")
        if len(value) > max_length:
            raise DataValidationError(f"第 {row_number} 行 {field} 超过 {max_length} 个字符")
        return value

    @staticmethod
    def alert_level(quantity: float | None, safety_stock: float | None) -> str | None:
        """按剩余量 / 安全库存的比例分级；没有安全库存阈值时不预警。"""
        if quantity is None or safety_stock is None or safety_stock <= 0:
            return None
        ratio = quantity / safety_stock
        if ratio < INVENTORY_CRITICAL_RATIO:
            return "critical"
        if ratio < INVENTORY_WARNING_RATIO:
            return "warning"
        return None
    # ---------------- 批次账本（门店侧库内的 ops_import_batches，门店侧各数据集共用） ----------------

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
        """按表头识别门店侧数据集；无法识别时返回 None。"""
        columns = {str(name).strip() for name in (fieldnames or []) if str(name).strip()}
        if set(TIMESLOT_REQUIRED_COLUMNS) <= columns:
            return DATASET_TIMESLOT
        if set(INVENTORY_REQUIRED_COLUMNS) <= columns:
            return DATASET_INVENTORY
        if set(TRAFFIC_REQUIRED_COLUMNS) <= columns:
            return DATASET_TRAFFIC
        if set(TRADE_AREA_REQUIRED_COLUMNS) <= columns:
            return DATASET_TRADE_AREA
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

    def find_batch(self, store_id: str, checksum: str) -> Any:
        with self._connection() as conn:
            return conn.execute(
                "select * from ops_import_batches where store_id=? and checksum=?",
                (store_id, checksum),
            ).fetchone()

    @staticmethod
    def repeat_result(prior: Any, checksum: str) -> ImportResult:
        """同一文件重复导入：不重复写业务表，只回报既有批次。"""
        return ImportResult(
            batch_id=prior["id"],
            platform=OPS_PLATFORM,
            rows_read=prior["rows_read"],
            rows_written=0,
            min_date=prior["min_date"],
            max_date=prior["max_date"],
            checksum=checksum,
            dataset=prior["dataset"],
        )

    def record_batch(
        self, conn: Connection, store_id: str, source_name: str, checksum: str,
        dates: list[str], dataset: str, now: str,
    ) -> int:
        return self.db.insert_returning_id(
            conn,
            """insert into ops_import_batches
            (store_id,source_name,checksum,rows_read,rows_written,min_date,max_date,status,imported_at,dataset)
            values (?,?,?,?,?,?,?,?,?,?)""",
            (
                store_id, source_name, checksum, len(dates), len(dates),
                min(dates), max(dates), "success", now, dataset,
            ),
        )

    @staticmethod
    def _checksum(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _guarded_store(self, raw: Any, row_number: int, expected_store_id: str) -> str:
        store_id = ("" if raw is None else str(raw)).strip()
        if store_id != expected_store_id:
            raise DataValidationError(f"第 {row_number} 行门店编码不匹配：{store_id}")
        return store_id

    # ---------------- 四类数据集的导入 ----------------

    def import_traffic_csv(self, path: Path, expected_store_id: str) -> ImportResult:
        """导入开台与客流，按 (门店, 日期) 幂等更新。"""
        checksum = self._checksum(path)
        prior = self.find_batch(expected_store_id, checksum)
        if prior:
            return self.repeat_result(prior, checksum)

        reader = self._reader(path, TRAFFIC_REQUIRED_COLUMNS)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row_number, raw in enumerate(reader, 2):
            metric_date = self._parse_date(raw["date"], row_number)
            store_id = self._guarded_store(raw["store_id"], row_number, expected_store_id)
            if metric_date in seen:
                raise DataValidationError(f"CSV 内存在重复记录：{metric_date}")
            seen.add(metric_date)
            parsed = {metric: self._parse_number(raw[metric], metric, row_number) for metric in TRAFFIC_METRICS}
            if all(value is None for value in parsed.values()):
                raise DataValidationError(f"第 {row_number} 行所有指标均为空")
            opened, capacity = parsed["tables_opened"], parsed["table_capacity"]
            if opened is not None and capacity is not None and opened > capacity:
                raise DataValidationError(
                    f"第 {row_number} 行 tables_opened({opened}) 不能大于 table_capacity({capacity})"
                )
            rows.append({"store_id": store_id, "metric_date": metric_date, **parsed})
        if not rows:
            raise DataValidationError("CSV 没有数据行")

        now = datetime.now().isoformat(timespec="seconds")
        columns = ",".join(TRAFFIC_METRICS)
        placeholders = ",".join("?" for _ in TRAFFIC_METRICS)
        updates = ",".join(f"{metric}=excluded.{metric}" for metric in TRAFFIC_METRICS)
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    f"""insert into store_traffic_daily
                    (store_id,metric_date,{columns},source_name,imported_at)
                    values (?,?,{placeholders},?,?)
                    on conflict(store_id,metric_date) do update set
                    {updates},source_name=excluded.source_name,imported_at=excluded.imported_at""",
                    (
                        row["store_id"], row["metric_date"],
                        *(row[metric] for metric in TRAFFIC_METRICS), path.name, now,
                    ),
                )
            batch_id = self.record_batch(
                conn, expected_store_id, path.name, checksum,
                [row["metric_date"] for row in rows], DATASET_TRAFFIC, now,
            )
        return ImportResult(
            batch_id=batch_id, platform=OPS_PLATFORM, rows_read=len(rows), rows_written=len(rows),
            min_date=min(row["metric_date"] for row in rows),
            max_date=max(row["metric_date"] for row in rows),
            checksum=checksum, dataset=DATASET_TRAFFIC,
        )

    def import_timeslot_csv(self, path: Path, expected_store_id: str) -> ImportResult:
        """导入时段分布，按 (门店, 日期, 时段) 幂等更新。"""
        checksum = self._checksum(path)
        prior = self.find_batch(expected_store_id, checksum)
        if prior:
            return self.repeat_result(prior, checksum)

        reader = self._reader(path, TIMESLOT_REQUIRED_COLUMNS)
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row_number, raw in enumerate(reader, 2):
            metric_date = self._parse_date(raw["date"], row_number)
            store_id = self._guarded_store(raw["store_id"], row_number, expected_store_id)
            slot = self._require_text(raw["slot"], "slot", row_number, SLOT_MAX_LENGTH)
            key = (metric_date, slot)
            if key in seen:
                raise DataValidationError(f"CSV 内存在重复记录：{metric_date} {slot}")
            seen.add(key)
            parsed = {metric: self._parse_number(raw[metric], metric, row_number) for metric in TIMESLOT_METRICS}
            if all(value is None for value in parsed.values()):
                raise DataValidationError(f"第 {row_number} 行所有指标均为空")
            rows.append({"store_id": store_id, "metric_date": metric_date, "slot": slot, **parsed})
        if not rows:
            raise DataValidationError("CSV 没有数据行")

        now = datetime.now().isoformat(timespec="seconds")
        columns = ",".join(TIMESLOT_METRICS)
        placeholders = ",".join("?" for _ in TIMESLOT_METRICS)
        updates = ",".join(f"{metric}=excluded.{metric}" for metric in TIMESLOT_METRICS)
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    f"""insert into time_slot_metrics
                    (store_id,metric_date,slot,{columns},source_name,imported_at)
                    values (?,?,?,{placeholders},?,?)
                    on conflict(store_id,metric_date,slot) do update set
                    {updates},source_name=excluded.source_name,imported_at=excluded.imported_at""",
                    (
                        row["store_id"], row["metric_date"], row["slot"],
                        *(row[metric] for metric in TIMESLOT_METRICS), path.name, now,
                    ),
                )
            batch_id = self.record_batch(
                conn, expected_store_id, path.name, checksum,
                [row["metric_date"] for row in rows], DATASET_TIMESLOT, now,
            )
        return ImportResult(
            batch_id=batch_id, platform=OPS_PLATFORM, rows_read=len(rows), rows_written=len(rows),
            min_date=min(row["metric_date"] for row in rows),
            max_date=max(row["metric_date"] for row in rows),
            checksum=checksum, dataset=DATASET_TIMESLOT,
        )

    def import_trade_area_csv(self, path: Path, expected_store_id: str) -> ImportResult:
        """导入商圈维度（门店自己的商圈定义，第 2 期竞品对比按此维度聚合）。"""
        checksum = self._checksum(path)
        prior = self.find_batch(expected_store_id, checksum)
        if prior:
            return self.repeat_result(prior, checksum)

        reader = self._reader(path, TRADE_AREA_REQUIRED_COLUMNS)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row_number, raw in enumerate(reader, 2):
            store_id = self._guarded_store(raw["store_id"], row_number, expected_store_id)
            area_id = self._require_text(raw["area_id"], "area_id", row_number)
            if area_id in seen:
                raise DataValidationError(f"CSV 内存在重复记录：area_id {area_id}")
            seen.add(area_id)
            rows.append({
                "store_id": store_id,
                "area_id": area_id,
                "area_name": self._require_text(raw["area_name"], "area_name", row_number),
                "area_type": ("" if raw.get("area_type") is None else str(raw["area_type"]).strip()) or None,
                "note": ("" if raw.get("note") is None else str(raw["note"]).strip()) or None,
            })
        if not rows:
            raise DataValidationError("CSV 没有数据行")

        now = datetime.now().isoformat(timespec="seconds")
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    """insert into trade_areas
                    (store_id,area_id,area_name,area_type,note,source_name,imported_at)
                    values (?,?,?,?,?,?,?)
                    on conflict(store_id,area_id) do update set
                    area_name=excluded.area_name,area_type=excluded.area_type,note=excluded.note,
                    source_name=excluded.source_name,imported_at=excluded.imported_at""",
                    (row["store_id"], row["area_id"], row["area_name"], row["area_type"],
                     row["note"], path.name, now),
                )
            # 商圈是维度表，没有自然日期；批次日期用导入日，保证 min/max 语义完整
            today = date.today().isoformat()
            batch_id = self.record_batch(
                conn, expected_store_id, path.name, checksum, [today], DATASET_TRADE_AREA, now,
            )
        return ImportResult(
            batch_id=batch_id, platform=OPS_PLATFORM, rows_read=len(rows), rows_written=len(rows),
            min_date=today, max_date=today, checksum=checksum, dataset=DATASET_TRADE_AREA,
        )
    def import_inventory_csv(self, path: Path, expected_store_id: str) -> ImportResult:
        """导入库存盘点，按 (门店, 日期, 物料) 幂等更新，并重算该日期的库存预警。"""
        checksum = self._checksum(path)
        prior = self.find_batch(expected_store_id, checksum)
        if prior:
            return self.repeat_result(prior, checksum)

        reader = self._reader(path, INVENTORY_REQUIRED_COLUMNS)
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row_number, raw in enumerate(reader, 2):
            metric_date = self._parse_date(raw["date"], row_number)
            store_id = self._guarded_store(raw["store_id"], row_number, expected_store_id)
            item_id = self._require_text(raw["item_id"], "item_id", row_number)
            key = (metric_date, item_id)
            if key in seen:
                raise DataValidationError(f"CSV 内存在重复记录：{metric_date} {item_id}")
            seen.add(key)
            parsed = {metric: self._parse_number(raw[metric], metric, row_number) for metric in INVENTORY_METRICS}
            if parsed["quantity"] is None:
                raise DataValidationError(f"第 {row_number} 行 quantity 不能为空")
            rows.append({
                "store_id": store_id,
                "metric_date": metric_date,
                "item_id": item_id,
                "name": self._require_text(raw["name"], "name", row_number),
                "unit": self._require_text(raw["unit"], "unit", row_number),
                **parsed,
            })
        if not rows:
            raise DataValidationError("CSV 没有数据行")

        now = datetime.now().isoformat(timespec="seconds")
        dates = sorted({row["metric_date"] for row in rows})
        columns = ",".join(INVENTORY_METRICS)
        placeholders = ",".join("?" for _ in INVENTORY_METRICS)
        updates = ",".join(f"{metric}=excluded.{metric}" for metric in INVENTORY_METRICS)
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    f"""insert into inventory_items
                    (store_id,metric_date,item_id,name,unit,{columns},source_name,imported_at)
                    values (?,?,?,?,?,{placeholders},?,?)
                    on conflict(store_id,metric_date,item_id) do update set
                    name=excluded.name,unit=excluded.unit,{updates},
                    source_name=excluded.source_name,imported_at=excluded.imported_at""",
                    (
                        row["store_id"], row["metric_date"], row["item_id"], row["name"], row["unit"],
                        *(row[metric] for metric in INVENTORY_METRICS), path.name, now,
                    ),
                )
            # 该批涉及的日期整体重算预警：先清后建，保证与盘点结果一致（不做增量猜测）
            for metric_date in dates:
                conn.execute(
                    "delete from inventory_alerts where store_id=? and metric_date=?",
                    (expected_store_id, metric_date),
                )
                item_rows = conn.execute(
                    """select item_id,name,unit,quantity,safety_stock from inventory_items
                    where store_id=? and metric_date=? order by item_id""",
                    (expected_store_id, metric_date),
                ).fetchall()
                for item in item_rows:
                    level = self.alert_level(item["quantity"], item["safety_stock"])
                    if level is None:
                        continue
                    conn.execute(
                        """insert into inventory_alerts
                        (store_id,metric_date,item_id,name,unit,quantity,safety_stock,level,status,created_at)
                        values (?,?,?,?,?,?,?,?,?,?)
                        on conflict(store_id,metric_date,item_id) do update set
                        name=excluded.name,unit=excluded.unit,quantity=excluded.quantity,
                        safety_stock=excluded.safety_stock,level=excluded.level,created_at=excluded.created_at""",
                        (expected_store_id, metric_date, item["item_id"], item["name"], item["unit"],
                         item["quantity"], item["safety_stock"], level, "open", now),
                    )
            batch_id = self.record_batch(
                conn, expected_store_id, path.name, checksum, dates, DATASET_INVENTORY, now,
            )
        return ImportResult(
            batch_id=batch_id, platform=OPS_PLATFORM, rows_read=len(rows), rows_written=len(rows),
            min_date=min(dates), max_date=max(dates), checksum=checksum, dataset=DATASET_INVENTORY,
        )

    # ---------------- 查询 ----------------

    def read_traffic(self, store_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """select * from store_traffic_daily where store_id=? and metric_date between ? and ?
                order by metric_date""",
                (store_id, start_date, end_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_time_slots(self, store_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """select * from time_slot_metrics where store_id=? and metric_date between ? and ?
                order by metric_date, slot""",
                (store_id, start_date, end_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_inventory(self, store_id: str, metric_date: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """select * from inventory_items where store_id=? and metric_date=? order by item_id""",
                (store_id, metric_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_alerts(self, store_id: str, status: str = "open", limit: int = 200,
                    until: str | None = None) -> list[dict[str, Any]]:
        """读取库存预警；默认只看未处理（open），按日期倒序、级别优先。

        `until` 用于按目标日期复现历史快照（只取该日期及之前的盘点）。
        """
        if status and status not in ALERT_STATUSES:
            raise ValueError(f"预警状态只支持：{'、'.join(ALERT_STATUSES)}")
        sql = "select * from inventory_alerts where store_id=?"
        params: list[Any] = [store_id]
        if status:
            sql += " and status=?"
            params.append(status)
        if until:
            sql += " and metric_date<=?"
            params.append(until)
        sql += " order by metric_date desc, case level when 'critical' then 0 else 1 end, item_id limit ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def read_trade_areas(self, store_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "select * from trade_areas where store_id=? order by area_id", (store_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self, store_id: str, fresh_days: int = 2) -> dict[str, Any]:
        """门店侧四类数据的新鲜度；age_days <= fresh_days 视为 fresh。"""
        today = date.today()
        sources: dict[str, dict[str, Any]] = {}
        with self._connection() as conn:
            for dataset, table in (
                (DATASET_TRAFFIC, "store_traffic_daily"),
                (DATASET_TIMESLOT, "time_slot_metrics"),
                (DATASET_INVENTORY, "inventory_items"),
            ):
                row = conn.execute(
                    f"""select min(metric_date) min_date, max(metric_date) max_date,
                    count(distinct metric_date) day_count, max(imported_at) imported_at
                    from {table} where store_id=?""",
                    (store_id,),
                ).fetchone()
                sources[dataset] = self._source_state(dataset, row, today, fresh_days, by_date=True)
            row = conn.execute(
                """select count(*) area_count, max(imported_at) imported_at from trade_areas
                where store_id=?""",
                (store_id,),
            ).fetchone()
            sources[DATASET_TRADE_AREA] = self._source_state(dataset=DATASET_TRADE_AREA, row=row,
                                                              today=today, fresh_days=fresh_days,
                                                              by_date=False)
        return {
            "store_id": store_id,
            "sources": sources,
            "has_data": any(item["state"] != "not_connected" for item in sources.values()),
        }

    @staticmethod
    def _source_state(dataset: str, row: Any, today: date, fresh_days: int, by_date: bool) -> dict[str, Any]:
        label = OPS_DATASET_LABELS[dataset]
        item: dict[str, Any] = {"label": label, "dataset": dataset}
        if row is None:
            return {**item, "state": "not_connected", "day_count": 0, "min_date": None,
                    "max_date": None, "age_days": None, "imported_at": None}
        if not by_date:
            count = int(row["area_count"] or 0)
            state = "fresh" if count else "not_connected"
            return {**item, "state": state, "area_count": count, "min_date": None, "max_date": None,
                    "age_days": None, "imported_at": row["imported_at"]}
        max_date = row["max_date"]
        if not max_date:
            return {**item, "state": "not_connected", "day_count": 0, "min_date": None,
                    "max_date": None, "age_days": None, "imported_at": None}
        age_days = (today - date.fromisoformat(max_date)).days
        return {
            **item,
            "state": "fresh" if age_days <= fresh_days else "stale",
            "day_count": int(row["day_count"] or 0),
            "min_date": row["min_date"],
            "max_date": max_date,
            "age_days": age_days,
            "imported_at": row["imported_at"],
        }

    def summary(self, store_id: str, days: int = 7, today: date | None = None) -> OpsSummary:
        """近 N 天的门店侧汇总：开台与客流、时段分布、未处理库存预警。"""
        if days < 1 or days > 90:
            raise ValueError("统计周期必须在 1 到 90 天之间")
        end = today or date.today()
        start = end - timedelta(days=days - 1)
        start_iso, end_iso = start.isoformat(), end.isoformat()

        traffic_rows = self.read_traffic(store_id, start_iso, end_iso)
        opened = sum(int(row["tables_opened"] or 0) for row in traffic_rows)
        capacity = sum(int(row["table_capacity"] or 0) for row in traffic_rows)
        covers = sum(int(row["covers"] or 0) for row in traffic_rows)
        revenue = round(sum(float(row["revenue"] or 0) for row in traffic_rows), 2)
        traffic = {
            "has_data": bool(traffic_rows),
            "day_count": len(traffic_rows),
            "tables_opened": opened,
            "table_capacity": capacity,
            "covers": covers,
            "revenue": revenue,
            "turnover_rate": round(opened / capacity, 4) if capacity else None,
        }

        slot_rows = self.read_time_slots(store_id, start_iso, end_iso)
        grouped: dict[str, dict[str, Any]] = {}
        for row in slot_rows:
            slot = row["slot"]
            item = grouped.setdefault(slot, {"slot": slot, "day_count": 0, "covers": 0, "orders": 0, "revenue": 0.0})
            item["day_count"] += 1
            item["covers"] += int(row["covers"] or 0)
            item["orders"] += int(row["orders"] or 0)
            item["revenue"] += float(row["revenue"] or 0)
        total_orders = sum(item["orders"] for item in grouped.values())
        time_slots = []
        for slot in sorted(grouped):
            item = grouped[slot]
            item["revenue"] = round(item["revenue"], 2)
            item["order_share"] = round(item["orders"] / total_orders, 4) if total_orders else None
            time_slots.append(item)

        alerts = [
            {
                "item_id": row["item_id"], "name": row["name"], "unit": row["unit"],
                "quantity": row["quantity"], "safety_stock": row["safety_stock"],
                "level": row["level"], "status": row["status"], "metric_date": row["metric_date"],
            }
            for row in self.read_alerts(store_id, status="open", until=end_iso)
        ]
        with self._connection() as conn:
            stock = conn.execute(
                """select max(metric_date) latest_date, count(distinct metric_date) day_count
                from inventory_items where store_id=? and metric_date<=?""",
                (store_id, end_iso),
            ).fetchone()
        latest_date = stock["latest_date"] if stock else None
        inventory = {
            "has_data": bool(latest_date),
            "latest_date": latest_date,
            "day_count": int(stock["day_count"] or 0) if stock else 0,
            "alert_count": len(alerts),
        }
        return OpsSummary(
            store_id=store_id, days=days,
            has_data=bool(traffic_rows or slot_rows or latest_date),
            traffic=traffic, time_slots=time_slots, inventory=inventory, alerts=alerts,
        )
    # ---------------- 目录批量导入 ----------------

    def import_directory(self, inbox: Path, expected_store_id: str) -> dict[str, Any]:
        """扫描目录批量导入门店侧数据：逐文件识别，单个文件失败不影响其它文件。"""
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
                        "无法识别门店侧数据集：表头与《开台与客流导入模板》《时段分布导入模板》"
                        "《库存与物料导入模板》《商圈导入模板》都不匹配"
                    )
                results.append(self.import_one(path, dataset, expected_store_id))
            except (DataValidationError, ValueError, OSError) as exc:
                results.append(import_row_failed(path, str(exc)))
        return inbox_payload(inbox, expected_store_id, results)

    def import_one(self, path: Path, dataset: str, expected_store_id: str) -> dict[str, Any]:
        importer = {
            DATASET_TRAFFIC: self.import_traffic_csv,
            DATASET_TIMESLOT: self.import_timeslot_csv,
            DATASET_INVENTORY: self.import_inventory_csv,
            DATASET_TRADE_AREA: self.import_trade_area_csv,
        }[dataset]
        result = importer(path, expected_store_id)
        return import_row_ok(path, dataset, OPS_DATASET_LABELS[dataset], result)


def import_row_ok(path: Path, dataset: str, label: str, result: ImportResult) -> dict[str, Any]:
    """统一收件箱里"成功 / 跳过"的结果行（九类数据集共用同一形状）。"""
    return {
        "file": path.name,
        "dataset": dataset,
        "dataset_label": label,
        "status": "success" if result.rows_written else "skipped",
        "rows_read": result.rows_read,
        "rows_written": result.rows_written,
        "min_date": result.min_date,
        "max_date": result.max_date,
        "error": None,
    }


def import_row_failed(path: Path, error: str) -> dict[str, Any]:
    """统一收件箱里"失败"的结果行（形状与成功行一致，便于统一渲染）。"""
    return {
        "file": path.name,
        "dataset": None,
        "dataset_label": None,
        "status": "failed",
        "rows_read": 0,
        "rows_written": 0,
        "min_date": None,
        "max_date": None,
        "error": error,
    }


def inbox_payload(inbox: Path, store_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """统一收件箱的汇总载荷（file_count / success / skipped / failed）。"""
    return {
        "inbox": str(inbox),
        "store_id": store_id,
        "file_count": len(results),
        "success_count": sum(item["status"] == "success" for item in results),
        "skipped_count": sum(item["status"] == "skipped" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "files": results,
    }


def import_inbox(
    inbox: Path,
    expected_store_id: str,
    platform_store: PlatformMetricStore,
    ops_store: StoreOpsDataStore,
    competitor_store: Any,
) -> dict[str, Any]:
    """统一收件箱导入：平台侧 → 门店侧 → 竞品，按表头逐级识别。

    这是 `import-dir` 的唯一实现路径（九类数据集共用同一个收件箱），
    避免同一个文件被多次回报结果。单个文件失败不影响其它文件。

    `competitor_store` 用 `Any` 标注而不导入类型：`competitor.py` 反向依赖本模块
    （它用 `StoreOpsDataStore` 共用 `ops_import_batches` 账本），这里再导入会形成循环。
    它必须提供 `detect_dataset()` 与 `import_one()` 两个方法。
    """
    inbox = Path(inbox)
    if not inbox.is_dir():
        raise DataValidationError(f"导入目录不存在：{inbox}")
    files = sorted(
        item for item in inbox.iterdir()
        if item.is_file() and item.suffix.lower() == ".csv" and not item.name.startswith(".")
    )
    def dispatch(path: Path) -> dict[str, Any]:
        header = StoreOpsDataStore._read_header(path)
        platform_dataset = PlatformMetricStore.detect_dataset(header)
        if platform_dataset is not None:
            importer = {
                DATASET_STORE: platform_store.import_csv,
                DATASET_PACKAGE: platform_store.import_packages_csv,
                DATASET_REVIEW: platform_store.import_reviews_csv,
            }[platform_dataset]
            result = importer(path, expected_store_id)
            return import_row_ok(path, platform_dataset,
                                 PLATFORM_DATASET_LABELS[platform_dataset], result)
        ops_dataset = StoreOpsDataStore.detect_dataset(header)
        if ops_dataset is not None:
            return ops_store.import_one(path, ops_dataset, expected_store_id)
        competitor_dataset = competitor_store.detect_dataset(header)
        if competitor_dataset is None:
            raise DataValidationError(
                "无法识别数据集：表头与平台侧三类模板、门店侧四类模板、竞品两类模板都不匹配"
            )
        return competitor_store.import_one(path, competitor_dataset, expected_store_id)

    accepted: dict[str, dict[str, Any]] = {}
    deferred: list[Path] = []
    for path in files:
        try:
            accepted[path.name] = dispatch(path)
        except (DataValidationError, ValueError, OSError):
            # 依赖可能在同一批文件里：竞品门店要校验本店商圈（area_id），而商圈文件
            # 未必排在它前面。先挂起，等其余文件落库后再重试一次，让结果不受文件名顺序影响。
            deferred.append(path)
    for path in deferred:
        try:
            accepted[path.name] = dispatch(path)
        except (DataValidationError, ValueError, OSError) as exc:
            accepted[path.name] = import_row_failed(path, str(exc))
    # 按原始文件顺序输出，保证同一批文件的回报顺序稳定可预期
    results = [accepted[path.name] for path in files]
    return inbox_payload(inbox, expected_store_id, results)

    def import_one(self, path: Path, dataset: str, expected_store_id: str) -> dict[str, Any]:
        importer = {
            DATASET_TRAFFIC: self.import_traffic_csv,
            DATASET_TIMESLOT: self.import_timeslot_csv,
            DATASET_INVENTORY: self.import_inventory_csv,
            DATASET_TRADE_AREA: self.import_trade_area_csv,
        }[dataset]
        result = importer(path, expected_store_id)
        return import_row_ok(path, dataset, OPS_DATASET_LABELS[dataset], result)
