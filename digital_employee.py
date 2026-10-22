#!/usr/bin/env python3
"""数字员工（第 10 期）：岗位与能力档案。

边界（与总方案第 10 期一致）：
1. 数字员工是**档案**：岗位职责 + 能力引用（引用第 5 期能力注册表的 `tool:<name>` id）。
2. 能力引用会被校验：不存在于注册表的能力 id 一律拒绝登记（避免"档案里写着、实际调不了"）。
3. 团队领导采用"数字员工扮演 + 人工兜底"的混合制（`team.py` 里的 `human_fallback`）；
   **不引入竞标与血条赛制**（计划明确不做）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from db import Database

EMPLOYEE_STATUSES = ("active", "paused", "retired")


class DigitalEmployeeError(ValueError):
    """数字员工档案的参数或状态错误。"""


class DigitalEmployeeStore:
    """`digital_employees` 的读写。"""

    def __init__(self, db_target: Any, tenant_id: str = "", *, capability_registry: Any = None):
        self.db = Database(db_target, tenant_id=tenant_id)
        self.tenant_id = tenant_id
        self.capability_registry = capability_registry
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
            conn.executescript("""
                create table if not exists digital_employees (
                  tenant_id text not null default '',
                  employee_id text not null,
                  store_id text not null,
                  employee_key text not null,
                  name text not null,
                  role_profile text not null default '',
                  capabilities_json text not null default '[]',
                  status text not null default 'active' check (status in ('active','paused','retired')),
                  created_by text not null default '',
                  created_at text not null,
                  primary key (tenant_id, employee_id),
                  unique (tenant_id, store_id, employee_key)
                );
                """)

    def _require_text(self, value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise DigitalEmployeeError(f"{field} 不能为空")
        return text

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def hire(self, store_id: str, employee_key: str, name: str, *,
             role_profile: str = "", capabilities: Optional[Iterable[str]] = None,
             created_by: str = "") -> dict[str, Any]:
        """登记一个数字员工；同 key 幂等（已存在直接返回）。"""
        store_id = self._require_text(store_id, "store_id")
        employee_key = self._require_text(employee_key, "employee_key")
        name = self._require_text(name, "name")
        capability_ids = [str(item) for item in (capabilities or [])]
        self._validate_capabilities(capability_ids)
        with self._connection() as conn:
            existing = conn.execute(
                "select * from digital_employees where tenant_id=? and store_id=? and employee_key=?",
                (self.tenant_id, store_id, employee_key)).fetchone()
            if existing:
                return self._decode(dict(existing))
            employee_id = f"de-{uuid.uuid4().hex[:12]}"
            conn.execute(
                """insert into digital_employees (tenant_id,employee_id,store_id,employee_key,name,
                   role_profile,capabilities_json,status,created_by,created_at)
                   values (?,?,?,?,?,?,?,'active',?,?)""",
                (self.tenant_id, employee_id, store_id, employee_key, name, str(role_profile or ""),
                 json.dumps(capability_ids, ensure_ascii=False), str(created_by or ""), self._now()))
        return self.get(employee_id)

    def _validate_capabilities(self, capability_ids: list[str]) -> None:
        if not capability_ids or self.capability_registry is None:
            return
        known = {item.capability_id for item in self.capability_registry.all()}
        unknown = [item for item in capability_ids if item not in known]
        if unknown:
            raise DigitalEmployeeError(
                f"能力不存在（应引用第 5 期能力注册表的 id）：{'、'.join(sorted(unknown))}")

    @staticmethod
    def _decode(record: dict[str, Any]) -> dict[str, Any]:
        value = record.get("capabilities_json")
        if isinstance(value, str):
            try:
                record["capabilities"] = json.loads(value)
            except json.JSONDecodeError:
                record["capabilities"] = []
        return record

    def get(self, employee_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from digital_employees where tenant_id=? and employee_id=?",
                               (self.tenant_id, str(employee_id))).fetchone()
        if not row:
            raise DigitalEmployeeError(f"数字员工不存在：{employee_id}")
        return self._decode(dict(row))

    def list(self, store_id: str, status: Optional[str] = None) -> list[dict[str, Any]]:
        store_id = self._require_text(store_id, "store_id")
        if status is not None and status not in EMPLOYEE_STATUSES:
            raise DigitalEmployeeError(f"status 必须是 {'、'.join(EMPLOYEE_STATUSES)} 之一，收到：{status!r}")
        sql = "select * from digital_employees where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, store_id]
        if status:
            sql += " and status=?"
            params.append(status)
        sql += " order by employee_key"
        with self._connection() as conn:
            return [self._decode(dict(row)) for row in conn.execute(sql, tuple(params)).fetchall()]

    def set_status(self, employee_id: str, status: str) -> dict[str, Any]:
        if status not in EMPLOYEE_STATUSES:
            raise DigitalEmployeeError(f"status 必须是 {'、'.join(EMPLOYEE_STATUSES)} 之一，收到：{status!r}")
        self.get(employee_id)
        with self._connection() as conn:
            conn.execute("update digital_employees set status=? where tenant_id=? and employee_id=?",
                         (status, self.tenant_id, str(employee_id)))
        return self.get(employee_id)