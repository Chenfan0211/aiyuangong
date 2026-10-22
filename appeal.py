#!/usr/bin/env python3
"""员工申诉闭环（第 4 期）：让"奖惩只出草稿"这条红线有制度出口。

三条硬约定（与总方案第 4 期一致）：
1. 申诉必须**绑定具体任务与理由**，且同一任务只允许一次有效申诉（pending / accepted）。
2. **申诉未结案时奖惩不得生效**：`blocker_for()` 供审批入口拦截（返回未结案申诉）。
3. 系统绝不直接给员工定罚款或纪律处分：申诉结论只决定"奖惩草稿能否进入审批"。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Database

APPEAL_STATUSES = ("pending", "accepted", "rejected")
OPEN_APPEAL_STATUSES = ("pending",)
DECISIONS = ("accepted", "rejected")
REASON_REQUIRED = ("rejected",)


class AppealError(ValueError):
    """申诉的参数或状态错误。"""


class AppealConflict(AppealError):
    """同一任务已有未结案申诉（重复提交）。"""


class AppealStore:
    """`appeals` 的读写入口（SQLite / PostgreSQL 双引擎）。"""

    def __init__(self, db_target: Any, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)
        self.tenant_id = tenant_id
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
                create table if not exists appeals (
                  tenant_id text not null default '',
                  appeal_id text not null,
                  store_id text not null,
                  task_id text not null,
                  employee_id text not null,
                  reason text not null,
                  status text not null check (status in ('pending','accepted','rejected')),
                  decision_note text not null default '',
                  decided_by text,
                  created_at text not null,
                  decided_at text,
                  primary key (tenant_id, appeal_id)
                );
                create index if not exists idx_appeals_store_task
                  on appeals(tenant_id, store_id, task_id, status);
                """
            )

    def _require_text(self, value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise AppealError(f"{field} 不能为空")
        return text

    # ---------------- 写 ----------------

    def submit(self, store_id: str, task_id: str, employee_id: str, reason: str) -> dict[str, Any]:
        """提交申诉：必须绑定任务与理由，且同一任务只允许一次有效申诉。"""
        store_id = self._require_text(store_id, "store_id")
        task_id = self._require_text(task_id, "task_id")
        employee_id = self._require_text(employee_id, "employee_id")
        reason = self._require_text(reason, "reason")
        with self._connection() as conn:
            existing = conn.execute(
                "select appeal_id,status from appeals where tenant_id=? and store_id=? and task_id=? "
                "and status in ('pending','accepted') limit 1",
                (self.tenant_id, store_id, task_id)).fetchone()
            if existing:
                raise AppealConflict(f"任务 {task_id} 已有未结案申诉：{existing['appeal_id']}")
            appeal_id = f"apl-{uuid.uuid4().hex[:12]}"
            moment = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """insert into appeals
                   (tenant_id,appeal_id,store_id,task_id,employee_id,reason,status,
                    decision_note,decided_by,created_at,decided_at)
                   values (?,?,?,?,?,?,'pending','',null,?,null)""",
                (self.tenant_id, appeal_id, store_id, task_id, employee_id, reason, moment))
        return self.get(appeal_id)

    def decide(self, appeal_id: str, decision: str, decided_by: str, note: str = "") -> dict[str, Any]:
        """受理申诉：只有 pending 可受理；驳回必须写明结论。"""
        appeal = self.get(appeal_id)
        if appeal["status"] != "pending":
            raise AppealError(f"申诉 {appeal_id} 当前状态为 {appeal['status']}，不能重复受理")
        if decision not in DECISIONS:
            raise AppealError(f"decision 必须是 {'、'.join(DECISIONS)} 之一，收到：{decision!r}")
        note = str(note or "").strip()
        if decision in REASON_REQUIRED and not note:
            raise AppealError("驳回申诉必须写明结论")
        decided_by = self._require_text(decided_by, "decided_by")
        moment = datetime.now().isoformat(timespec="seconds")
        with self._connection() as conn:
            conn.execute(
                "update appeals set status=?, decision_note=?, decided_by=?, decided_at=? "
                "where tenant_id=? and appeal_id=?",
                (decision, note, decided_by, moment, self.tenant_id, appeal_id))
        return self.get(appeal_id)

    # ---------------- 读 ----------------

    def get(self, appeal_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "select * from appeals where tenant_id=? and appeal_id=?",
                (self.tenant_id, str(appeal_id))).fetchone()
        if not row:
            raise AppealError(f"申诉不存在：{appeal_id}")
        return dict(row)

    def list(self, store_id: str, status: Optional[str] = None,
             task_id: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        store_id = self._require_text(store_id, "store_id")
        if status is not None and status not in APPEAL_STATUSES:
            raise AppealError(f"status 必须是 {'、'.join(APPEAL_STATUSES)} 之一，收到：{status!r}")
        sql = "select * from appeals where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, store_id]
        if status:
            sql += " and status=?"
            params.append(status)
        if task_id:
            sql += " and task_id=?"
            params.append(str(task_id))
        sql += " order by created_at, appeal_id limit ?"
        params.append(int(limit))
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def blocker_for(self, store_id: str, task_id: Any) -> Optional[dict[str, Any]]:
        """该任务是否有未结案申诉（有则奖惩不得生效）。"""
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        store_id = self._require_text(store_id, "store_id")
        with self._connection() as conn:
            row = conn.execute(
                "select * from appeals where tenant_id=? and store_id=? and task_id=? "
                "and status='pending' order by created_at limit 1",
                (self.tenant_id, store_id, task_id)).fetchone()
        return dict(row) if row else None