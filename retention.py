#!/usr/bin/env python3
"""数据保留与删除策略（P0 第 6 项：顾客与员工数据最小化）。

定位：**离线运维动作**，不是运行时能力。设计上刻意保守：

* 保留期写在 `store_config.json` 的 `retention` 段（0 = 永久保留），一处可改、可评审；
* `plan_purge()` 只算不删（默认行为），`purge(apply=True)` 才真删；
* 真删之前可选 `export_dir`：先把**要被删的行**导成 JSON.gz 归档（复用备份的行哈希格式），
  做到"先留存再删除"；
* 删除会写一条 `retention_purge` 审计（在删除之后写，保证这条记录本身不会被同一轮删掉）；
* 主数据（users / memberships / stores / store_configs）默认永久保留，策略里显式为 0，避免误配。

删除顺序按"先子后父"排列（task_evidence/task_events 先于 action_tasks），以适配 PostgreSQL 的外键。

只删本系统库里的行；`task_evidence.object_ref` 指向的截图/文件由文件存储侧按同样策略处理（不在本模块范围）。
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from db import Database

# 每张表的"判定时间列"；None 表示不做自动清理（人工管理）
DATE_COLUMNS: dict[str, Optional[str]] = {
    "platform_metrics": "metric_date",
    "package_metrics": "metric_date",
    "metric_import_batches": "max_date",
    "reviews": "created_at",
    "action_tasks": "updated_at",
    "task_evidence": "submitted_at",
    "task_events": "created_at",
    "audit_events": "created_at",
    "agent_sessions": "last_message_at",
    "agent_messages": "created_at",
    "auth_sessions": "expires_at",
    "login_attempts": "last_failed_at",
    "webhook_nonces": "received_at",
    "notifications": "created_at",
    "agent_tool_runs": "created_at",
    "approvals": "requested_at",
    "users": None,
    "memberships": None,
    "stores": None,
    "store_configs": None,
}

# 先子后父：删除顺序（只列出可清理的表）
DELETE_ORDER: tuple[str, ...] = (
    "task_evidence", "task_events", "action_tasks", "approvals",
    "agent_messages", "agent_sessions", "agent_tool_runs",
    "notifications", "webhook_nonces", "login_attempts", "auth_sessions",
    "reviews", "package_metrics", "platform_metrics", "metric_import_batches",
    "audit_events",
)

# 默认保留天数（0 = 永久）：店内可解释的默认值，写进 store_config.json 便于评审与调整
DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "platform_metrics": 1095,          # 经营指标 3 年（做同比要用）
    "package_metrics": 1095,
    "metric_import_batches": 1095,
    "reviews": 180,                    # 评价明细含顾客内容摘要，半年
    "action_tasks": 730,               # 任务与证据 2 年（申诉与复盘）
    "task_evidence": 730,
    "task_events": 730,
    "approvals": 730,
    "audit_events": 730,               # 安全审计 2 年
    "agent_messages": 90,              # 对话内容可能含顾客信息，90 天
    "agent_sessions": 90,
    "agent_tool_runs": 180,
    "notifications": 180,
    "auth_sessions": 30,               # 过期/撤销会话 30 天
    "login_attempts": 30,
    "webhook_nonces": 1,
    "users": 0,                        # 主数据永久保留（人工管理）
    "memberships": 0,
    "channel_bindings": 730,
    "stores": 0,
    "store_configs": 0,
}


class RetentionError(ValueError):
    """策略配置错误。"""


@dataclass(frozen=True)
class TablePlan:
    table: str
    days: int
    date_column: Optional[str]
    cutoff: Optional[str]
    rows: int
    action: str          # purge / keep / skip

    def as_dict(self) -> dict[str, Any]:
        return {"table": self.table, "days": self.days, "date_column": self.date_column,
                "cutoff": self.cutoff, "rows": self.rows, "action": self.action}


def policy_from_config(config: dict[str, Any]) -> dict[str, int]:
    """从门店配置读保留期；未列出的表用默认值。0 表示永久保留。"""
    configured = config.get("retention") or {}
    policy: dict[str, int] = {}
    for table, default_days in DEFAULT_RETENTION_DAYS.items():
        if table in configured:
            try:
                days = int(configured[table])
            except (TypeError, ValueError) as exc:
                raise RetentionError(f"retention.{table} 必须是天数（0 表示永久保留）") from exc
            if days < 0:
                raise RetentionError(f"retention.{table} 不能为负数")
            policy[table] = days
        else:
            policy[table] = default_days
    unknown = set(configured) - set(DEFAULT_RETENTION_DAYS)
    if unknown:
        raise RetentionError(f"retention 里有未知表：{'、'.join(sorted(unknown))}")
    return policy


def _existing_tables(db: Database, conn: Any) -> set[str]:
    """库里**实际存在**的表（不能只看 schema.sql 声明的表：SQLite 侧各 store 按需建表）。"""
    if db.dialect == "postgresql":
        rows = conn.execute("select table_name as name from information_schema.tables "
                            "where table_schema='public'").fetchall()
    else:
        rows = conn.execute("select name from sqlite_master where type='table'").fetchall()
    return {row["name"] for row in rows}


def _columns(db: Database, conn: Any, table: str) -> list[str]:
    """取表的列名（两个引擎各自的元数据视图）。"""
    if db.dialect == "postgresql":
        rows = conn.execute("select column_name as name from information_schema.columns "
                            "where table_schema='public' and table_name=? order by ordinal_position",
                            (table,)).fetchall()
    else:
        rows = conn.execute(f"pragma table_info({table})").fetchall()
    return [row["name"] for row in rows]




def _scope_where(db: Database, conn: Any, table: str, tenant_id: str, store_id: str) -> tuple[str, list[Any]]:
    columns = db.table_columns(conn, table)
    clauses: list[str] = []
    params: list[Any] = []
    if tenant_id and "tenant_id" in columns:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if store_id and "store_id" in columns:
        clauses.append("store_id = ?")
        params.append(store_id)
    return ((" and " + " and ".join(clauses)) if clauses else "", params)

def _cutoff(days: int, now: datetime) -> str:
    return (now - timedelta(days=days)).date().isoformat()


def plan_purge(db_target: Any, policy: dict[str, int], now: Optional[datetime] = None, *,
               tenant_id: str = "", store_id: str = "") -> list[TablePlan]:
    """只统计不删除：每张表将要删掉多少行、按哪一列、截止到哪天。"""
    moment = now or datetime.now()
    db = Database(db_target, tenant_id=tenant_id)
    plans: list[TablePlan] = []
    with db.connection() as conn:
        existing = _existing_tables(db, conn)
        # 先按删除顺序列出可清理的表，再补上永久保留的表（让计划呈现完整策略）
        ordered = list(DELETE_ORDER) + [table for table in DATE_COLUMNS if table not in DELETE_ORDER]
        for table in ordered:
            column = DATE_COLUMNS.get(table)
            days = policy.get(table, 0)
            if days <= 0:                        # 策略说永久保留 → 明确回报 keep（优先级高于"无日期列"）
                plans.append(TablePlan(table, days, column, None, 0, "keep"))
                continue
            if table not in existing or column is None:   # 表还没建 / 没有判定列 → 跳过而不是报错
                plans.append(TablePlan(table, days, column, None, 0, "skip"))
                continue
            bound = _cutoff(days, moment)
            scope, scope_params = _scope_where(db, conn, table, tenant_id, store_id)
            count = int(conn.execute(
                f"select count(*) as c from {table} where {column} < ?{scope}",
                tuple([bound] + scope_params)).fetchone()["c"])
            plans.append(TablePlan(table, days, column, bound, count, "purge" if count else "keep"))
    return plans


def purge(
    db_target: Any,
    policy: dict[str, int],
    *,
    apply: bool = False,
    export_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    actor: str = "ops:retention",
    tenant_id: str = "",
    store_id: str = "",
) -> dict[str, Any]:
    """执行（或仅计划）清理。`apply=False` 时只返回计划，绝不删除。"""
    plans = plan_purge(db_target, policy, now=now, tenant_id=tenant_id, store_id=store_id)
    result: dict[str, Any] = {
        "apply": apply, "planned": [plan.as_dict() for plan in plans],
        "deleted": {}, "exported": None, "total_deleted": 0,
        "planned_total": sum(plan.rows for plan in plans),
    }
    if not apply:
        return result

    db = Database(db_target, tenant_id=tenant_id)
    archive_rows: dict[str, dict[str, Any]] = {}
    deleted: dict[str, int] = {}
    if export_dir is None:
        export_dir = Path("data") / "retention_archive"
    with db.connection() as conn:
        for plan in plans:
            if plan.action != "purge" or not plan.date_column or not plan.cutoff:
                continue
            columns = _columns(db, conn, plan.table)
            scope, scope_params = _scope_where(db, conn, plan.table, tenant_id, store_id)
            rows = [dict(row) for row in conn.execute(
                f"select {', '.join(columns)} from {plan.table} "
                f"where {plan.date_column} < ?{scope}",
                tuple([plan.cutoff] + scope_params)).fetchall()]
            if rows:
                archive_rows[plan.table] = {"columns": columns, "cutoff": plan.cutoff, "rows": rows}

        if archive_rows:
            export_dir = Path(export_dir)
            export_dir.mkdir(parents=True, exist_ok=True)
            stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
            path = export_dir / f"purged-{stamp}.json.gz"
            payload = {"kind": "retention_archive", "created_at": datetime.now().isoformat(timespec="seconds"),
                       "actor": actor, "tables": archive_rows}
            path.write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")))
            summary = archive_summary(path)
            for table, item in archive_rows.items():
                if summary["tables"][table]["rows"] != len(item["rows"]):
                    raise RetentionError(f"归档校验失败：{table}")
            result["exported"] = {"path": str(path), "tables": {k: len(v["rows"]) for k, v in archive_rows.items()}}

        for plan in plans:
            if plan.action != "purge" or not plan.date_column or not plan.cutoff:
                continue
            scope, scope_params = _scope_where(db, conn, plan.table, tenant_id, store_id)
            cursor = conn.execute(
                f"delete from {plan.table} where {plan.date_column} < ?{scope}",
                tuple([plan.cutoff] + scope_params))
            deleted[plan.table] = int(cursor.rowcount or 0)

    result["deleted"] = deleted
    result["total_deleted"] = sum(deleted.values())

    # 审计放在删除之后写：这条记录本身不会被同一轮清理掉
    from auth import AuthStore

    AuthStore(db_target, tenant_id=tenant_id).record_audit(
        event_type="retention_purge", actor_type="system", actor_id=actor,
        tenant_id=tenant_id or None, store_id=store_id or None,
        object_type="retention_policy", object_id=(now or datetime.now()).date().isoformat(),
        after={"deleted": deleted, "total": result["total_deleted"],
               "exported": result["exported"]["path"] if result["exported"] else None,
               "policy": {plan.table: plan.days for plan in plans}},
    )
    return result


def archive_summary(path: Path) -> dict[str, Any]:
    """读取归档文件，回报每张表留存了多少行（给演练/审计对账用）。"""
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("kind") != "retention_archive":
        raise RetentionError(f"不是保留期归档文件：{path}")
    return {"created_at": payload.get("created_at"), "actor": payload.get("actor"),
            "tables": {table: {"cutoff": item["cutoff"], "rows": len(item["rows"])}
                       for table, item in payload["tables"].items()}}
