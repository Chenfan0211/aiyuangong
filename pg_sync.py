#!/usr/bin/env python3
"""PostgreSQL 落地工具（二期批次 8）：建表 → 从 SQLite 迁移数据 → 对账。

用法：
    python pg_sync.py --dsn "postgresql://postgres@127.0.0.1:5432/postgres" init
    python pg_sync.py --dsn ... etl
    python pg_sync.py --dsn ... verify
    python pg_sync.py --dsn ... all

版本化迁移（正式生产升级入口，直接跑 init 只用于本地/首次建库）：
    python pg_sync.py --dsn ... migrate --status     # 查看已应用 / 待应用版本（只读）
    python pg_sync.py --dsn ... migrate --dry-run    # 只打印将要执行的迁移，不改库
    python pg_sync.py --dsn ... migrate --apply      # 应用（advisory lock + 恢复点 + 单事务，失败整体回滚）
    python pg_sync.py --dsn ... migrate --verify     # 校验版本、校验和与已落地表

说明：
  * DDL 取自本目录 `schema.sql` 的「A. 已落地」段（幂等，可重复执行）；
  * 迁移为**全量覆盖式**：按业务主键 upsert，重复执行不产生重复数据；
  * 运行时表都缺 `tenant_id`，由 store_config.json 的 tenant_id 注入；
  * SQLite 的 `target_json` / `evidence_required_json` / `before_json` / `after_json` 会转成 jsonb；
  * SQLite 的 naive 时间戳按门店时区（默认 +08:00）解释后写入 timestamptz。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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

# ---------- 迁移版本化（migrate 子命令）----------
# schema.sql 的 A0 段：迁移账本（版本记录 + 持锁记录），先于版本化迁移创建
MIGRATION_TABLE_MARKER = "-- A0. 迁移记录"
MIGRATION_SECTION_END = "-- A. 已落地"
MIGRATION_LOCK_KEY = 7534221901115001          # pg_advisory_lock 的 64 位键，全局唯一，勿修改
RECOVERY_DIR = ROOT / "data" / "backups" / "migrations"
EXIT_OK = 0
EXIT_LOCKED = 3                                # 抢不到迁移锁：本次不做任何改动
EXIT_NO_RECOVERY_POINT = 4                     # 恢复点写不出来：拒绝开始迁移
EXIT_MIGRATION_FAILED = 5                      # 迁移失败（已整体回滚）
EXIT_VERIFY_FAILED = 6                         # verify 发现版本/校验和/结构问题


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
               "status", "created_at", "password_changed_at", "last_login_at", "must_change_password"),
              ("user_id",),
              timestamp_columns=("created_at", "password_changed_at", "last_login_at"),
              boolean_columns=("must_change_password",)),
    TableSpec("memberships", "memberships",
              ("store_id", "user_id", "role", "status", "created_at"),
              ("tenant_id", "store_id", "user_id"),
              timestamp_columns=("created_at",)),
    TableSpec("channel_bindings", "channel_bindings",
              ("binding_id", "store_id", "provider", "external_user_id", "display_name",
               "mobile_hash", "mobile_masked", "user_id", "status", "created_by",
               "created_at", "updated_at"),
              ("binding_id",),
              timestamp_columns=("created_at", "updated_at")),
    TableSpec("auth_sessions", "auth_sessions",
              ("token_hash", "user_id", "store_id", "role", "created_at", "expires_at",
               "absolute_expires_at", "last_seen_at", "revoked_at", "revoked_reason"),
              ("token_hash",),
              timestamp_columns=("created_at", "expires_at", "absolute_expires_at", "last_seen_at", "revoked_at")),
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


def sql_statements(text: str) -> list[str]:
    """按分号切出 SQL 片段，保留含真实语句的片段、丢弃纯注释块。"""
    def has_sql(part: str) -> bool:
        return any(line.strip() and not line.strip().startswith("--") for line in part.splitlines())

    return [part.strip() for part in text.split(";") if part.strip() and has_sql(part)]


def read_schema_section_a() -> list[str]:
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    marker = "-- B. 规划中"
    if marker in text:
        text = text.split(marker)[0]
    # 保留所有含真实 SQL 的语句（RLS 的 alter/drop policy 也必须执行），跳过纯注释块
    return sql_statements(text)


def read_schema_migration_section() -> list[str]:
    """取 schema.sql 的 A0 段（迁移账本两张表），作为 migrate 的 bootstrap DDL。"""
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    if MIGRATION_TABLE_MARKER not in text:
        raise SystemExit(f"schema.sql 缺少迁移账本段标记：{MIGRATION_TABLE_MARKER}")
    start = text.index(MIGRATION_TABLE_MARKER)
    end = text.index(MIGRATION_SECTION_END, start)
    return sql_statements(text[start:end])


@dataclass(frozen=True)
class Migration:
    """一次版本化迁移。

    * `statements`：要执行的 DDL；
    * `adopt_if_present`：历史库（已用 init 建好结构、但还没有版本行）认领基线用的探测表 ——
      该表已存在时只登记版本号、不重跑 DDL；
    * `requires_tables`：verify 时要求存在的表；
    * `checksum_tracked=False`：语句随 schema.sql 演进（基线迁移就是如此），校验和只作漂移提示，
      不参与 verify 的失败判定。
    """

    version: int
    name: str
    statements: tuple[str, ...] = ()
    adopt_if_present: str = ""
    requires_tables: tuple[str, ...] = ()
    checksum_tracked: bool = True


def tables_in_statements(statements: Iterable[str]) -> tuple[str, ...]:
    """从 DDL 里抽出 `create table if not exists <name>` 的表名（去重、保序）。"""
    names = re.findall(r"create table if not exists\s+([a-z0-9_]+)",
                       "\n".join(statements), flags=re.IGNORECASE)
    return tuple(dict.fromkeys(name.lower() for name in names))


def migration_checksum(migration: Migration) -> str:
    payload = json.dumps(
        {"version": migration.version, "name": migration.name, "statements": list(migration.statements)},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_migrations() -> tuple[Migration, ...]:
    """版本化迁移清单（当前只有 v1 基线：schema.sql A 段的全部已落地结构）。

    新增迁移时在末尾追加 `Migration(version=N, name=..., statements=(...))`；
    已发布迁移的 version 与 statements 不得再改（改了 verify 会报校验和漂移）。
    """
    baseline = tuple(read_schema_section_a())
    return (
        Migration(version=1, name="baseline_landed_schema", statements=baseline,
                  adopt_if_present="stores", requires_tables=tables_in_statements(baseline),
                  checksum_tracked=False),
        Migration(
            version=2,
            name="auth_lifecycle_and_absolute_expiry",
            statements=(
                "alter table users add column if not exists must_change_password boolean not null default false",
                "alter table auth_sessions add column if not exists absolute_expires_at timestamptz",
                "update auth_sessions set absolute_expires_at=expires_at where absolute_expires_at is null",
                "alter table auth_sessions alter column absolute_expires_at set not null",
            ),
            requires_tables=("users", "auth_sessions"),
            checksum_tracked=True,
        ),        Migration(
            version=3,
            name="channel_bindings_and_inbound_identity",
            statements=(
                """create table if not exists channel_bindings (
                  binding_id text primary key,
                  tenant_id text not null default '',
                  store_id text not null,
                  provider text not null,
                  external_user_id text not null,
                  display_name text not null default '',
                  mobile_hash text not null default '',
                  mobile_masked text not null default '',
                  user_id text,
                  status text not null default 'active',
                  created_by text not null default '',
                  created_at timestamptz not null,
                  updated_at timestamptz not null,
                  unique (tenant_id, store_id, provider, external_user_id)
                )""",
                "create unique index if not exists ux_channel_bindings_mobile on channel_bindings (tenant_id, store_id, provider, mobile_hash) where mobile_hash <> ''",
                "create index if not exists idx_channel_bindings_user on channel_bindings (tenant_id, store_id, user_id, status)",
            ),
            requires_tables=("channel_bindings",),
            checksum_tracked=True,
        ),
    )


MIGRATIONS: tuple[Migration, ...] = load_migrations()


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


# ---------------- 迁移版本化：账本、锁、恢复点、执行 ----------------

def table_exists(pg: psycopg.Connection, table: str) -> bool:
    with pg.cursor() as cur:
        cur.execute("select to_regclass(%s) is not null", (f"public.{table}",))
        return bool(cur.fetchone()[0])


def read_migration_state(pg: psycopg.Connection) -> list[dict[str, Any]]:
    """读版本账本；账本表还不存在（空库 / dry-run）时返回空列表，不创建任何对象。"""
    if not table_exists(pg, "schema_migrations"):
        return []
    with pg.cursor() as cur:
        cur.execute("""select version, name, checksum, applied_at, adopted, applied_by
                       from schema_migrations order by version""")
        columns = [item.name for item in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def migration_lock_held(pg: psycopg.Connection) -> bool:
    """是否有别的会话持有迁移 advisory lock（只读 pg_locks，不加锁）。"""
    classid, objid = (MIGRATION_LOCK_KEY >> 32) & 0xFFFFFFFF, MIGRATION_LOCK_KEY & 0xFFFFFFFF
    with pg.cursor() as cur:
        cur.execute("""select exists (select 1 from pg_locks
                       where locktype = 'advisory' and classid::bigint = %s
                         and objid::bigint = %s and objsubid = 1)""", (classid, objid))
        return bool(cur.fetchone()[0])


def ensure_migration_tables(pg: psycopg.Connection) -> int:
    """创建迁移账本两张表（幂等；init 也会创建，但只有 apply 写版本行）。"""
    statements = read_schema_migration_section()
    with pg.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
    return len(statements)


def acquire_migration_lock(pg: psycopg.Connection) -> bool:
    """取迁移 advisory lock（不等待）；返回 False 表示已有迁移在跑，本次不得做任何改动。"""
    with pg.cursor() as cur:
        cur.execute("select pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        return bool(cur.fetchone()[0])


def release_migration_lock(pg: psycopg.Connection) -> None:
    try:
        with pg.cursor() as cur:
            cur.execute("select pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
        pg.commit()
    except Exception:                                  # pragma: no cover - 连接已断等极端情况
        pg.rollback()


def lock_holder(pg: psycopg.Connection) -> str:
    with pg.cursor() as cur:
        cur.execute("select current_user, pg_backend_pid()")
        user, pid = cur.fetchone()
    return f"pg_sync.py pid={pid} user={user}"


def record_lock_holder(pg: psycopg.Connection, holder: str) -> None:
    """把持锁人写进账本（advisory lock 的可见副本，便于排查“谁在迁移”）。"""
    with pg.cursor() as cur:
        cur.execute("""insert into schema_migration_lock (lock_key, locked_by, locked_at, released_at)
                       values (%s, %s, now(), null)
                       on conflict (lock_key) do update
                         set locked_by = excluded.locked_by, locked_at = excluded.locked_at,
                             released_at = null""", (MIGRATION_LOCK_KEY, holder))


def mark_lock_released(pg: psycopg.Connection) -> None:
    with pg.cursor() as cur:
        cur.execute("""update schema_migration_lock set released_at = now()
                       where lock_key = %s and released_at is null""", (MIGRATION_LOCK_KEY,))


def lock_ledger(pg: psycopg.Connection) -> dict[str, Any]:
    if not table_exists(pg, "schema_migration_lock"):
        return {}
    with pg.cursor() as cur:
        cur.execute("""select lock_key, locked_by, locked_at, released_at
                       from schema_migration_lock where lock_key = %s""", (MIGRATION_LOCK_KEY,))
        row = cur.fetchone()
    if not row:
        return {}
    return {"lock_key": row[0], "locked_by": row[1], "locked_at": str(row[2]),
            "released_at": str(row[3]) if row[3] is not None else None}


def schema_fingerprint(pg: psycopg.Connection) -> str:
    """public schema 的列级指纹（表 / 列 / 类型 / 可空），用于确认“失败后库没变”。"""
    with pg.cursor() as cur:
        cur.execute("""select table_name, column_name, data_type, is_nullable
                       from information_schema.columns
                       where table_schema = 'public'
                       order by table_name, column_name, data_type, is_nullable""")
        rows = cur.fetchall()
    blob = "\n".join("|".join(str(value) for value in row) for row in rows)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def table_row_estimates(pg: psycopg.Connection) -> dict[str, int]:
    """public schema 各表行数估算（pg_class.reltuples，避免对大表做全表 count）。"""
    with pg.cursor() as cur:
        cur.execute("""select c.relname, coalesce(c.reltuples, -1)::bigint
                       from pg_class c join pg_namespace n on n.oid = c.relnamespace
                       where n.nspname = 'public' and c.relkind = 'r'
                       order by c.relname""")
        return {row[0]: int(row[1]) for row in cur.fetchall()}


def should_adopt(pg: psycopg.Connection, migration: Migration,
                 applied: list[dict[str, Any]]) -> bool:
    """历史库认领基线：还没有任何版本行，而该迁移的探测表已经存在 → 只登记版本、不重跑 DDL。"""
    return (bool(migration.adopt_if_present) and not applied
            and table_exists(pg, migration.adopt_if_present))


def pending_summary(pg: psycopg.Connection, migration: Migration,
                    applied: list[dict[str, Any]]) -> dict[str, Any]:
    adoptable = should_adopt(pg, migration, applied)
    return {"version": migration.version, "name": migration.name,
            "action": "adopt" if adoptable else "apply", "adoptable": adoptable,
            "statements": 0 if adoptable else len(migration.statements),
            "tables": list(migration.requires_tables)}


def write_recovery_manifest(pg: psycopg.Connection, *, pending: list[Migration],
                            applied: list[dict[str, Any]], recovery_dir: Path,
                            target: str) -> dict[str, Any]:
    """apply 前写恢复点清单：必须在任何 DDL 之前落盘，写不出来就不允许开始迁移。

    回滚保障本身来自“整批迁移在单个事务内执行、失败整体回滚”（PostgreSQL 支持事务性 DDL）；
    清单记录改动前的版本、结构指纹与各表行数估算，供人工恢复与灾备核对。
    """
    recovery_dir = Path(recovery_dir)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "strategy": "transactional_ddl",
        "atomic": True,
        "current_version": applied[-1]["version"] if applied else 0,
        "target_version": pending[-1].version,
        "applied": [{"version": row["version"], "name": row["name"]} for row in applied],
        "planned": [{"version": item.version, "name": item.name,
                     "action": "adopt" if should_adopt(pg, item, applied) else "apply",
                     "statements": len(item.statements)} for item in pending],
        "schema_fingerprint": schema_fingerprint(pg),
        "inventory": {"tables": table_row_estimates(pg),
                      "row_count_note": "pg_class.reltuples 估算值，不是精确 count"},
        "rollback": ("本次 apply 是单事务（PostgreSQL 事务性 DDL），失败会整体回滚到本清单状态；"
                     "已提交迁移的回滚以迁移前备份恢复为准，不提供自动 down migration"),
    }
    blob = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    record["sha256"] = digest
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = recovery_dir / f"{stamp}-pre-v{record['target_version']}.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2),
                        encoding="utf-8")
    tmp_path.replace(path)
    return {"manifest": str(path), "sha256": digest, "strategy": "transactional_ddl", "atomic": True}


def verify_report(pg: psycopg.Connection, migrations: tuple[Migration, ...],
                  applied: list[dict[str, Any]], pending: list[Migration]) -> dict[str, Any]:
    known = {migration.version for migration in migrations}
    mismatches: list[dict[str, Any]] = []
    for migration in migrations:
        recorded = next((row["checksum"] for row in applied if row["version"] == migration.version), None)
        if recorded is None or not migration.checksum_tracked:
            continue
        expected_checksum = migration_checksum(migration)
        if recorded != expected_checksum:
            mismatches.append({"version": migration.version, "name": migration.name,
                               "recorded": recorded, "expected": expected_checksum})
    applied_versions = {row["version"] for row in applied}
    missing_tables = sorted({table for migration in migrations if migration.version in applied_versions
                             for table in migration.requires_tables if not table_exists(pg, table)})
    unknown = sorted(version for version in applied_versions if version not in known)
    ok = not pending and not mismatches and not missing_tables and not unknown
    return {"ok": ok, "mode": "verify", "up_to_date": not pending,
            "current_version": applied[-1]["version"] if applied else 0,
            "expected_version": migrations[-1].version,
            "applied": applied,
            "missing": [{"version": item.version, "name": item.name} for item in pending],
            "pending": [{"version": item.version, "name": item.name} for item in pending],
            "unknown_applied": unknown, "checksum_mismatches": mismatches,
            "missing_tables": missing_tables, "schema_fingerprint": schema_fingerprint(pg),
            "locked": migration_lock_held(pg)}


def apply_migrations(pg: psycopg.Connection, migrations: tuple[Migration, ...],
                     pending: list[Migration], applied: list[dict[str, Any]], *,
                     recovery_dir: Path, target: str) -> tuple[dict[str, Any], int]:
    """应用待执行迁移：恢复点 → 锁 → 账本 → 单事务执行，任一步失败都不继续。"""
    expected = migrations[-1].version
    current = applied[-1]["version"] if applied else 0
    if not pending:
        return ({"ok": True, "mode": "apply", "applied_now": [], "applied": applied,
                 "current_version": current, "expected_version": expected,
                 "up_to_date": True, "recovery": None}, EXIT_OK)

    if not acquire_migration_lock(pg):
        return ({"ok": False, "mode": "apply", "reason": "migration_locked",
                 "error": "另一个迁移进程持有 advisory lock，本次不做任何改动",
                 "lock_key": MIGRATION_LOCK_KEY, "current_version": current,
                 "expected_version": expected, "up_to_date": False}, EXIT_LOCKED)
    try:
        # 1) 恢复点：任何 DDL 之前先写成功，否则直接拒绝开始
        try:
            recovery = write_recovery_manifest(pg, pending=pending, applied=applied,
                                               recovery_dir=recovery_dir, target=target)
        except Exception as exc:
            pg.rollback()
            return ({"ok": False, "mode": "apply", "reason": "recovery_point_failed",
                     "error": f"{type(exc).__name__}: {exc}", "recovery": None,
                     "current_version": current, "expected_version": expected,
                     "up_to_date": False}, EXIT_NO_RECOVERY_POINT)

        # 2) 账本表 + 持锁人（幂等，独立提交，失败后也能看出“谁在迁移”）
        try:
            ensure_migration_tables(pg)
            record_lock_holder(pg, lock_holder(pg))
            pg.commit()
        except Exception as exc:
            pg.rollback()
            return ({"ok": False, "mode": "apply", "reason": "bootstrap_failed",
                     "error": f"{type(exc).__name__}: {exc}", "recovery": recovery,
                     "current_version": current, "expected_version": expected,
                     "up_to_date": False}, EXIT_MIGRATION_FAILED)

        # 3) 迁移：整批在一个事务里执行，任一步失败即整体回滚，并停止后续迁移
        applied_now: list[dict[str, Any]] = []
        migration = pending[0]
        try:
            for migration in pending:
                adopt = should_adopt(pg, migration, applied)
                if not adopt:
                    with pg.cursor() as cur:
                        for statement in migration.statements:
                            cur.execute(statement)
                with pg.cursor() as cur:
                    cur.execute("""insert into schema_migrations
                                     (version, name, checksum, adopted, applied_by)
                                   values (%s, %s, %s, %s, current_user)""",
                                (migration.version, migration.name,
                                 migration_checksum(migration), adopt))
                applied_now.append({"version": migration.version, "name": migration.name,
                                    "adopted": adopt,
                                    "statements": 0 if adopt else len(migration.statements)})
            pg.commit()
        except Exception as exc:
            pg.rollback()
            return ({"ok": False, "mode": "apply", "reason": "migration_failed",
                     "error": f"{type(exc).__name__}: {exc}",
                     "failed_version": migration.version, "failed_name": migration.name,
                     "recovery": recovery, "recovered": True,
                     "error_hint": "整批迁移已整体回滚（业务表不变）；核对恢复点清单后可重试",
                     "current_version": current, "expected_version": expected,
                     "up_to_date": False}, EXIT_MIGRATION_FAILED)

        # 4) 结果以账本为准（applied_at 取库内真实值）
        state = read_migration_state(pg)
        rows = {row["version"]: row for row in state}
        for item in applied_now:
            item["applied_at"] = str(rows.get(item["version"], {}).get("applied_at", ""))
        return ({"ok": True, "mode": "apply", "applied_now": applied_now, "applied": state,
                 "current_version": state[-1]["version"] if state else current,
                 "expected_version": expected, "up_to_date": True, "recovery": recovery,
                 "lock": {"key": MIGRATION_LOCK_KEY, "holder": lock_holder(pg)},
                 "schema_fingerprint": schema_fingerprint(pg)}, EXIT_OK)
    finally:
        try:
            mark_lock_released(pg)
            pg.commit()
        except Exception:                              # pragma: no cover - 释放记录失败不回滚迁移结果
            pg.rollback()
        release_migration_lock(pg)


def run_migrations(pg: psycopg.Connection, *, mode: str,
                   migrations: Optional[tuple[Migration, ...]] = None,
                   recovery_dir: Optional[Path] = None,
                   target: str = "") -> tuple[dict[str, Any], int]:
    """执行迁移动作，返回 (payload, exit_code)。mode: status / dry-run / apply / verify。"""
    migrations = tuple(migrations) if migrations is not None else MIGRATIONS
    if not migrations:
        raise SystemExit("没有可用的迁移定义")
    known = {migration.version for migration in migrations}
    applied = read_migration_state(pg)
    applied_versions = {row["version"] for row in applied}
    pending = [migration for migration in migrations if migration.version not in applied_versions]
    current = applied[-1]["version"] if applied else 0
    expected = migrations[-1].version

    if mode == "status":
        return ({"ok": True, "mode": "status", "current_version": current,
                 "expected_version": expected, "up_to_date": not pending, "applied": applied,
                 "pending": [pending_summary(pg, migration, applied) for migration in pending],
                 "unknown_applied": sorted(v for v in applied_versions if v not in known),
                 "locked": migration_lock_held(pg), "lock_ledger": lock_ledger(pg)}, EXIT_OK)
    if mode == "dry-run":
        return ({"ok": True, "mode": "dry-run", "current_version": current,
                 "expected_version": expected, "up_to_date": not pending,
                 "would_write": bool(pending),
                 "planned": [pending_summary(pg, migration, applied) for migration in pending],
                 "applied": applied, "locked": migration_lock_held(pg),
                 "recovery_dir": str(recovery_dir or RECOVERY_DIR)}, EXIT_OK)
    if mode == "verify":
        report = verify_report(pg, migrations, applied, pending)
        return report, (EXIT_OK if report["ok"] else EXIT_VERIFY_FAILED)
    if mode == "apply":
        return apply_migrations(pg, migrations, pending, applied,
                                recovery_dir=Path(recovery_dir or RECOVERY_DIR), target=target)
    raise SystemExit(f"未知的迁移模式：{mode}")

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
    migrate_parser = sub.add_parser(
        "migrate", parents=[common], help="版本化迁移：--status / --dry-run / --apply / --verify")
    migrate_mode = migrate_parser.add_mutually_exclusive_group(required=True)
    migrate_mode.add_argument("--status", action="store_true", help="查看已应用 / 待应用版本（只读）")
    migrate_mode.add_argument("--dry-run", action="store_true", help="打印将要执行的迁移，不改库")
    migrate_mode.add_argument("--apply", action="store_true",
                              help="应用待执行迁移（advisory lock + 恢复点 + 单事务）")
    migrate_mode.add_argument("--verify", action="store_true", help="校验版本、校验和与已落地表")
    migrate_parser.add_argument("--recovery-dir", type=Path, default=RECOVERY_DIR,
                                help="apply 前写恢复点清单的目录（默认 data/backups/migrations）")
    args = parser.parse_args()
    migrate_action = next((name.replace("_", "-") for name in ("status", "dry_run", "apply", "verify")
                           if getattr(args, name, False)), "")

    config = json.loads(args.config.read_text(encoding="utf-8"))
    tenant_id = config["tenant_id"]
    payload: dict[str, Any] = {"dsn": args.dsn.split("@")[-1], "tenant_id": tenant_id, "store_id": config["store_id"]}

    exit_code = EXIT_OK
    with psycopg.connect(args.dsn, autocommit=False) as pg:
        if args.command == "migrate":
            result, exit_code = run_migrations(pg, mode=migrate_action,
                                               recovery_dir=args.recovery_dir,
                                               target=args.dsn.split("@")[-1])
            payload.update(result)

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

    if args.command != "migrate":
        payload["ok"] = (bool(payload.get("row_counts_ok", True))
                         and bool(payload.get("business_checks_ok", True)))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if args.command == "migrate":
        if exit_code != EXIT_OK:
            raise SystemExit(exit_code)
    elif not payload["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
