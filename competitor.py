#!/usr/bin/env python3
"""竞品监控数据（竞品门店、竞品采样明细）的统一指标库。

第 2 期 v1 的边界（与总方案第 2 期一致）：
- 数据来源只有**人工每周采样导出**：系统**不做任何网络抓取**，不自动执行竞品侧动作。
- 采样行必须带**采样人、采集方式、来源说明**：竞品信息是第三方公开数据，来源必须自证。
- 商圈归属：`competitor_stores.area_id` 必须存在于本店 `trade_areas`（第 1 期的商圈数据集），
  否则该行导入直接判错——这是"同商圈竞品对比"能成立的前提。
- 批次账本**复用门店侧库的 `ops_import_batches`**（由 `StoreOpsDataStore` 拥有），
  竞品数据与门店侧四类数据共用同一本账，不再另开一套。

本模块只负责"把采样数据正确落库"；匹配确认（`competitor_matches`）与四维对比分别是
C 批与 D 批的工作，此处只建表不写业务逻辑。
"""

from __future__ import annotations

import csv
import hashlib
import math
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from db import Connection, Database
from platform_data import PLATFORMS, DataValidationError, ImportResult
from store_ops_data import (
    OPS_PLATFORM,
    StoreOpsDataStore,
    import_row_ok as _ok_row,
)


DATASET_COMPETITOR_STORE = "competitor_store"
DATASET_COMPETITOR_OFFER = "competitor_offer"

COMPETITOR_DATASET_LABELS = {
    DATASET_COMPETITOR_STORE: "竞品门店",
    DATASET_COMPETITOR_OFFER: "竞品采样明细",
}

# 采样行必须自证的三个合规字段：采样人、采集方式、来源说明
COMPETITOR_OFFER_REQUIRED_COLUMNS = (
    "date", "store_id", "competitor_id", "package_name", "price",
    "sampled_by", "source_method", "source_note",
)
COMPETITOR_OFFER_OPTIONAL_COLUMNS = ("activity", "activity_note")
COMPETITOR_STORE_REQUIRED_COLUMNS = ("store_id", "competitor_id", "competitor_name", "area_id")

TRADE_AREA_LIMIT_IN_MESSAGE = 10
MATCH_STATUSES = ("pending", "confirmed", "rejected")
TEXT_MAX_LENGTH = 200
NOTE_MAX_LENGTH = 500


class CompetitorStore:
    """竞品门店与采样明细的导入入口（SQLite / PostgreSQL 双引擎）。

    `ops_store` 指向同一数据库里的门店侧库：竞品的批次账本与商圈清单都由它提供。
    不传时按同一个 `db_target` 现建一个，保证单独使用本类时账本与 `trade_areas` 也存在。
    """

    def __init__(self, db_target: Any, tenant_id: str = "", ops_store: StoreOpsDataStore | None = None):
        self.db = Database(db_target, tenant_id=tenant_id)
        self.ops = ops_store or StoreOpsDataStore(db_target, tenant_id=tenant_id)
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connection(self):
        return self.db.connection()

    def _init_schema(self) -> None:
        if self.db.dialect == "postgresql":
            with self._connection() as conn:
                self.db.apply_schema(conn)
            return
        with self._connection() as conn:
            conn.executescript(
                """
                create table if not exists competitor_stores (
  tenant_id text not null default '',
                  store_id text not null,
                  competitor_id text not null,
                  competitor_name text not null,
                  area_id text not null,
                  note text,
                  source_name text not null,
                  imported_at text not null,
                  primary key (store_id, competitor_id)
                );
                create index if not exists idx_competitor_stores_area
                  on competitor_stores(store_id, area_id);

                create table if not exists competitor_offers (
  tenant_id text not null default '',
                  store_id text not null,
                  competitor_id text not null,
                  sample_date text not null,
                  package_name text not null,
                  price real not null,
                  activity text,
                  activity_note text,
                  source_method text,
                  source_note text,
                  sampled_by text,
                  source_name text not null,
                  imported_at text not null,
                  primary key (store_id, competitor_id, sample_date, package_name)
                );
                create index if not exists idx_competitor_offers_window
                  on competitor_offers(store_id, sample_date);

                create table if not exists competitor_matches (
  tenant_id text not null default '',
                  store_id text not null,
                  platform text not null check (platform in ('meituan','douyin')),
                  package_id text not null,
                  competitor_id text not null,
                  competitor_package_name text not null,
                  status text not null default 'confirmed' check (status in ('pending','confirmed','rejected')),
                  confirmed_by text,
                  confirmed_at text,
                  note text,
                  created_at text not null,
                  updated_at text not null,
                  unique (store_id, platform, package_id, competitor_id, competitor_package_name)
                );
                create index if not exists idx_competitor_matches_status
                  on competitor_matches(store_id, status);
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """幂等迁移：SQLite 旧库补租户列与唯一索引（PostgreSQL 侧由 schema.sql 保证）。"""
        with self._connection() as conn:
            self.db.ensure_tenant_column(
                conn, "competitor_stores",
                ("ux_competitor_stores_runtime", ("tenant_id", "store_id", "competitor_id")))
            self.db.ensure_tenant_column(
                conn, "competitor_offers",
                ("ux_competitor_offers_runtime",
                 ("tenant_id", "store_id", "competitor_id", "sample_date", "package_name")))
            self.db.ensure_tenant_column(
                conn, "competitor_matches",
                ("ux_competitor_matches_runtime",
                 ("tenant_id", "store_id", "platform", "package_id", "competitor_id",
                  "competitor_package_name")))
    # ---------------- 表头识别与基础校验 ----------------

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
        """按表头识别竞品数据集；无法识别时返回 None。

        与平台侧三类、门店侧四类互不抢占：竞品采样要求 `competitor_id + package_name + price`，
        竞品门店要求 `competitor_id + competitor_name + area_id`（没有 `area_name`，
        因此不会被商圈数据集抢走）。
        """
        columns = {str(name).strip() for name in (fieldnames or []) if str(name).strip()}
        if set(COMPETITOR_OFFER_REQUIRED_COLUMNS) <= columns:
            return DATASET_COMPETITOR_OFFER
        if set(COMPETITOR_STORE_REQUIRED_COLUMNS) <= columns:
            return DATASET_COMPETITOR_STORE
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
    def _parse_date(raw: Any, row_number: int) -> str:
        try:
            return date.fromisoformat(("" if raw is None else str(raw)).strip()).isoformat()
        except ValueError as exc:
            raise DataValidationError(f"第 {row_number} 行 date 必须是 YYYY-MM-DD") from exc

    @staticmethod
    def _parse_price(raw: Any, row_number: int) -> float:
        value = ("" if raw is None else str(raw)).strip()
        if value == "":
            raise DataValidationError(f"第 {row_number} 行 price 不能为空")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise DataValidationError(f"第 {row_number} 行 price 不是数字") from exc
        if number < 0:
            raise DataValidationError(f"第 {row_number} 行 price 不能为负数")
        return float(number)

    @staticmethod
    def _require_text(raw: Any, field: str, row_number: int, max_length: int = TEXT_MAX_LENGTH) -> str:
        value = ("" if raw is None else str(raw)).strip()
        if not value:
            raise DataValidationError(f"第 {row_number} 行 {field} 不能为空")
        if len(value) > max_length:
            raise DataValidationError(f"第 {row_number} 行 {field} 超过 {max_length} 个字符")
        return value

    @staticmethod
    def _optional_text(raw: Any, max_length: int = NOTE_MAX_LENGTH) -> str | None:
        value = ("" if raw is None else str(raw)).strip()
        if not value:
            return None
        return value[:max_length]

    def _table_exists(self, conn: Connection, table: str) -> bool:
        if self.db.dialect == "postgresql":
            row = conn.execute(
                "select 1 from information_schema.tables where table_schema='public' and table_name=?",
                (table,),
            ).fetchone()
        else:
            row = conn.execute(
                "select 1 from sqlite_master where type='table' and name=?", (table,)
            ).fetchone()
        return row is not None

    def known_areas(self, store_id: str) -> set[str]:
        """本店商圈清单（来自第 1 期的 `trade_areas`）。

        商圈是竞品归属的空间维度，没有它就无法界定"同商圈竞品"，因此竞品门店导入必须过这一关。
        `trade_areas` 表不存在时按空集处理，由调用方给出"请先导入商圈"的明确提示。
        """
        with self._connection() as conn:
            if not self._table_exists(conn, "trade_areas"):
                return set()
            rows = conn.execute(
                "select area_id from trade_areas where store_id=?", (store_id,)
            ).fetchall()
        return {str(row["area_id"]) for row in rows}

    def _guard_area(self, area_id: str, known: set[str], row_number: int) -> str:
        if not known:
            raise DataValidationError(
                "本店还没有商圈清单，无法校验 area_id；请先导入商圈数据集（import-trade-area）"
            )
        if area_id not in known:
            preview = "、".join(sorted(known)[:TRADE_AREA_LIMIT_IN_MESSAGE])
            more = "…" if len(known) > TRADE_AREA_LIMIT_IN_MESSAGE else ""
            raise DataValidationError(
                f"第 {row_number} 行 area_id「{area_id}」不在本店商圈清单中（现有：{preview}{more}）"
            )
        return area_id
    @staticmethod
    def _guard_store(raw: Any, row_number: int, expected_store_id: str) -> str:
        store_id = ("" if raw is None else str(raw)).strip()
        if store_id != expected_store_id:
            raise DataValidationError(f"第 {row_number} 行门店编码不匹配：{store_id}")
        return store_id

    # ---------------- 两类数据集的导入 ----------------

    def import_competitor_store_csv(self, path: Path, expected_store_id: str) -> ImportResult:
        """导入竞品门店维度，按 (门店, 竞品) 幂等更新；`area_id` 必须在本店商圈清单内。"""
        checksum = _checksum(path)
        prior = self.ops.find_batch(expected_store_id, checksum)
        if prior:
            return self.ops.repeat_result(prior, checksum)

        reader = self._reader(path, COMPETITOR_STORE_REQUIRED_COLUMNS)
        known = self.known_areas(expected_store_id)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row_number, raw in enumerate(reader, 2):
            store_id = self._guard_store(raw.get("store_id"), row_number, expected_store_id)
            competitor_id = self._require_text(raw.get("competitor_id"), "competitor_id", row_number)
            if competitor_id in seen:
                raise DataValidationError(f"CSV 内存在重复记录：competitor_id {competitor_id}")
            seen.add(competitor_id)
            area_id = self._require_text(raw.get("area_id"), "area_id", row_number)
            rows.append({
                "store_id": store_id,
                "competitor_id": competitor_id,
                "competitor_name": self._require_text(
                    raw.get("competitor_name"), "competitor_name", row_number),
                "area_id": self._guard_area(area_id, known, row_number),
                "note": self._optional_text(raw.get("note")),
            })
        if not rows:
            raise DataValidationError("CSV 没有数据行")

        now = datetime.now().isoformat(timespec="seconds")
        today = date.today().isoformat()
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    """insert into competitor_stores
                    (store_id,competitor_id,competitor_name,area_id,note,source_name,imported_at)
                    values (?,?,?,?,?,?,?)
                    on conflict(store_id,competitor_id) do update set
                    competitor_name=excluded.competitor_name,area_id=excluded.area_id,note=excluded.note,
                    source_name=excluded.source_name,imported_at=excluded.imported_at""",
                    (row["store_id"], row["competitor_id"], row["competitor_name"], row["area_id"],
                     row["note"], path.name, now),
                )
            # 竞品门店是维度表（无自然日期），批次日期用导入日，与商圈数据集保持一致
            batch_id = self.ops.record_batch(
                conn, expected_store_id, path.name, checksum, [today],
                DATASET_COMPETITOR_STORE, now,
            )
        return ImportResult(
            batch_id=batch_id, platform=OPS_PLATFORM, rows_read=len(rows), rows_written=len(rows),
            min_date=today, max_date=today, checksum=checksum, dataset=DATASET_COMPETITOR_STORE,
        )

    def import_competitor_offer_csv(self, path: Path, expected_store_id: str) -> ImportResult:
        """导入竞品采样明细，按 (门店, 竞品, 采样日期, 套餐名) 幂等。

        **不覆盖历史**：不同采样日期的行各留一份，这是后续算环比的前提。
        采样人 / 采集方式 / 来源说明是必填的合规留痕字段。
        """
        checksum = _checksum(path)
        prior = self.ops.find_batch(expected_store_id, checksum)
        if prior:
            return self.ops.repeat_result(prior, checksum)

        reader = self._reader(path, COMPETITOR_OFFER_REQUIRED_COLUMNS)
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for row_number, raw in enumerate(reader, 2):
            sample_date = self._parse_date(raw.get("date"), row_number)
            store_id = self._guard_store(raw.get("store_id"), row_number, expected_store_id)
            competitor_id = self._require_text(raw.get("competitor_id"), "competitor_id", row_number)
            package_name = self._require_text(raw.get("package_name"), "package_name", row_number)
            key = (competitor_id, sample_date, package_name)
            if key in seen:
                raise DataValidationError(
                    f"CSV 内存在重复记录：{competitor_id} {sample_date} {package_name}")
            seen.add(key)
            rows.append({
                "store_id": store_id,
                "competitor_id": competitor_id,
                "sample_date": sample_date,
                "package_name": package_name,
                "price": self._parse_price(raw.get("price"), row_number),
                "activity": self._optional_text(raw.get("activity")),
                "activity_note": self._optional_text(raw.get("activity_note")),
                "source_method": self._require_text(raw.get("source_method"), "source_method", row_number),
                "source_note": self._require_text(raw.get("source_note"), "source_note", row_number),
                "sampled_by": self._require_text(raw.get("sampled_by"), "sampled_by", row_number),
            })
        if not rows:
            raise DataValidationError("CSV 没有数据行")

        now = datetime.now().isoformat(timespec="seconds")
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    """insert into competitor_offers
                    (store_id,competitor_id,sample_date,package_name,price,activity,activity_note,
                     source_method,source_note,sampled_by,source_name,imported_at)
                    values (?,?,?,?,?,?,?,?,?,?,?,?)
                    on conflict(store_id,competitor_id,sample_date,package_name) do update set
                    price=excluded.price,activity=excluded.activity,activity_note=excluded.activity_note,
                    source_method=excluded.source_method,source_note=excluded.source_note,
                    sampled_by=excluded.sampled_by,source_name=excluded.source_name,
                    imported_at=excluded.imported_at""",
                    (row["store_id"], row["competitor_id"], row["sample_date"], row["package_name"],
                     row["price"], row["activity"], row["activity_note"], row["source_method"],
                     row["source_note"], row["sampled_by"], path.name, now),
                )
            dates = sorted({row["sample_date"] for row in rows})
            batch_id = self.ops.record_batch(
                conn, expected_store_id, path.name, checksum, dates,
                DATASET_COMPETITOR_OFFER, now,
            )
        return ImportResult(
            batch_id=batch_id, platform=OPS_PLATFORM, rows_read=len(rows), rows_written=len(rows),
            min_date=min(dates), max_date=max(dates), checksum=checksum,
            dataset=DATASET_COMPETITOR_OFFER,
        )

    def import_one(self, path: Path, dataset: str, expected_store_id: str) -> dict[str, Any]:
        """按数据集名导入单个文件，返回统一收件箱的结果行（供 `import_inbox` 调用）。"""
        importer = {
            DATASET_COMPETITOR_STORE: self.import_competitor_store_csv,
            DATASET_COMPETITOR_OFFER: self.import_competitor_offer_csv,
        }[dataset]
        result = importer(path, expected_store_id)
        return _ok_row(path, dataset, COMPETITOR_DATASET_LABELS[dataset], result)

    # ---------------- 匹配（人工确认，硬闸门） ----------------

    def upsert_match(
        self,
        store_id: str,
        *,
        platform: str,
        package_id: str,
        competitor_id: str,
        competitor_package_name: str,
        status: str = "confirmed",
        note: str | None = None,
        actor_id: str = "",
    ) -> dict[str, Any]:
        """登记或更新一条"本店套餐 ↔ 竞品套餐"匹配（**只由人工发起**）。

        本方法**不做任何按名字猜测的候选生成**：调用方给出的就是人工挑选的结果。
        `confirmed` 才写 `confirmed_by` / `confirmed_at`；`pending` / `rejected` 不写，
        但"谁在什么时候做的这个决定"由接口层写入审计日志。

        返回 `{record, created, previous_status}`，审计留痕由调用方完成。
        """
        platform = (platform or "").strip().lower()
        if platform not in PLATFORMS:
            raise DataValidationError("platform 只支持：" + "、".join(PLATFORMS))
        if status not in MATCH_STATUSES:
            raise DataValidationError("status 只支持：" + "、".join(MATCH_STATUSES))
        fields = {
            "package_id": package_id,
            "competitor_id": competitor_id,
            "competitor_package_name": competitor_package_name,
        }
        for name, value in fields.items():
            text = ("" if value is None else str(value)).strip()
            if not text:
                raise DataValidationError(f"{name} 不能为空")
            if len(text) > TEXT_MAX_LENGTH:
                raise DataValidationError(f"{name} 超过 {TEXT_MAX_LENGTH} 个字符")
            fields[name] = text
        package_id = fields["package_id"]
        competitor_id = fields["competitor_id"]
        competitor_package_name = fields["competitor_package_name"]
        note = self._optional_text(note)

        # 匹配是"本店套餐 / 竞品 / 竞品套餐"三者的连接；后两者必须已经存在，
        # 否则这条映射在后续对比里会变成静默失效，宁可在此处明确报错。
        with self._connection() as conn:
            registered = conn.execute(
                "select 1 from competitor_stores where store_id=? and competitor_id=?",
                (store_id, competitor_id),
            ).fetchone()
            sampled = conn.execute(
                """select 1 from competitor_offers
                where store_id=? and competitor_id=? and package_name=? limit 1""",
                (store_id, competitor_id, competitor_package_name),
            ).fetchone()
        if registered is None:
            raise DataValidationError(
                f"竞品「{competitor_id}」尚未登记；请先导入竞品门店数据（import-competitor-store）"
            )
        if sampled is None:
            raise DataValidationError(
                f"竞品「{competitor_id}」没有名为「{competitor_package_name}」的采样记录；"
                "请先导入竞品采样数据（import-competitor-offer）"
            )

        now = datetime.now().isoformat(timespec="seconds")
        # confirmed_by / confirmed_at 只描述"确认"这件事；pending / rejected 的
        # 决策人与决策时间由调用方写入审计日志，不在本表留半套信息。
        is_confirmed = status == "confirmed"
        confirmed_by = (actor_id or None) if is_confirmed else None
        confirmed_at = now if is_confirmed else None
        with self._connection() as conn:
            prior = conn.execute(
                """select status from competitor_matches
                where store_id=? and platform=? and package_id=? and competitor_id=?
                  and competitor_package_name=?""",
                (store_id, platform, package_id, competitor_id, competitor_package_name),
            ).fetchone()
            conn.execute(
                """insert into competitor_matches
                (store_id,platform,package_id,competitor_id,competitor_package_name,status,
                 confirmed_by,confirmed_at,note,created_at,updated_at)
                values (?,?,?,?,?,?,?,?,?,?,?)
                on conflict(store_id,platform,package_id,competitor_id,competitor_package_name)
                do update set status=excluded.status,confirmed_by=excluded.confirmed_by,
                confirmed_at=excluded.confirmed_at,note=excluded.note,updated_at=excluded.updated_at""",
                (store_id, platform, package_id, competitor_id, competitor_package_name, status,
                 confirmed_by, confirmed_at, note, now, now),
            )
            record = dict(conn.execute(
                """select * from competitor_matches
                where store_id=? and platform=? and package_id=? and competitor_id=?
                  and competitor_package_name=?""",
                (store_id, platform, package_id, competitor_id, competitor_package_name),
            ).fetchone())
        return {"record": record, "created": prior is None,
                "previous_status": prior["status"] if prior else None}

    def list_matches(self, store_id: str, status: str | None = None) -> list[dict[str, Any]]:
        """匹配清单；`status` 为空时返回全部三种状态。"""
        if status is not None and status not in MATCH_STATUSES:
            raise ValueError("status 只支持：" + "、".join(MATCH_STATUSES))
        sql = "select * from competitor_matches where store_id=?"
        params: list[Any] = [store_id]
        if status is not None:
            sql += " and status=?"
            params.append(status)
        sql += " order by status, platform, package_id, competitor_id, competitor_package_name"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def confirmed_matches(self, store_id: str) -> list[dict[str, Any]]:
        """**对比的唯一输入**：只有人工确认过的匹配才可用（pending / rejected 一律不参与）。"""
        return self.list_matches(store_id, status="confirmed")

    def unmatched_competitor_packages(self, store_id: str) -> list[dict[str, Any]]:
        """已采样、但没有任何 `confirmed` 匹配的竞品套餐（供人工挑选）。

        这里只做"哪些还没配"的陈列，**不猜它对应本店哪个套餐**。
        """
        with self._connection() as conn:
            rows = conn.execute(
                """select o.competitor_id, o.package_name,
                          count(*) as sample_count, max(o.sample_date) as latest_sample
                from competitor_offers o
                where o.store_id=?
                  and not exists (
                    select 1 from competitor_matches m
                    where m.store_id=o.store_id and m.competitor_id=o.competitor_id
                      and m.competitor_package_name=o.package_name and m.status='confirmed')
                group by o.competitor_id, o.package_name
                order by o.competitor_id, o.package_name""",
                (store_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------- 查询 ----------------

    def read_stores(self, store_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "select * from competitor_stores where store_id=? order by competitor_id", (store_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def read_offers(self, store_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """select * from competitor_offers where store_id=? and sample_date between ? and ?
                order by sample_date, competitor_id, package_name""",
                (store_id, start_date, end_date),
            ).fetchall()
        return [dict(row) for row in rows]



    # ---------------- 四维对比（原第 2 期 D 批） ----------------

    def compare(self, store_id: str, config: dict[str, Any] | None = None,
                *, today: date | None = None) -> dict[str, Any]:
        """竞品四维对比：价格 / 套餐 / 活动 / 商圈变化。

        四条硬约束（与总方案第 2 期口径一致）：
        1. **只有 `confirmed` 匹配参与**价格与套餐对比；pending / rejected 一律不参与。
        2. 样本低于 `competitor_min_samples` 时只报"样本不足"，**不输出分位与变化率**。
        3. 环比取**上一等长窗口**（默认 28 天）。
        4. `competitor_max_matches` 截断时如实标注。
        """
        rules = (config or {}).get("rules") or {}
        band_size = float(rules.get("competitor_price_band_size", 20) or 20)
        window_days = int(rules.get("competitor_window_days", 28) or 28)
        min_samples = int(rules.get("competitor_min_samples", 5) or 5)
        shift_percent = float(rules.get("competitor_area_shift_percent", 10) or 10)
        max_matches = int(rules.get("competitor_max_matches", 50) or 50)
        anchor = today or date.today()
        start = anchor - timedelta(days=window_days - 1)
        previous_end = start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=window_days - 1)
        start_iso, end_iso = start.isoformat(), anchor.isoformat()

        matches = self.confirmed_matches(store_id)
        truncated = max(0, len(matches) - max_matches)
        matches = matches[:max_matches]
        offers = self.read_offers(store_id, start_iso, end_iso)
        previous_offers = self.read_offers(store_id, previous_start.isoformat(),
                                           previous_end.isoformat())
        competitors = {row["competitor_id"]: row for row in self.read_stores(store_id)}
        area_of = {key: value["area_id"] for key, value in competitors.items()}
        prices = [float(row["price"]) for row in offers]
        previous_prices = [float(row["price"]) for row in previous_offers]

        def _insufficient(count: int) -> bool:
            return count < min_samples

        price: dict[str, Any] = {
            "band_size": band_size,
            "sample_size": len(prices),
            "insufficient": _insufficient(len(prices)),
            "min": None, "p25": None, "p50": None, "p75": None, "max": None,
            "bands": [],
        }
        if prices and not _insufficient(len(prices)):
            price.update({
                "min": round(min(prices), 2),
                "p25": round(_percentile(prices, 0.25), 2),
                "p50": round(_percentile(prices, 0.50), 2),
                "p75": round(_percentile(prices, 0.75), 2),
                "max": round(max(prices), 2),
                "bands": _price_bands(prices, band_size),
            })

        previous_median = (_percentile(previous_prices, 0.50) if previous_prices
                           and not _insufficient(len(previous_prices)) else None)
        current_median = price["p50"]
        change_percent = None
        if current_median is not None and previous_median not in (None, 0):
            change_percent = round((current_median - previous_median) / previous_median * 100, 2)
        area_change: dict[str, Any] = {
            "window_days": window_days,
            "previous_window": {"start": previous_start.isoformat(), "end": previous_end.isoformat()},
            "current_median": current_median,
            "previous_median": (round(previous_median, 2) if previous_median is not None else None),
            "change_percent": change_percent,
            "shift_threshold_percent": shift_percent,
            "insufficient": _insufficient(len(prices)) or _insufficient(len(previous_prices)),
            "shifted": bool(change_percent is not None and abs(change_percent) >= shift_percent),
        }

        store_prices = {
            str(item.get("package_id")): float(item.get("sale_price") or 0)
            for item in ((config or {}).get("package_costs") or [])
            if item.get("sale_price")
        }
        matched_rows = []
        for item in matches:
            store_price = store_prices.get(str(item["package_id"]))
            competitor_price = _latest_offer_price(offers, item["competitor_id"],
                                                   item["competitor_package_name"])
            if store_price is None or competitor_price is None:
                continue
            matched_rows.append({
                "area_id": area_of.get(item["competitor_id"]),
                "platform": item["platform"],
                "package_id": item["package_id"],
                "competitor_id": item["competitor_id"],
                "competitor_package_name": item["competitor_package_name"],
                "store_price": round(store_price, 2),
                "competitor_price": round(competitor_price, 2),
                "gap": round(store_price - competitor_price, 2),
                "gap_rate": (round((store_price - competitor_price) / competitor_price, 4)
                             if competitor_price else None),
            })

        def _bands_of(values: list[float]) -> set[int]:
            return {int(math.floor(value / band_size)) * int(band_size) for value in values}

        store_band_set = _bands_of(list(store_prices.values()))
        competitor_band_set = _bands_of(prices)
        packages: dict[str, Any] = {
            "store_package_count": len(store_prices),
            "competitor_package_count": len({row["package_name"] for row in offers}),
            "store_bands": sorted(store_band_set),
            "competitor_bands": sorted(competitor_band_set),
            "band_gap": sorted(store_band_set - competitor_band_set),
            "confirmed_match_count": len(matches),
            "priced_match_count": len(matched_rows),
            "matches": matched_rows,
        }

        activity_rows = [row for row in offers if (row.get("activity") or "").strip()]
        activity_counts: dict[str, int] = {}
        for row in activity_rows:
            label = str(row["activity"]).strip()
            activity_counts[label] = activity_counts.get(label, 0) + 1
        activities: dict[str, Any] = {
            "offer_count": len(offers),
            "with_activity": len(activity_rows),
            "kinds": [{"activity": name, "count": count}
                      for name, count in sorted(activity_counts.items(), key=lambda kv: (-kv[1], kv[0]))],
        }

        # 按商圈分组：文档口径是"按商圈与价格带对比"，规则也是"每个商圈最多一条"，
        # 所以这里必须给出分商圈的价格带与环比，而不只是全量聚合。
        by_area = []
        for area_id in sorted(set(area_of.values()) | {
                row["area_id"] for row in competitors.values()}):
            area_prices = [float(row["price"]) for row in offers
                           if area_of.get(row["competitor_id"]) == area_id]
            area_previous = [float(row["price"]) for row in previous_offers
                             if area_of.get(row["competitor_id"]) == area_id]
            enough_now = area_prices and not _insufficient(len(area_prices))
            enough_before = area_previous and not _insufficient(len(area_previous))
            current = round(_percentile(area_prices, 0.50), 2) if enough_now else None
            previous = _percentile(area_previous, 0.50) if enough_before else None
            delta = (round((current - previous) / previous * 100, 2)
                     if current is not None and previous not in (None, 0) else None)
            by_area.append({
                "area_id": area_id,
                "sample_size": len(area_prices),
                "insufficient": not (enough_now and enough_before),
                "current_median": current,
                "previous_median": round(previous, 2) if previous is not None else None,
                "change_percent": delta,
                "shifted": bool(delta is not None and abs(delta) >= shift_percent),
            })

        return {
            "store_id": store_id,
            "window": {"start": start_iso, "end": end_iso, "days": window_days},
            "by_area": by_area,
            "has_data": bool(offers),
            "thresholds": {
                "price_band_size": band_size,
                "window_days": window_days,
                "min_samples": min_samples,
                "area_shift_percent": shift_percent,
                "max_matches": max_matches,
            },
            "competitors": [
                {"competitor_id": key, "competitor_name": value["competitor_name"],
                 "area_id": value["area_id"]}
                for key, value in sorted(competitors.items())
            ],
            "price": price,
            "packages": packages,
            "activities": activities,
            "area_change": area_change,
            "unmatched_competitor_packages": self.unmatched_competitor_packages(store_id),
            "truncated_matches": truncated,
            "note": ("只有 confirmed 匹配参与价格与套餐对比；样本低于门槛时只报样本不足，不下结论。"),
        }

def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _percentile(values: list[float], quantile: float) -> float:
    """线性插值分位（与常见统计口径一致，便于测试边界与并列值）。"""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = quantile * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _price_bands(prices: list[float], band_size: float) -> list[dict[str, Any]]:
    """按固定档宽分档；档位从 0 起算，左闭右开。"""
    counts: dict[int, int] = {}
    for price in prices:
        low = int(math.floor(price / band_size)) * int(band_size)
        counts[low] = counts.get(low, 0) + 1
    return [{"low": low, "high": low + int(band_size), "count": counts[low]}
            for low in sorted(counts)]


def _latest_offer_price(offers: list[dict[str, Any]], competitor_id: str,
                        package_name: str) -> float | None:
    """取窗口内该竞品套餐的最新一次采样价（同一天多条时取最后写入的）。"""
    hits = [row for row in offers
            if row["competitor_id"] == competitor_id and row["package_name"] == package_name]
    if not hits:
        return None
    hits.sort(key=lambda row: (str(row["sample_date"]), str(row.get("imported_at") or "")))
    return float(hits[-1]["price"])
