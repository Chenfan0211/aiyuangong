#!/usr/bin/env python3
"""规则库：把硬编码规则外置成可版本化、可审批、可回滚的数据（第 3 期）。

第 3 期第 1 步只做"底座"：表转正 + 读写/版本/状态机。**不改动 `prototype.py` 的判定逻辑**——
先把规则放进表里、把一致性验证做出来，再谈接线（第 2 步）。

三条硬约定（与总方案第 3 期一致）：
1. **同一 `rule_key` 只允许一个 `approved` 版本**：审批新版本时，旧的自动转 `retired`，
   避免"到底哪版生效"说不清。
2. `draft` 只能改自己，不能改 `approved`；任何状态变更都要留下 `approved_by` / 审计。
3. 规则**加载失败不静默降级到"无规则"**：读表异常由调用方决定回退内置默认规则。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Database

RULE_STATUSES = ("draft", "approved", "paused", "retired")


class RuleStoreError(ValueError):
    """规则库的状态机错误（非法转换、版本不存在等）。"""


class RuleStore:
    """`action_rules` 的读写入口（SQLite / PostgreSQL 双引擎）。"""

    def __init__(self, db_target: Any, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)
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
                create table if not exists action_rules (
  tenant_id text not null default '',
                  id text not null,
                  rule_key text not null,
                  version integer not null,
                  scope text not null,
                  condition_json text not null,
                  action_template_json text not null,
                  status text not null check (status in ('draft','approved','paused','retired')),
                  approved_by text,
                  approved_at text,
                  primary key (tenant_id, rule_key, version)
                );
                create index if not exists idx_action_rules_status
                  on action_rules(tenant_id, rule_key, status);
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """幂等迁移：SQLite 旧库补租户列与唯一索引（PostgreSQL 侧由 schema.sql 保证）。"""
        with self._connection() as conn:
            self.db.ensure_tenant_column(
                conn, "action_rules",
                ("ux_action_rules_runtime", ("tenant_id", "rule_key", "version")))

    # ---------------- 读 ----------------

    def list_rules(self, rule_key: Optional[str] = None) -> list[dict[str, Any]]:
        """列出规则（可按 rule_key 过滤），按 key → 版本倒序。"""
        sql = "select * from action_rules"
        params: list[Any] = []
        if rule_key:
            sql += " where rule_key=?"
            params.append(rule_key)
        sql += " order by rule_key, version desc"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._decode(row) for row in rows]

    def get_rule(self, rule_key: str, version: Optional[int] = None) -> Optional[dict[str, Any]]:
        """取指定版本；`version=None` 时优先取 `approved`，没有则取最新版本。"""
        with self._connection() as conn:
            if version is not None:
                row = conn.execute(
                    "select * from action_rules where rule_key=? and version=?",
                    (rule_key, int(version)),
                ).fetchone()
            else:
                row = conn.execute(
                    """select * from action_rules where rule_key=?
                    order by case status when 'approved' then 0 else 1 end, version desc
                    limit 1""",
                    (rule_key,),
                ).fetchone()
        return self._decode(row) if row else None

    def approved_rules(self) -> list[dict[str, Any]]:
        """当前生效的规则集合（每个 rule_key 至多一条）。"""
        with self._connection() as conn:
            rows = conn.execute(
                "select * from action_rules where status='approved' order by rule_key"
            ).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: Any) -> dict[str, Any]:
        item = dict(row)
        for key in ("scope", "condition_json", "action_template_json"):
            if isinstance(item.get(key), str):
                try:
                    item[key] = json.loads(item[key])
                except json.JSONDecodeError:
                    item[key] = {}
        return item

    # ---------------- 写（版本 + 状态机） ----------------

    def save_draft(
        self,
        rule_key: str,
        *,
        scope: dict[str, Any],
        condition: dict[str, Any],
        action_template: dict[str, Any],
    ) -> dict[str, Any]:
        """写入一个新的 `draft` 版本（版本号 = 现有最大版本 + 1）。

        `approved` / `paused` 的版本不会被覆盖——改规则永远是"新增一版"，不是就地改。
        """
        key = str(rule_key or "").strip()
        if not key:
            raise RuleStoreError("rule_key 不能为空")
        with self._connection() as conn:
            row = conn.execute(
                "select coalesce(max(version), 0) as v from action_rules where rule_key=?", (key,)
            ).fetchone()
            version = int(row["v"]) + 1
            conn.execute(
                """insert into action_rules
                (id,rule_key,version,scope,condition_json,action_template_json,status)
                values (?,?,?,?,?,?, 'draft')""",
                (str(uuid.uuid4()), key, version,
                 json.dumps(scope, ensure_ascii=False),
                 json.dumps(condition, ensure_ascii=False),
                 json.dumps(action_template, ensure_ascii=False)),
            )
        return self.get_rule(key, version) or {}

    def decide(self, rule_key: str, version: int, status: str, *, actor_id: str = "",
               note: str = "") -> dict[str, Any]:
        """审批/暂停/退役一个版本。

        `approved` 时会**自动把同 rule_key 的其它 approved 版本转 retired**，
        保证任何时刻每个 rule_key 只有一个生效版本（第 3 期硬约定 1）。
        """
        if status not in RULE_STATUSES:
            raise RuleStoreError("status 只支持：" + "、".join(RULE_STATUSES))
        key, ver = str(rule_key or "").strip(), int(version)
        current = self.get_rule(key, ver)
        if current is None:
            raise RuleStoreError(f"规则 {key} v{ver} 不存在")
        if status == "draft":
            raise RuleStoreError("不能把已存在的版本改回 draft；要改规则请新增一版")
        now = datetime.now().isoformat(timespec="seconds")
        with self._connection() as conn:
            if status == "approved":
                conn.execute(
                    """update action_rules set status='retired'
                    where rule_key=? and status='approved' and version<>?""",
                    (key, ver),
                )
            conn.execute(
                """update action_rules set status=?, approved_by=?, approved_at=?
                where rule_key=? and version=?""",
                (status, actor_id or None, now if status == "approved" else None, key, ver),
            )
        record = self.get_rule(key, ver) or {}
        record["note"] = note
        return record