#!/usr/bin/env python3
"""PostgreSQL 落地工具（二期批次 8）：建表 → 从 SQLite 迁移数据 → 对账。

用法：
    python pg_sync.py --dsn "postgresql://postgres@127.0.0.1:5432/postgres" init
    python pg_sync.py --dsn ... etl
    python pg_sync.py --dsn ... verify
    python pg_sync.py --dsn ... all

说明：
  * DDL 取自本目录 `schema.sql` 的「A. 已落地」段（幂等，可重复执行）；
  * 迁移为**全量覆盖式**：按业务主键 upsert，重复执行不产生重复数据；
  * 运行时表都缺 `tenant_id`，由 store_config.json 的 tenant_id 注入；
  * SQLite 的 `target_json` / `evidence_required_json` / `before_json` / `after_json` 会转成 jsonb；
  * SQLite 的 naive 时间戳按门店时区（默认 +08:00）解释后写入 timestamptz。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent
SCHEMA_FILE = ROOT / "schema.sql"
SHOP_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class TableSpec:
    """一张运行时表 → PostgreSQL 表的迁移规格。"""

    name: str                       # PostgreSQL 表名
    source: str                     # SQLite 表名
    columns: tuple[str, ...]        # 同名列（源 → 目标）
    conflict: tuple[str, ...]       # upsert 依据
    db: str = "ops"                 # 数据来源：ops / metrics / chat
    inject_tenant: bool = True      # 是否注入 tenant_id
    rename: dict[str, str] = field(default_factory=dict)     # 源列 → 目标列
    json_columns: tuple[str, ...] = ()                       # 需转 jsonb 的目标列
    timestamp_columns: tuple[str, ...] = ()                  # 需按门店时区解释
    date_columns: tuple[str, ...] = ()
    boolean_columns: tuple[str, ...] = ()                    # SQLite 的 0/1 → PostgreSQL boolean
    copy_id: bool = False           # 是否保留自增 id（并修正序列）
    replace: bool = False           # 追加型表（无自然唯一键）：按租户清空后重灌，保证幂等
    order_by: str = ""


TABLES: tuple[TableSpec, ...] = (
    # columns 必须列出目标表的"全部非自增列"（含 NOT NULL 时间列与 jsonb 列）；
    # timestamp_columns / json_columns / date_columns 只是类型转换提示。
    TableSpec("platform_metrics", "platform_metrics",
              ("store_id", "metric_date", "platform", "impressions", "detail_views", "paid_orders",
               "verified_orders", "gmv", "refunds", "review_count", "negative_review_count", "avg_rating",
               "source_type", "source_name", "imported_at"),
              ("tenant_id", "store_id", "metric_date", "platform"), db="metrics",
              timestamp_columns=("imported_at",), date_columns=("metric_date",)),
    TableSpec("package_metrics", "package_metrics",
              ("store_id", "metric_date", "platform", "package_id", "package_name", "detail_views",
               "paid_orders", "verified_orders", "gmv", "source_name", "imported_at"),
              ("tenant_id", "store_id", "metric_date", "platform", "package_id"), db="metrics",
              timestamp_columns=("imported_at",), date_columns=("metric_date",)),
    TableSpec("reviews", "reviews",
              ("store_id", "platform", "review_id", "rating", "created_at", "status", "category",
               "replied_at", "source_name", "imported_at"),
              ("tenant_id", "store_id", "platform", "review_id"), db="metrics",
              timestamp_columns=("created_at", "replied_at", "imported_at")),
    TableSpec("metric_import_batches", "metric_import_batches",
              ("id", "store_id", "platform", "source_name", "checksum", "rows_read", "rows_written",
               "min_date", "max_date", "status", "imported_at", "dataset"),
              ("tenant_id", "store_id", "checksum"), db="metrics", copy_id=True,
              timestamp_columns=("imported_at",), date_columns=("min_date", "max_date")),

    TableSpec("action_tasks", "action_tasks",
              # 列名与运行时一致（批次 8b 起 schema.sql 不再重命名 JSON 列），jsonb 类型由 PG 侧保证
              ("task_id", "store_id", "source_fingerprint", "category", "title", "assignee_id", "priority",
               "status", "target_json", "reason", "evidence_required_json", "approval_required",
               "due_at", "created_at", "updated_at"),
              ("task_id",),
              json_columns=("target_json", "evidence_required_json"),
              timestamp_columns=("due_at", "created_at", "updated_at"),
              boolean_columns=("approval_required",)),
    TableSpec("task_evidence", "task_evidence",
              ("id", "task_id", "evidence_type", "object_ref", "note", "submitted_by", "submitted_at"),
              (), copy_id=True, replace=True,
              timestamp_columns=("submitted_at",)),
    TableSpec("task_events", "task_events",
              ("id", "task_id", "from_status", "to_status", "actor_id", "reason", "created_at"),
              (), copy_id=True, replace=True,
              timestamp_columns=("created_at",)),

    TableSpec("users", "users",
              ("user_id", "username", "display_name", "password_algo", "password_salt", "password_hash",
               "status", "created_at", "password_changed_at", "last_login_at"),
              ("user_id",),
              timestamp_columns=("created_at", "password_changed_at", "last_login_at")),
    TableSpec("memberships", "memberships",
              ("store_id", "user_id", "role", "status", "created_at"),
              ("tenant_id", "store_id", "user_id"),
              timestamp_columns=("created_at",)),
    TableSpec("auth_sessions", "auth_sessions",
              ("token_hash", "user_id", "store_id", "role", "created_at", "expires_at", "last_seen_at",
               "revoked_at", "revoked_reason"),
              ("token_hash",),
              timestamp_columns=("created_at", "expires_at", "last_seen_at", "revoked_at")),
    TableSpec("login_attempts", "login_attempts",
              ("scope_key", "fail_count", "first_failed_at", "last_failed_at", "locked_until"),
              ("scope_key",), inject_tenant=False,
              timestamp_columns=("first_failed_at", "last_failed_at", "locked_until")),
    TableSpec("audit_events", "audit_events",
              ("id", "store_id", "actor_type", "actor_id", "event_type", "object_type", "object_id",
               "before_json", "after_json", "request_id", "created_at"),
              (), copy_id=True, replace=True,
              json_columns=("before_json", "after_json"),
              timestamp_columns=("created_at",)),

    TableSpec("approvals", "approvals",
              ("approval_id", "store_id", "task_id", "action_type", "payload_snapshot_json", "status",
               "requested_by", "requested_at", "decided_by", "decided_at", "decision_reason",
               "idempotency_key"),
              ("approval_id",), json_columns=("payload_snapshot_json",),
              timestamp_columns=("requested_at", "decided_at")),
    TableSpec("agent_tool_runs", "agent_tool_runs",
              ("run_id", "store_id", "session_id", "tool_name", "mode", "actor_id",
               "input_redacted_json", "output_redacted_json", "status", "error", "created_at",
               "finished_at"),
              ("run_id",), json_columns=("input_redacted_json", "output_redacted_json"),
              timestamp_columns=("created_at", "finished_at")),

    TableSpec("webhook_nonces", "webhook_nonces",
              ("nonce", "source", "received_at"), ("nonce",), inject_tenant=False,
              timestamp_columns=("received_at",)),
    TableSpec("notifications", "notifications",
              ("notification_id", "store_id", "kind", "window_key", "channel", "status",
               "detail_json", "created_at"),
              ("notification_id",), json_columns=("detail_json",),
              timestamp_columns=("created_at")),
    TableSpec("agent_sessions", "agent_sessions",
              ("session_id", "user_id", "user_role", "created_at", "last_message_at"),
              ("session_id",),
              timestamp_columns=("created_at", "last_message_at")),
    TableSpec("agent_messages", "agent_messages",
              ("id", "session_id", "user_id", "message_role", "content", "intent", "created_at"),
              (), db="chat", copy_id=True, replace=True,
              timestamp_columns=("created_at",)),
)


def pg_required_columns(pg: psycopg.Connection, table: str) -> set[str]:
    """取目标表中"非空且无默认值且非自增"的列，用于校验迁移列是否漏配。"""
    with pg.cursor() as cur:
        cur.execute(
            """select column_name from information_schema.columns
               where table_name = %s and is_nullable = 'NO'
                 and column_default is null and is_identity = 'NO' and is_generated = 'NEVER'""",
            (table,),
        )
        return {row[0] for row in cur.fetchall()}


def read_schema_section_a() -> list[str]:
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    marker = "-- B. 规划中"
    if marker in text:
        text = text.split(marker)[0]
    statements = [part.strip() for part in text.split(";")]
    # 保留所有含真实 SQL 的语句（RLS 的 alter/drop policy 也必须执行），跳过纯注释块
    def has_sql(part: str) -> bool:
        return any(line.strip() and not line.strip().startswith("--") for line in part.splitlines())

    return [part for part in statements if part and has_sql(part)]


def _to_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHOP_TZ)


def _to_jsonb(value: Any) -> Optional[Jsonb]:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return Jsonb(value)
    try:
        return Jsonb(json.loads(str(value)))
    except json.JSONDecodeError:
        return Jsonb({"raw": str(value)})


def connect_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_rows(conn: sqlite3.Connection, table: str, columns: Iterable[str]) -> list[dict[str, Any]]:
    cols = ", ".join(columns)
    return [dict(row) for row in conn.execute(f"select {cols} from {table}")]


def migrate_table(
    pg: psycopg.Connection,
    spec: TableSpec,
    sources: dict[str, sqlite3.Connection],
    tenant_id: str,
) -> int:
    conn = sources[spec.db]
    source_columns = list(spec.columns) + list(spec.rename)
    if spec.copy_id:
        source_columns = ["id"] + [c for c in source_columns if c != "id"]
    if not sqlite_has_table(conn, spec.source):
        return 0
    existing = {row["name"] for row in conn.execute(f"pragma table_info({spec.source})")}
    source_columns = [column for column in source_columns if column in existing]
    rows = sqlite_rows(conn, spec.source, source_columns)

    target_columns: list[str] = []
    if spec.inject_tenant:
        target_columns.append("tenant_id")
    for column in source_columns:
        target_columns.append(spec.rename.get(column, column))

    placeholders = ", ".join(["%s"] * len(target_columns))
    column_list = ", ".join(target_columns)
    updates = ", ".join(
        f"{column} = excluded.{column}" for column in target_columns if column not in spec.conflict
    )
    statement = f"insert into {spec.name} ({column_list}) values ({placeholders})"
    if not spec.replace:
        if updates:
            statement += f" on conflict ({', '.join(spec.conflict)}) do update set {updates}"
        else:
            statement += " on conflict do nothing"

    missing = pg_required_columns(pg, spec.name) - set(target_columns)
    if missing:
        raise SystemExit(f"{spec.name} 迁移列不完整，缺少非空列：{sorted(missing)}")

    with pg.cursor() as cur:
        if spec.replace:
            if spec.inject_tenant:
                cur.execute(f"delete from {spec.name} where tenant_id = %s", (tenant_id,))
            else:
                cur.execute(f"delete from {spec.name}")
        for row in rows:
            values: list[Any] = []
            if spec.inject_tenant:
                values.append(tenant_id)
            for column in source_columns:
                target = spec.rename.get(column, column)
                value = row[column]
                if target in spec.json_columns:
                    value = _to_jsonb(value)
                elif target in spec.timestamp_columns:
                    value = _to_timestamp(value)
                elif target in spec.boolean_columns:
                    value = None if value is None else bool(value)
                values.append(value)
            cur.execute(statement, values)

        if spec.copy_id:
            sequence = spec.name + "_id_seq"
            cur.execute(
                "select setval(%s, coalesce((select max(id) from " + spec.name + "), 1))",
                (sequence,),
            )
    pg.commit()
    return len(rows)


def seed_store(pg: psycopg.Connection, config: dict[str, Any]) -> None:
    with pg.cursor() as cur:
        cur.execute(
            """insert into stores (tenant_id, store_id, name, timezone, status)
               values (%s, %s, %s, %s, %s)
               on conflict (tenant_id, store_id) do update set name = excluded.name""",
            (config["tenant_id"], config["store_id"], config.get("name", config["store_id"]),
             config.get("timezone", "Asia/Shanghai"), "pilot"),
        )
        cur.execute(
            # 列名与运行时一致（批次 8c 起 schema.sql 用 config_json，与 SQLite 对齐）
            """insert into store_configs (tenant_id, store_id, config_json, updated_at)
               values (%s, %s, %s, now())
               on conflict (tenant_id, store_id) do update set
                 config_json = excluded.config_json, updated_at = now()""",
            (config["tenant_id"], config["store_id"], Jsonb(config)),
        )
    pg.commit()


def sqlite_has_table(conn: sqlite3.Connection, table: str) -> bool:
    """源库可能还没有某张表（例如刚上线治理模块但还没产生审批单）。"""
    rows = conn.execute(
        "select name from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    return rows is not None


def count_sqlite(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"select count(*) from {table}").fetchone()[0])


def count_pg(pg: psycopg.Connection, table: str, tenant_id: str) -> int:
    with pg.cursor() as cur:
        if table == "login_attempts":
            cur.execute(f"select count(*) from {table}")
        else:
            cur.execute(f"select count(*) from {table} where tenant_id = %s", (tenant_id,))
        return int(cur.fetchone()[0])


def sum_pg(pg: psycopg.Connection, sql: str, params: tuple = ()) -> Any:
    with pg.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def ts_list_open(path: Path, store_id: str) -> list[str]:
    if not path.exists():
        return []
    conn = connect_sqlite(path)
    try:
        rows = conn.execute(
            """select task_id from action_tasks where store_id=?
               and status in ('proposed','ready','accepted','overdue')""", (store_id,)).fetchall()
        return [row["task_id"] for row in rows]
    finally:
        conn.close()


def ts_list_verified(path: Path, store_id: str) -> list[str]:
    if not path.exists():
        return []
    conn = connect_sqlite(path)
    try:
        rows = conn.execute(
            "select task_id from action_tasks where store_id=? and status='verified'", (store_id,)).fetchall()
        return [row["task_id"] for row in rows]
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL 落地工具")
    parser.add_argument("--dsn", required=True, help="例如 postgresql://postgres@127.0.0.1:5432/postgres")
    parser.add_argument("--config", type=Path, default=ROOT / "store_config.json")
    parser.add_argument("--metrics-db", type=Path, default=ROOT / "data" / "platform_metrics.db")
    parser.add_argument("--ops-db", type=Path, default=ROOT / "data" / "store_ops.db")
    parser.add_argument("--chat-db", type=Path, default=ROOT / "data" / "agent_demo.db")
    parser.add_argument("--only", default=None, help="只处理指定表（逗号分隔），用于排查")
    # 子命令内也接受 --dsn / --only：default=SUPPRESS 保证不覆盖全局值
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dsn", default=argparse.SUPPRESS)
    common.add_argument("--only", default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("init", "建表（执行 schema.sql 的已落地段）"),
                            ("etl", "从 SQLite 迁移数据"),
                            ("verify", "对账 SQLite 与 PostgreSQL"),
                            ("all", "init + etl + verify")):
        sub.add_parser(name, parents=[common], help=help_text)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    tenant_id = config["tenant_id"]
    payload: dict[str, Any] = {"dsn": args.dsn.split("@")[-1], "tenant_id": tenant_id, "store_id": config["store_id"]}

    with psycopg.connect(args.dsn, autocommit=False) as pg:
        if args.command in ("init", "all"):
            statements = read_schema_section_a()
            with pg.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
            pg.commit()
            payload["ddl_statements"] = len(statements)

        if args.command in ("etl", "all"):
            sources = {
                "metrics": connect_sqlite(args.metrics_db),
                "ops": connect_sqlite(args.ops_db),
                "chat": connect_sqlite(args.chat_db),
            }
            only = {name.strip() for name in args.only.split(",")} if args.only else None
            seed_store(pg, config)
            migrated: dict[str, int] = {}
            for spec in TABLES:
                if only and spec.name not in only:
                    continue
                source_path = {"metrics": args.metrics_db, "ops": args.ops_db, "chat": args.chat_db}[spec.db]
                if not source_path.exists():
                    continue
                migrated[spec.name] = migrate_table(pg, spec, sources, tenant_id)
            for conn in sources.values():
                conn.close()
            payload["migrated_rows"] = migrated

        if args.command in ("verify", "all"):
            sources = {
                "metrics": connect_sqlite(args.metrics_db),
                "ops": connect_sqlite(args.ops_db),
                "chat": connect_sqlite(args.chat_db),
            }
            checks: list[dict[str, Any]] = []
            for spec in TABLES:
                source_path = {"metrics": args.metrics_db, "ops": args.ops_db, "chat": args.chat_db}[spec.db]
                if not source_path.exists() or not sqlite_has_table(sources[spec.db], spec.source):
                    continue
                expected = count_sqlite(sources[spec.db], spec.source)
                actual = count_pg(pg, spec.name, tenant_id)
                checks.append({"table": spec.name, "sqlite": expected, "postgres": actual,
                               "ok": expected == actual})
            payload["row_counts"] = checks
            payload["row_counts_ok"] = all(item["ok"] for item in checks)

            # 业务口径对账：SQLite 侧走平台库 API，PG 侧直接 SQL，两边必须一致
            from platform_data import PlatformMetricStore
            metrics_api = PlatformMetricStore(args.metrics_db)
            # 全量读取（DataQuality/PeriodSummary 有 90/365 天上限，对账要用穷尽区间）
            all_rows = metrics_api.read_daily_metrics(config["store_id"], "1900-01-01", "2999-12-31")
            sqlite_gmv = sum(float(row["gmv"] or 0) for row in all_rows)
            sqlite_paid = sum(int(row["paid_orders"] or 0) for row in all_rows)
            sqlite_negative = sum(int(row["negative_review_count"] or 0) for row in all_rows)
            pg_gmv = float(sum_pg(pg, "select coalesce(sum(gmv),0) from platform_metrics where tenant_id=%s", (tenant_id,)))
            pg_paid = int(sum_pg(pg, "select coalesce(sum(paid_orders),0) from platform_metrics where tenant_id=%s", (tenant_id,)))
            pg_negative = int(sum_pg(pg, "select coalesce(sum(negative_review_count),0) from platform_metrics where tenant_id=%s", (tenant_id,)))
            pg_rows = int(sum_pg(pg, "select count(*) from platform_metrics where tenant_id=%s", (tenant_id,)))
            pg_open_negatives = int(sum_pg(
                pg, "select count(*) from reviews where tenant_id=%s and status='open' and rating<=2", (tenant_id,)))
            sqlite_open_negatives = len([
                row for row in metrics_api.read_reviews(config["store_id"])
                if row["rating"] is not None and float(row["rating"]) <= 2 and row["status"] == "open"
            ])
            sqlite_open_tasks = len(ts_list_open(args.ops_db, config["store_id"]))
            pg_open_tasks = int(sum_pg(
                pg, """select count(*) from action_tasks where tenant_id=%s
                       and status in ('proposed','ready','accepted','overdue')""", (tenant_id,)))
            pg_verified_tasks = int(sum_pg(
                pg, "select count(*) from action_tasks where tenant_id=%s and status='verified'", (tenant_id,)))
            sqlite_verified_tasks = len(ts_list_verified(args.ops_db, config["store_id"]))
            payload["business_checks"] = {
                "gmv": {"sqlite": round(sqlite_gmv, 2), "postgres": round(pg_gmv, 2),
                        "ok": abs(sqlite_gmv - pg_gmv) < 0.01},
                "paid_orders": {"sqlite": sqlite_paid, "postgres": pg_paid, "ok": sqlite_paid == pg_paid},
                "negative_reviews": {"sqlite": sqlite_negative, "postgres": pg_negative,
                                     "ok": sqlite_negative == pg_negative},
                "platform_rows": {"sqlite": len(all_rows), "postgres": pg_rows, "ok": len(all_rows) == pg_rows},
                "open_negative_reviews": {"sqlite": sqlite_open_negatives, "postgres": pg_open_negatives,
                                          "ok": sqlite_open_negatives == pg_open_negatives},
                "open_tasks": {"sqlite": sqlite_open_tasks, "postgres": pg_open_tasks,
                               "ok": sqlite_open_tasks == pg_open_tasks},
                "verified_tasks": {"sqlite": sqlite_verified_tasks, "postgres": pg_verified_tasks,
                                   "ok": sqlite_verified_tasks == pg_verified_tasks},
            }
            payload["business_checks_ok"] = all(
                item.get("ok", True) for item in payload["business_checks"].values() if isinstance(item, dict)
            )
            for conn in sources.values():
                conn.close()

    payload["ok"] = bool(payload.get("row_counts_ok", True)) and bool(payload.get("business_checks_ok", True))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if not payload["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
