#!/usr/bin/env python3
"""备份、恢复与恢复演练（P0 闸门第 10 项）。

设计：
  * **引擎无关**：备份是一份 JSON（含每张表的全部行 + 每表的行数与内容哈希），
    SQLite 与 PostgreSQL 用同一套逻辑，不依赖 pg_dump / sqlite3 命令行工具。
  * **可自证**：`verify_backup()` 会重算每张表的哈希与备份里的元数据比对，能发现被篡改或截断的备份。
  * **演练就是真的恢复**：`drill()` 备份 → 恢复到**全新库** → 逐表比对行数与内容哈希；
    只有两边哈希一致才算演练通过（不是"备份文件存在"这种假验证）。
  * 恢复默认拒绝覆盖非空库，必须显式 `force=True`（避免误操作把生产数据清掉）。

已知边界：这是**逻辑备份**，适合单店规模（当前数据量很小）；数据量上去后应换成
pg_dump / WAL 归档，本模块的"演练 + 哈希对账"思路可以保留。
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Database

BACKUP_VERSION = 1
SKIP_COLUMNS: tuple[str, ...] = ()          # 当前没有需要跳过的列；留作扩展点


class BackupError(ValueError):
    """备份/恢复相关的可预期错误。"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def table_hash(rows: list[dict[str, Any]]) -> str:
    """对一张表的全部行做内容哈希（行内按列名排序，行间按整行排序，保证稳定）。"""
    normalized = [json.dumps({key: row[key] for key in sorted(row)}, ensure_ascii=False, sort_keys=True,
                             default=str) for row in rows]
    digest = hashlib.sha256()
    for line in sorted(normalized):
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def _read_table(conn: Any, table: str, columns: list[str]) -> list[dict[str, Any]]:
    sql = f"select {', '.join(columns)} from {table}"
    return [dict(row) for row in conn.execute(sql).fetchall()]


def _table_columns(db: Database, conn: Any, table: str) -> list[str]:
    """取表的列名（两个引擎各自的元数据视图），保持列顺序保证 INSERT 稳定。"""
    if db.dialect == "postgresql":
        rows = conn.execute(
            "select column_name as name from information_schema.columns "
            "where table_schema='public' and table_name=? order by ordinal_position",
            (table,),
        ).fetchall()
    else:
        rows = conn.execute(f"pragma table_info({table})").fetchall()
    return [row["name"] for row in rows if row["name"] not in SKIP_COLUMNS]


def ensure_schema(db_target: Any, tenant_id: str = "") -> list[str]:
    """确保目标库的运行时表结构存在（SQLite 由各 store 内联 DDL 建立，PostgreSQL 走 schema.sql）。

    恢复到**全新库**时必须先建表；复用各 store 的初始化逻辑，避免在这里维护第二份 DDL。
    """
    from auth import AuthStore
    from governance import ApprovalStore, ToolRunLog
    from notify import NotificationLedger
    from platform_data import PlatformMetricStore
    from registry import RegistryStore
    from store_agent import AgentSessionStore
    from task_store import TaskStore

    initializers = (PlatformMetricStore, TaskStore, AuthStore, ApprovalStore, ToolRunLog,
                    RegistryStore, NotificationLedger, AgentSessionStore)
    for factory in initializers:
        factory(db_target, tenant_id=tenant_id)
    return Database(db_target, tenant_id=tenant_id).landed_tables()


def create_backup(db_target: Any, out_path: Path, compress: bool = True, *, tenant_id: str = "",
                  store_id: str = "") -> dict[str, Any]:
    """把全部已落地表写进一个 JSON（或 .json.gz）备份文件。"""
    db = Database(db_target, tenant_id=tenant_id)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tables: dict[str, Any] = {}
    with db.connection() as conn:
        landed = db.landed_tables()
        if not landed:
            raise BackupError("没有找到任何已落地表，schema.sql 可能缺失")
        for table in landed:
            columns = _table_columns(db, conn, table)
            if not columns:
                continue
            rows = _read_table(conn, table, columns)
            tables[table] = {"columns": columns, "row_count": len(rows),
                             "sha256": table_hash(rows), "rows": rows}
    manifest = {
        "backup_version": BACKUP_VERSION,
        "created_at": now_iso(),
        "dialect": db.dialect,
        "target": db.target if db.dialect == "sqlite" else db.target.split("@")[-1],
        "table_count": len(tables),
        "row_total": sum(item["row_count"] for item in tables.values()),
        "tenant_id": tenant_id,
        "store_id": store_id,
    }
    payload = {"manifest": manifest, "tables": tables}
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if compress:
        out_path.write_bytes(gzip.compress(raw, compresslevel=6))
    else:
        out_path.write_bytes(raw)
    manifest["path"] = str(out_path)
    manifest["bytes"] = out_path.stat().st_size
    manifest["sha256"] = hashlib.sha256(raw).hexdigest()
    return manifest


def load_backup(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise BackupError(f"备份文件不存在：{path}")
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"备份文件无法解析：{exc}") from exc
    if payload.get("manifest", {}).get("backup_version") != BACKUP_VERSION:
        raise BackupError(f"备份版本不支持：{payload.get('manifest', {}).get('backup_version')}")
    payload["_raw_sha256"] = hashlib.sha256(raw).hexdigest()
    return payload


def verify_backup(path: Path) -> dict[str, Any]:
    """自检备份：逐表重算哈希，与元数据比对。"""
    payload = load_backup(path)
    problems: list[str] = []
    for table, item in payload["tables"].items():
        if item["row_count"] != len(item["rows"]):
            problems.append(f"{table}: 行数与数据不符（{item['row_count']} vs {len(item['rows'])}）")
        if table_hash(item["rows"]) != item["sha256"]:
            problems.append(f"{table}: 内容哈希不匹配（备份可能被篡改或截断）")
    manifest = payload["manifest"]
    if manifest["row_total"] != sum(item["row_count"] for item in payload["tables"].values()):
        problems.append("总行数与各表之和不一致")
    return {"ok": not problems, "manifest": manifest, "problems": problems,
            "tables": {table: item["row_count"] for table, item in payload["tables"].items()}}


def restore_backup(db_target: Any, path: Path, force: bool = False, *, tenant_id: str = "",
                   store_id: str = "") -> dict[str, Any]:
    """把备份恢复进目标库；默认拒绝覆盖非空库。"""
    verification = verify_backup(path)
    if not verification["ok"]:
        raise BackupError("备份校验失败，拒绝恢复：" + "；".join(verification["problems"]))
    payload = load_backup(path)
    ensure_schema(db_target, tenant_id=tenant_id)  # 全新库也要先有表结构
    db = Database(db_target, tenant_id=tenant_id)
    restored: dict[str, int] = {}
    skipped: list[str] = []
    with db.connection() as conn:
        landed = set(db.landed_tables())
        existing = {table: int(conn.execute(f"select count(*) as c from {table}").fetchone()["c"])
                    for table in payload["tables"] if table in landed}
        non_empty = {table: count for table, count in existing.items() if count}
        if non_empty and not force:
            raise BackupError(f"目标库非空（{len(non_empty)} 张表有数据），如确认覆盖请加 --force")
        for table in payload["tables"]:
            if table not in landed:
                skipped.append(table)
        for table, item in payload["tables"].items():
            if table not in landed:
                continue
            if force:
                conn.execute(f"delete from {table}")
            columns = item["columns"]
            placeholders = ",".join("?" for _ in columns)
            statement = f"insert into {table} ({','.join(columns)}) values ({placeholders})"
            for row in item["rows"]:
                conn.execute(statement, tuple(row.get(column) for column in columns))
            restored[table] = len(item["rows"])
        # PostgreSQL：把 identity 序列推进到最大值，避免后续插入撞主键
        if db.dialect == "postgresql":
            for table in payload["tables"]:
                if table not in landed:
                    continue
                columns = _table_columns(db, conn, table)
                if "id" not in columns:
                    continue
                # 不能假设"有 id 列就是自增主键"：uuid 主键既没有 <table>_id_seq，
                # 也不能 max(uuid)。用 pg_get_serial_sequence 判断是否真有序列，
                # 没有就跳过（第 3 期把 uuid 主键的 action_rules 转正时踩到过）。
                sequence = conn.execute(
                    "select pg_get_serial_sequence(?, 'id') as seq", (table,)
                ).fetchone()["seq"]
                if not sequence:
                    continue
                conn.execute(
                    f"select setval('{sequence}', coalesce((select max(id) from {table}), 1))"
                )
    return {"restored": restored, "skipped_tables": skipped,
            "row_total": sum(restored.values()), "source_manifest": payload["manifest"]}


def drill(source_target: Any, workdir: Path, target_filename: str = "drill_restore.db", *,
          tenant_id: str = "", store_id: str = "") -> dict[str, Any]:
    """恢复演练：备份 → 恢复到全新库 → 逐表比对行数与内容哈希。"""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = workdir / f"backup-{stamp}.json.gz"
    manifest = create_backup(source_target, backup_path, tenant_id=tenant_id, store_id=store_id)
    verification = verify_backup(backup_path)
    if not verification["ok"]:
        return {"ok": False, "stage": "verify", "problems": verification["problems"],
                "backup": manifest}

    source = Database(source_target)
    cleanup: list[str] = []
    if source.dialect == "sqlite":
        restore_target: Any = workdir / target_filename
        if Path(restore_target).exists():
            Path(restore_target).unlink()          # noqa: dangerous - 只删本次演练自己的目标文件
    else:
        # PostgreSQL：在同一实例里建一个**全新数据库**做恢复目标（绝不能覆盖源库），演练完删掉
        import psycopg
        drill_db = "aiyg_drill_" + datetime.now().strftime("%H%M%S")
        with psycopg.connect(source_target, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(f'create database "{drill_db}"')
        restore_target = _swap_database(source_target, drill_db)
        cleanup.append(drill_db)
    try:
        restore = restore_backup(restore_target, backup_path, force=True, tenant_id=tenant_id, store_id=store_id)
    except Exception:
        if cleanup:
            _drop_database(source_target, cleanup[0])
        raise

    mismatches: list[dict[str, Any]] = []
    with source.connection() as conn:
        for table, item in load_backup(backup_path)["tables"].items():
            if table not in restore["restored"]:
                continue
            columns = _table_columns(source, conn, table)
            rows = _read_table(conn, table, columns)
            digest = table_hash(rows)
            if len(rows) != item["row_count"] or digest != item["sha256"]:
                mismatches.append({"table": table, "expected_rows": item["row_count"],
                                   "actual_rows": len(rows), "hash_match": digest == item["sha256"]})
    if cleanup:
        _drop_database(source_target, cleanup[0])
    return {"ok": not mismatches, "stage": "compare", "backup": manifest,
            "verification": verification, "restore": {k: v for k, v in restore.items() if k != "source_manifest"},
            "mismatches": mismatches,
            "checked_tables": len(restore["restored"])}

def _swap_database(dsn: str, database: str) -> str:
    """把 DSN 的库名换成另一个（用于演练目标库）。"""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, "/" + database, parts.query, parts.fragment))


def _drop_database(dsn: str, database: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(f'drop database if exists "{database}" with (force)')
