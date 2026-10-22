#!/usr/bin/env python3
"""团队与任务认领（第 10 期）。

边界（与总方案第 10 期一致）：
1. 团队领导 = **数字员工扮演 + 人工兜底**（`leader_employee_id` + `human_fallback`）。
2. 任务机制 = **指派 + 认领**：`assign()` 直接指派，`claim()` 让成员认领；
   **不引入竞标与血条赛制**（计划明确不做）。
3. 团队任务状态机：`offered → claimed → completed`，`offered/claimed → canceled`；非法转换一律拒绝。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Database

TEAM_STATUSES = ("active", "archived")
MEMBER_TYPES = ("digital_employee", "human")
TEAM_TASK_STATUSES = ("offered", "claimed", "completed", "canceled")
ALLOWED_TASK_TRANSITIONS: dict[str, set[str]] = {
    "offered": {"claimed", "completed", "canceled"},
    "claimed": {"completed", "canceled"},
    "completed": set(),
    "canceled": set(),
}


class TeamError(ValueError):
    """团队的参数或状态错误。"""


class TeamStore:
    """`teams` / `team_members` / `team_tasks` 的读写。"""

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
            conn.executescript("""
                create table if not exists teams (
                  tenant_id text not null default '',
                  team_id text not null,
                  store_id text not null,
                  team_key text not null,
                  name text not null,
                  leader_employee_id text,
                  human_fallback text not null default '',
                  status text not null default 'active' check (status in ('active','archived')),
                  created_at text not null,
                  primary key (tenant_id, team_id),
                  unique (tenant_id, store_id, team_key)
                );
                create table if not exists team_members (
                  tenant_id text not null default '',
                  team_id text not null,
                  member_id text not null,
                  member_type text not null check (member_type in ('digital_employee','human')),
                  member_ref text not null,
                  member_role text not null default 'member',
                  joined_at text not null,
                  primary key (tenant_id, team_id, member_id)
                );
                create table if not exists team_tasks (
                  tenant_id text not null default '',
                  team_task_id text not null,
                  team_id text not null,
                  store_id text not null,
                  task_id text not null,
                  title text not null,
                  assignee_ref text,
                  status text not null default 'offered'
                    check (status in ('offered','claimed','completed','canceled')),
                  assigned_by text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  claimed_at text,
                  primary key (tenant_id, team_task_id),
                  unique (tenant_id, team_id, task_id)
                );
                create index if not exists idx_team_tasks_scope
                  on team_tasks(tenant_id, team_id, status);
                """)

    def _require_text(self, value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise TeamError(f"{field} 不能为空")
        return text

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ---------------- 团队 ----------------

    def create_team(self, store_id: str, team_key: str, name: str, *,
                    leader_employee_id: Optional[str] = None, human_fallback: str = "") -> dict[str, Any]:
        """建团队：领导可由数字员工扮演，同时登记人工兜底负责人（混合制）。"""
        store_id = self._require_text(store_id, "store_id")
        team_key = self._require_text(team_key, "team_key")
        name = self._require_text(name, "name")
        with self._connection() as conn:
            existing = conn.execute(
                "select * from teams where tenant_id=? and store_id=? and team_key=?",
                (self.tenant_id, store_id, team_key)).fetchone()
            if existing:
                return dict(existing)
            team_id = f"team-{uuid.uuid4().hex[:12]}"
            conn.execute(
                """insert into teams (tenant_id,team_id,store_id,team_key,name,leader_employee_id,
                   human_fallback,status,created_at) values (?,?,?,?,?,?,?,'active',?)""",
                (self.tenant_id, team_id, store_id, team_key, name,
                 str(leader_employee_id or "") or None, str(human_fallback or ""), self._now()))
        return self.get_team(team_id)

    def get_team(self, team_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from teams where tenant_id=? and team_id=?",
                               (self.tenant_id, str(team_id))).fetchone()
            if not row:
                raise TeamError(f"团队不存在：{team_id}")
            record = dict(row)
            record["members"] = [dict(item) for item in conn.execute(
                "select * from team_members where tenant_id=? and team_id=? order by joined_at, member_id",
                (self.tenant_id, str(team_id))).fetchall()]
        return record

    def list_teams(self, store_id: str) -> list[dict[str, Any]]:
        store_id = self._require_text(store_id, "store_id")
        with self._connection() as conn:
            rows = conn.execute("select team_id from teams where tenant_id=? and store_id=? order by team_key",
                                (self.tenant_id, store_id)).fetchall()
        return [self.get_team(row["team_id"]) for row in rows]

    def add_member(self, team_id: str, member_type: str, member_ref: str, *,
                   member_role: str = "member") -> dict[str, Any]:
        self.get_team(team_id)
        if member_type not in MEMBER_TYPES:
            raise TeamError(f"member_type 必须是 {'、'.join(MEMBER_TYPES)} 之一，收到：{member_type!r}")
        member_ref = self._require_text(member_ref, "member_ref")
        member_id = f"tm-{uuid.uuid4().hex[:12]}"
        with self._connection() as conn:
            conn.execute(
                """insert into team_members (tenant_id,team_id,member_id,member_type,member_ref,
                   member_role,joined_at) values (?,?,?,?,?,?,?)""",
                (self.tenant_id, str(team_id), member_id, member_type, member_ref,
                 str(member_role or "member"), self._now()))
        return self.get_team(team_id)

    # ---------------- 团队任务（指派 + 认领） ----------------

    def assign(self, team_id: str, task_id: str, title: str, *,
               assignee_ref: Optional[str] = None, assigned_by: str = "") -> dict[str, Any]:
        """指派任务（assignee_ref 为空表示"挂到团队池里等人认领"）。"""
        team = self.get_team(team_id)
        task_id = self._require_text(task_id, "task_id")
        status = "claimed" if assignee_ref else "offered"
        team_task_id = f"tt-{uuid.uuid4().hex[:12]}"
        moment = self._now()
        with self._connection() as conn:
            conn.execute(
                """insert into team_tasks (tenant_id,team_task_id,team_id,store_id,task_id,title,
                   assignee_ref,status,assigned_by,created_at,updated_at,claimed_at)
                   values (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.tenant_id, team_task_id, team["team_id"], team["store_id"], task_id,
                 self._require_text(title, "title"), str(assignee_ref or "") or None, status,
                 str(assigned_by or ""), moment, moment, moment if assignee_ref else None))
        return self.get_task(team_task_id)

    def claim(self, team_task_id: str, claimant: str) -> dict[str, Any]:
        """认领：只有 offered 可被认领（不引入竞标，先到先得）。"""
        record = self.get_task(team_task_id)
        if record["status"] != "offered":
            raise TeamError(f"团队任务 {team_task_id} 当前状态为 {record['status']}，不能被认领")
        claimant = self._require_text(claimant, "claimant")
        moment = self._now()
        with self._connection() as conn:
            conn.execute(
                "update team_tasks set status='claimed', assignee_ref=?, claimed_at=?, updated_at=? "
                "where tenant_id=? and team_task_id=?",
                (claimant, moment, moment, self.tenant_id, str(team_task_id)))
        return self.get_task(team_task_id)

    def complete(self, team_task_id: str, *, actor: str = "") -> dict[str, Any]:
        return self._transition(team_task_id, "completed", actor=actor)

    def cancel(self, team_task_id: str, *, actor: str = "", reason: str = "") -> dict[str, Any]:
        return self._transition(team_task_id, "canceled", actor=actor or reason)

    def _transition(self, team_task_id: str, to_status: str, *, actor: str = "") -> dict[str, Any]:
        record = self.get_task(team_task_id)
        if to_status not in TEAM_TASK_STATUSES:
            raise TeamError(f"未知状态：{to_status}")
        allowed = ALLOWED_TASK_TRANSITIONS.get(record["status"], set())
        if to_status not in allowed:
            raise TeamError(f"团队任务 {team_task_id} 不能从 {record['status']} 变更为 {to_status}"
                            f"（允许：{'、'.join(sorted(allowed)) or '无'}）")
        with self._connection() as conn:
            conn.execute("update team_tasks set status=?, updated_at=? where tenant_id=? and team_task_id=?",
                         (to_status, self._now(), self.tenant_id, str(team_task_id)))
        return self.get_task(team_task_id)

    def get_task(self, team_task_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from team_tasks where tenant_id=? and team_task_id=?",
                               (self.tenant_id, str(team_task_id))).fetchone()
        if not row:
            raise TeamError(f"团队任务不存在：{team_task_id}")
        return dict(row)

    def list_tasks(self, team_id: str, status: Optional[str] = None) -> list[dict[str, Any]]:
        self.get_team(team_id)
        if status is not None and status not in TEAM_TASK_STATUSES:
            raise TeamError(f"status 必须是 {'、'.join(TEAM_TASK_STATUSES)} 之一，收到：{status!r}")
        sql = "select * from team_tasks where tenant_id=? and team_id=?"
        params: list[Any] = [self.tenant_id, str(team_id)]
        if status:
            sql += " and status=?"
            params.append(status)
        sql += " order by created_at, team_task_id"
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]