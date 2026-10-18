#!/usr/bin/env python3
"""SQLite / PostgreSQL 双引擎连接层（二期批次 8b）。

目标：**同一份业务代码**（platform_data.py / task_store.py / auth.py）既能跑 SQLite，
也能跑 PostgreSQL，由连接目标决定，不需要为两个引擎维护两套实现。

四件事：
  1. 占位符：SQL 一律用 `?`，PostgreSQL 侧自动翻译成 `%s`（SQL 里没有 % 模式，翻译安全）；
  2. 行规范化：PostgreSQL 的 date/timestamp 回读是对象、numeric 是 Decimal、jsonb 是 dict/list，
     统一规范成与 SQLite 一致的形态（ISO 字符串 / float / JSON 文本），使上层代码无需分支；
  3. 自增主键：SQLite 用 `cursor.lastrowid`，PostgreSQL 用 `insert ... returning id`，收口到 `insert_returning_id()`；
  4. 建表：SQLite 沿用各 store 的内联 DDL；PostgreSQL 直接执行权威 `schema.sql` 的「已落地」段。

时区约定：运行时的时间字符串一律是**门店本地时间**（默认 Asia/Shanghai）。PostgreSQL 连接会设置
`TIME ZONE`，回读时再统一为不带偏移的本地 ISO 字符串，从而与 SQLite 侧完全一致。

SQLite 说明：`sqlite3` 的 `with conn` 只提交事务、不关闭连接，因此在 Windows 上会锁住数据库文件
（批次 1 修过）。本模块的 `connection()` 统一负责提交/回滚 + 必定关闭。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

PG_PREFIXES = ("postgresql://", "postgres://")
STORE_TZ_NAME = os.getenv("STORE_TZ", "Asia/Shanghai")           # 仅用于说明与展示
STORE_TZ = timezone(timedelta(hours=float(os.getenv("STORE_TZ_OFFSET_HOURS", "8"))))


def store_tz_interval() -> str:
    """把门店时区转成 `+08:00` 形式的数值偏移。

    不用 IANA 名称（如 Asia/Shanghai）设置 PostgreSQL 会话时区，因为部分 PostgreSQL 构建
    （例如 Windows 上的便携版）不带完整时区数据库，`SET TIME ZONE 'Asia/Shanghai'` 会报
    invalid value for parameter "TimeZone"。数值偏移在任何构建上都可用。
    """
    offset = STORE_TZ.utcoffset(None) or timedelta(0)
    sign = "+" if offset >= timedelta(0) else "-"
    minutes = int(abs(offset).total_seconds()) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_SECTION_B_MARKER = "-- B. 规划中"


class _TimezoneNoiseFilter(logging.Filter):
    """过滤 psycopg 关于 POSIX 时区名的已知无害日志（见 Database.connection）。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return "unknown PostgreSQL timezone" not in record.getMessage()


# 全局表（不按租户隔离）：登录节流按用户名计数、Webhook nonce 由外部平台生成
GLOBAL_TABLES = {"login_attempts", "webhook_nonces"}
INSERT_RE = re.compile(r"(insert\s+into\s+)(\w+)(\s*)\(([^)]*)\)(\s*values\s*)\(([^)]*)\)", re.IGNORECASE)
CONFLICT_RE = re.compile(r"(on\s+conflict\s*)\(([^)]*)\)", re.IGNORECASE)


def inject_tenant(sql: str, params: Any, tenant_id: str) -> tuple[str, Any]:
    """给 INSERT 补 `tenant_id`（含 `on conflict` 目标），使每一行都带租户归属。

    * 表是全局表、或语句已显式写了 tenant_id → 原样返回；未配置租户时注入空串（与列默认值一致）；
    * 位置参数在末尾追加，命名参数注入 `:__tenant_id`；
    * 冲突目标同步补租户，保证 `on conflict` 命中的是**租户内**唯一键。
    """
    match = INSERT_RE.search(sql)
    if not match:
        return sql, params
    table = match.group(2).lower()
    columns = match.group(4)
    if table in GLOBAL_TABLES or re.search(r"\btenant_id\b", columns, re.IGNORECASE):
        return sql, params
    tenant_id = tenant_id or ""
    values = match.group(6)
    if ":" in values and "?" not in values:                     # 命名参数
        if not isinstance(params, dict):
            return sql, params
        new_params: Any = dict(params)
        new_params["__tenant_id"] = tenant_id
        new_values = values + ",:__tenant_id"
    else:                                                       # 位置参数
        if isinstance(params, dict):
            return sql, params
        new_params = tuple(params) + (tenant_id,)
        new_values = values + ",?"
    new_sql = (sql[:match.start()] + match.group(1) + match.group(2) + match.group(3)
               + "(" + columns + ",tenant_id)" + match.group(5) + "(" + new_values + ")"
               + sql[match.end():])
    # 冲突目标里若已显式包含 tenant_id 就不能再加（否则列重复）
    new_sql = CONFLICT_RE.sub(
        lambda c: c.group(0) if "tenant_id" in c.group(2)
        else f"{c.group(1)}({c.group(2)},tenant_id)",
        new_sql, count=1,
    )
    return new_sql, new_params


def resolve_target(target: Any) -> tuple[str, str]:
    """把连接目标解析成 (dialect, 目标字符串)。Path / 普通字符串 = SQLite 文件。"""
    if isinstance(target, str) and target.startswith(PG_PREFIXES):
        return "postgresql", target
    return "sqlite", str(target)


def is_postgres(target: Any) -> bool:
    return resolve_target(target)[0] == "postgresql"


def _normalize_iso_string(value: str) -> str:
    """把带偏移的 ISO 时间字符串统一成"门店本地时间、不带偏移"。

    PostgreSQL 的 timestamptz 回读会带时区；SQLite 则原样保存写入的文本，
    而规则引擎生成的截止时间带 `+08:00`。两边的运行时规范都是本地时间字符串，
    这里把带偏移的文本也归一化，保证两个引擎读出的值完全一致。
    """
    if len(value) < 19 or value[4] != "-" or value[10] not in "T ":
        return value
    tail = value[-6:]
    if not (value.endswith("Z") or tail[0] in "+-" and tail[1:3].isdigit() and tail[3] == ":"):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(STORE_TZ).replace(tzinfo=None).isoformat()


def normalize_value(value: Any) -> Any:
    """把数据库回读值规范成统一形态（ISO 本地时间字符串 / float / JSON 文本）。"""
    if isinstance(value, str):
        return _normalize_iso_string(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(STORE_TZ).replace(tzinfo=None)
        return value.isoformat(timespec="seconds") if value.microsecond == 0 else value.isoformat()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return value


def normalize_row(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):          # sqlite3.Row 既不是 dict 也不是 tuple，必须显式处理
        return {key: normalize_value(row[key]) for key in row.keys()}
    if isinstance(row, dict):
        return {key: normalize_value(value) for key, value in row.items()}
    if isinstance(row, (list, tuple)):
        return type(row)(normalize_value(value) for value in row)
    return row


def translate_placeholders(sql: str) -> str:
    """把 SQLite 占位符翻译成 psycopg 风格，跳过字符串字面量与标识符。

    * `?`        → `%s`（位置参数）
    * `:name`    → `%(name)s`（命名参数；跳过 `::` 类型转换）
    """
    out: list[str] = []
    index = 0
    quote: Optional[str] = None
    while index < len(sql):
        char = sql[index]
        if quote:
            out.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "?":
            out.append("%s")
            index += 1
            continue
        if (char == ":" and index + 1 < len(sql)
                and (sql[index + 1].isalpha() or sql[index + 1] == "_")
                and (index == 0 or sql[index - 1] != ":")):
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            out.append("%(" + sql[index + 1:end] + ")s")
            index = end
            continue
        out.append(char)
        index += 1
    return "".join(out)


def adapt_params(params: Any) -> Any:
    """保持参数字典的映射语义（SQLite 命名参数需要 Mapping），其余转成元组。"""
    if params is None:
        return ()
    if isinstance(params, dict):
        return dict(params)
    if isinstance(params, (str, bytes)):
        return (params,)
    try:
        return tuple(params)
    except TypeError:
        return (params,)


class Cursor:
    """游标包装：统一行形态，并暴露 lastrowid/rowcount。"""

    def __init__(self, cursor: Any, dialect: str, tenant_id: str = ""):
        self._cursor = cursor
        self._dialect = dialect
        self._tenant_id = tenant_id

    def execute(self, sql: str, params: Iterable[Any] = ()) -> "Cursor":
        adapted = adapt_params(params)
        query, adapted = inject_tenant(sql, adapted, self._tenant_id)
        if self._dialect != "sqlite":
            query = translate_placeholders(query)
        self._cursor.execute(query, adapted)
        return self

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> "Cursor":
        rows = []
        query = sql
        for item in seq_of_params:
            query, adapted = inject_tenant(sql, adapt_params(item), self._tenant_id)
            rows.append(adapted)
        if self._dialect != "sqlite":
            query = translate_placeholders(query)
        self._cursor.executemany(query, rows)
        return self

    def fetchone(self) -> Any:
        return normalize_row(self._cursor.fetchone())

    def fetchall(self) -> list[Any]:
        return [normalize_row(row) for row in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[Any]:
        for row in self._cursor:
            yield normalize_row(row)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int:
        return int(getattr(self._cursor, "lastrowid", 0) or 0)

    def close(self) -> None:
        self._cursor.close()


class Connection:
    """连接包装：统一执行入口（占位符翻译 + 行规范化 + 租户注入）。"""

    def __init__(self, connection: Any, dialect: str, tenant_id: str = ""):
        self._connection = connection
        self.dialect = dialect
        self.tenant_id = tenant_id

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Cursor:
        return Cursor(self._connection.cursor(), self.dialect, self.tenant_id).execute(sql, params)

    def cursor(self) -> Cursor:
        return Cursor(self._connection.cursor(), self.dialect, self.tenant_id)

    def executescript(self, script: str) -> None:
        if self.dialect == "sqlite":
            self._connection.executescript(script)
            return
        for statement in split_statements(script):
            self._connection.cursor().execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


def split_statements(script: str) -> list[str]:
    """按分号拆分 SQL 脚本并去掉注释行（供 PostgreSQL 逐句执行）。"""
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def read_schema_section_a(path: Path = SCHEMA_FILE) -> str:
    text = Path(path).read_text(encoding="utf-8")
    if SCHEMA_SECTION_B_MARKER in text:
        text = text.split(SCHEMA_SECTION_B_MARKER)[0]
    return text


class Database:
    """一个数据库目标的连接工厂。`target` 可以是 SQLite 文件路径，也可以是 PostgreSQL DSN。"""

    def __init__(self, target: Any, schema_file: Path = SCHEMA_FILE, tenant_id: str = ""):
        self.dialect, self.target = resolve_target(target)
        self.schema_file = Path(schema_file)
        # 运行时按门店作用域读写；tenant_id 作为"归属标注"由服务端注入默认值，
        # 真正的按租户过滤随多门店批次开启（届时改为逐查询过滤）。
        self.tenant_id = tenant_id or ""

    def __repr__(self) -> str:  # pragma: no cover - 便于排查
        display = self.target if self.dialect == "sqlite" else self.target.split("@")[-1]
        return f"<Database {self.dialect} {display}>"

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        if self.dialect == "sqlite":
            conn = sqlite3.connect(str(self.target))
            conn.row_factory = sqlite3.Row
            try:
                with conn:
                    yield Connection(conn, "sqlite", self.tenant_id)
            finally:
                conn.close()
            return

        import warnings

        import psycopg
        from psycopg.rows import dict_row

        # 部分 PostgreSQL 构建不带时区名（见 store_tz_interval 的说明），会话时区会以 POSIX 名
        # 形式返回（如 `<+08>-08`），psycopg 会记录一条 "unknown PostgreSQL timezone" 日志。
        # 本模块自行做时区换算（normalize_value 统一 astimezone(STORE_TZ)），该日志无害；
        # 时间往返的正确性由 test_pg_runtime.py 断言保证，这里只过滤这一条已知噪音。
        warnings.filterwarnings("ignore", message=r".*unknown PostgreSQL timezone.*")
        logging.getLogger("psycopg").addFilter(_TimezoneNoiseFilter())
        conn = psycopg.connect(self.target, row_factory=dict_row)
        try:
            conn.execute(f"set time zone interval '{store_tz_interval()}' hour to minute")
            if self.tenant_id:
                # 行级安全（RLS）策略用这个变量判定租户；未设置的连接看不到任何租户行
                conn.execute("select set_config('app.tenant_id', %s, false)", (self.tenant_id,))
            with conn:                       # psycopg 的 with conn 会提交并关闭连接
                yield Connection(conn, "postgresql", self.tenant_id)
        finally:
            if not conn.closed:
                conn.close()

    # ---------------- 方言相关能力 ----------------

    def landed_tables(self) -> list[str]:
        """schema.sql 已落地段里的表名。"""
        import re
        return re.findall(r"create table if not exists\s+(\w+)", read_schema_section_a(self.schema_file))

    def apply_schema(self, conn: Connection) -> int:
        """仅在 PostgreSQL 上执行权威 schema.sql 的已落地段（幂等），并注入租户默认值。"""
        if self.dialect != "postgresql":
            return 0
        statements = split_statements(read_schema_section_a(self.schema_file))
        cursor = conn.cursor()
        for statement in statements:
            cursor.execute(statement)
        if self.tenant_id:
            for table in self.landed_tables():
                if "tenant_id" in self.table_columns(conn, table):
                    conn.execute(
                        f"alter table {table} alter column tenant_id set default '{self.tenant_id}'"
                    )
        return len(statements)

    def table_columns(self, conn: Connection, table: str) -> set[str]:
        if self.dialect == "sqlite":
            rows = conn.execute(f"pragma table_info({table})").fetchall()
            return {row["name"] for row in rows}
        rows = conn.execute(
            "select column_name as name from information_schema.columns where table_name = ?",
            (table,),
        ).fetchall()
        return {row["name"] for row in rows}

    def insert_returning_id(self, conn: Connection, sql: str, params: Iterable[Any] = ()) -> int:
        """插入并返回自增主键：SQLite 用 lastrowid，PostgreSQL 用 returning id。"""
        if self.dialect == "sqlite":
            return conn.execute(sql, params).lastrowid
        cursor = conn.execute(sql + " returning id", params)
        row = cursor.fetchone()
        if isinstance(row, dict):
            return int(row["id"])
        return int(row[0])

    def ensure_tenant_column(self, conn: Connection, table: str,
                             unique_index: tuple[str, tuple[str, ...]] | None = None) -> None:
        """确保表有 tenant_id 列；需要时同时补"租户级唯一索引"（老库迁移用）。"""
        self.add_column_if_missing(conn, table, "tenant_id", "tenant_id text not null default ''")
        if unique_index:
            name, columns = unique_index
            conn.execute(f"create unique index if not exists {name} on {table} ({', '.join(columns)})")

    def add_column_if_missing(self, conn: Connection, table: str, column: str, ddl: str) -> bool:
        """补列迁移：SQLite 不支持 add column if not exists，PostgreSQL 支持。"""
        if column in self.table_columns(conn, table):
            return False
        if self.dialect == "sqlite":
            conn.execute(f"alter table {table} add column {ddl}")
        else:
            conn.execute(f"alter table {table} add column if not exists {ddl}")
        return True


# 兼容旧调用：一期代码里到处是 `with connection(path) as conn`
def connection(target: Any) -> Iterator[Connection]:
    return Database(target).connection()
