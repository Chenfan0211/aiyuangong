#!/usr/bin/env python3
"""任务与证据库：把规则引擎生成的动作草稿变成可查询、可验收、可审计的任务。

与平台指标库分库（默认 data/store_ops.db）：平台数据可以由导出文件重建，
但任务、证据、状态变更属于人工处置过程，不可重建，因此单独存储与备份。

关键约束：
  - `source_fingerprint = sha256(store_id|date|category|task_id)`，同一天同一对象不会重复派单；
  - 重新生成计划只刷新任务的描述性字段，**不覆盖人工推进后的状态**；
  - `done` / `verified` 必须已有证据记录（对应"仅回复'做完了'不算完成"的门店 SOP）；
  - 每次状态变化都写入 task_events，`overdue` 由扫描任务标记。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Connection, Database

STATUSES = ("proposed", "ready", "accepted", "done", "verified", "rejected", "overdue", "canceled")
OPEN_STATUSES = ("proposed", "ready", "accepted", "overdue")
EVIDENCE_REQUIRED_STATUSES = ("done", "verified")
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"ready", "rejected", "canceled"},          # ready = 审批通过
    "ready": {"accepted", "done", "rejected", "canceled"},
    "accepted": {"done", "rejected", "canceled"},
    "done": {"verified", "rejected"},
    "verified": set(),
    "rejected": {"ready", "canceled"},                      # 允许返工重开
    "overdue": {"accepted", "done", "rejected", "canceled"},
    "canceled": set(),
}


class TaskStoreError(ValueError):
    """任务状态非法、任务不存在等业务错误。"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def source_fingerprint(store_id: str, day: str, category: str, task_id: str) -> str:
    raw = f"{store_id}|{day}|{category}|{task_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TaskStore:
    def __init__(self, db_target, tenant_id: str = ""):
        """`db_target` 可以是 SQLite 文件路径，也可以是 postgresql:// DSN。"""
        self.db = Database(db_target, tenant_id=tenant_id)
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate()

    def _migrate(self) -> None:
        """老库迁移：补 tenant_id 列与租户级唯一索引（PostgreSQL 侧由 schema.sql 保证）。"""
        with self._connection() as conn:
            self.db.ensure_tenant_column(
                conn, "action_tasks",
                ("ux_action_tasks_runtime", ("tenant_id", "store_id", "source_fingerprint")))
            for table in ("task_evidence", "task_events", "agent_sessions"):
                self.db.ensure_tenant_column(conn, table)

    def _connection(self):
        """统一走 Database：提交/回滚后必定关闭连接（详见 db.py）。"""
        return self.db.connection()

    def _migrate(self) -> None:
        with self._connection() as conn:
            self.db.ensure_tenant_column(
                conn, "action_tasks",
                ("ux_action_tasks_runtime", ("tenant_id", "store_id", "source_fingerprint")))
            for table in ("task_evidence", "task_events", "agent_sessions"):
                self.db.ensure_tenant_column(conn, table)

    def _init_schema(self) -> None:
        if self.db.dialect == "postgresql":
            with self._connection() as conn:
                self.db.apply_schema(conn)
            return
        with self._connection() as conn:
            conn.executescript(
                """
                create table if not exists action_tasks (
  tenant_id text not null default '',
                  task_id text primary key,
                  store_id text not null,
                  source_fingerprint text not null,
                  category text not null,
                  title text not null,
                  assignee_id text not null,
                  priority text not null,
                  status text not null,
                  target_json text not null,
                  reason text not null,
                  evidence_required_json text not null,
                  approval_required integer not null default 0,
                  rule_key text not null default '',
                  rule_version text not null default '',
                  due_at text not null,
                  created_at text not null,
                  updated_at text not null,
                  unique (store_id, source_fingerprint)
                );
                create index if not exists idx_action_tasks_store_status
                  on action_tasks(store_id, status, due_at);

                create table if not exists task_evidence (
  tenant_id text not null default '',
                  id integer primary key autoincrement,
                  task_id text not null,
                  evidence_type text not null,
                  object_ref text not null,
                  note text,
                  submitted_by text not null,
                  submitted_at text not null
                );
                create index if not exists idx_task_evidence_task on task_evidence(task_id, id);

                create table if not exists task_events (
  tenant_id text not null default '',
                  id integer primary key autoincrement,
                  task_id text not null,
                  from_status text,
                  to_status text not null,
                  actor_id text not null,
                  reason text,
                  created_at text not null
                );
                create index if not exists idx_task_events_task on task_events(task_id, id);

                create table if not exists agent_sessions (
  tenant_id text not null default '',
                  session_id text primary key,
                  user_id text not null,
                  user_role text not null,
                  created_at text not null,
                  last_message_at text not null
                );
                """
            )

    # ---------------- 计划落库 ----------------

    def save_plan(self, plan: dict[str, Any], store_id: str) -> dict[str, Any]:
        """把 generate_daily_plan() 的输出落库；同一天同一对象不重复派单，人工进度不被覆盖。"""
        day = str(plan.get("date") or "")
        if not day:
            raise TaskStoreError("计划缺少 date 字段，无法落库")
        created = 0
        updated = 0
        task_ids: list[str] = []
        now = now_iso()
        with self._connection() as conn:
            for task in plan.get("tasks") or []:
                fingerprint = source_fingerprint(store_id, day, task["category"], task["task_id"])
                existing = conn.execute(
                    "select status from action_tasks where store_id=? and source_fingerprint=?",
                    (store_id, fingerprint),
                ).fetchone()
                params = {
                    "task_id": task["task_id"],
                    "store_id": store_id,
                    "source_fingerprint": fingerprint,
                    "category": task["category"],
                    "title": task["title"],
                    "assignee_id": task["assignee"],
                    "priority": task["priority"],
                    "target_json": json.dumps(task.get("target") or {}, ensure_ascii=False),
                    "reason": task["reason"],
                    "evidence_required_json": json.dumps(task.get("evidence_required") or [], ensure_ascii=False),
                    "approval_required": bool(task.get("requires_approval")),
                    "rule_key": str(task.get("rule_key") or ""),
                    "rule_version": str(task.get("rule_version") or ""),
                    "due_at": task["due_at"],
                    "updated_at": now,
                }
                if existing:
                    conn.execute(
                        """update action_tasks set title=:title, assignee_id=:assignee_id, priority=:priority,
                           target_json=:target_json, reason=:reason, evidence_required_json=:evidence_required_json,
                           approval_required=:approval_required, rule_key=:rule_key,
                           rule_version=:rule_version, due_at=:due_at, updated_at=:updated_at
                           where store_id=:store_id and source_fingerprint=:source_fingerprint""",
                        params,
                    )
                    updated += 1
                else:
                    params["status"] = "proposed" if task.get("requires_approval") else "ready"
                    params["created_at"] = now
                    conn.execute(
                        """insert into action_tasks
                           (task_id,store_id,source_fingerprint,category,title,assignee_id,priority,status,
                            target_json,reason,evidence_required_json,approval_required,rule_key,rule_version,
                            due_at,created_at,updated_at)
                           values (:task_id,:store_id,:source_fingerprint,:category,:title,:assignee_id,:priority,
                                   :status,:target_json,:reason,:evidence_required_json,:approval_required,
                                   :rule_key,:rule_version,:due_at,:created_at,:updated_at)""",
                        params,
                    )
                    conn.execute(
                        """insert into task_events(task_id,from_status,to_status,actor_id,reason,created_at)
                           values(?,?,?,?,?,?)""",
                        (task["task_id"], None, params["status"], "system:daily-plan",
                         "任务由规则引擎生成", now),
                    )
                    created += 1
                task_ids.append(task["task_id"])
        return {"created": created, "updated": updated, "task_ids": task_ids}

    def mark_overdue(self, store_id: str, moment: Optional[str] = None) -> int:
        """把已过截止时间且未关闭的任务标记为 overdue，返回被标记数量。"""
        moment = moment or now_iso()
        marked = 0
        with self._connection() as conn:
            rows = conn.execute(
                """select task_id,status from action_tasks
                   where store_id=? and due_at < ? and status in ('ready','accepted','done')
                   order by due_at""",
                (store_id, moment),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "update action_tasks set status='overdue', updated_at=? where task_id=?",
                    (moment, row["task_id"]),
                )
                conn.execute(
                    """insert into task_events(task_id,from_status,to_status,actor_id,reason,created_at)
                       values(?,?,?,?,?,?)""",
                    (row["task_id"], row["status"], "overdue", "system:overdue-sweep",
                     "超过截止时间仍未完成", moment),
                )
                marked += 1
        return marked

    # ---------------- 查询 ----------------

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from action_tasks where task_id=?", (task_id,)).fetchone()
        if not row:
            raise TaskStoreError(f"任务不存在：{task_id}")
        return self._task_row(row)

    def list_tasks(
        self,
        store_id: str,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "select * from action_tasks where store_id=?"
        params: list[Any] = [store_id]
        if status:
            if status == "open":
                placeholders = ",".join("?" for _ in OPEN_STATUSES)
                sql += f" and status in ({placeholders})"
                params.extend(OPEN_STATUSES)
            else:
                if status not in STATUSES:
                    raise TaskStoreError(f"未知状态：{status}")
                sql += " and status=?"
                params.append(status)
        if assignee:
            sql += " and assignee_id=?"
            params.append(assignee)
        sql += " order by case priority when 'urgent' then 0 when 'high' then 1 when 'medium' then 2 else 3 end, due_at"
        sql += " limit ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._task_row(row) for row in rows]

    def count_by_status(self, store_id: str) -> dict[str, int]:
        with self._connection() as conn:
            rows = conn.execute(
                "select status, count(*) as total from action_tasks where store_id=? group by status",
                (store_id,),
            ).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

    def overdue_tasks(self, store_id: str) -> list[dict[str, Any]]:
        """供快照使用：已逾期任务的精简表示（含被标记次数）。"""
        with self._connection() as conn:
            rows = conn.execute(
                """select t.task_id, t.assignee_id, t.title, t.due_at,
                          (select count(*) from task_events e
                            where e.task_id=t.task_id and e.to_status='overdue') as overdue_times
                   from action_tasks t
                   where t.store_id=? and t.status='overdue'
                   order by t.due_at""",
                (store_id,),
            ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "employee_id": row["assignee_id"],
                "missed_count": max(1, int(row["overdue_times"])),
                "title": row["title"],
                "due_at": row["due_at"],
            }
            for row in rows
        ]

    def events(self, task_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "select * from task_events where task_id=? order by id", (task_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def evidence(self, task_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "select * from task_evidence where task_id=? order by id", (task_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _task_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["target"] = json.loads(item.pop("target_json"))
        item["evidence_required"] = json.loads(item.pop("evidence_required_json"))
        item["approval_required"] = bool(item["approval_required"])
        return item

    # ---------------- 状态与证据 ----------------

    def change_status(
        self,
        task_id: str,
        to_status: str,
        actor_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        if to_status not in STATUSES:
            raise TaskStoreError(f"未知状态：{to_status}（可用：{'、'.join(STATUSES)}）")
        task = self.get_task(task_id)
        allowed = ALLOWED_TRANSITIONS.get(task["status"], set())
        if to_status not in allowed:
            raise TaskStoreError(
                f"任务 {task_id} 不能从 {task['status']} 变更为 {to_status}（允许：{'、'.join(sorted(allowed)) or '无'}）"
            )
        if to_status in EVIDENCE_REQUIRED_STATUSES and not self.evidence(task_id):
            raise TaskStoreError(f"任务 {task_id} 还没有提交任何证据，不能标记为 {to_status}")
        moment = now_iso()
        with self._connection() as conn:
            conn.execute(
                "update action_tasks set status=?, updated_at=? where task_id=?",
                (to_status, moment, task_id),
            )
            conn.execute(
                """insert into task_events(task_id,from_status,to_status,actor_id,reason,created_at)
                   values(?,?,?,?,?,?)""",
                (task_id, task["status"], to_status, actor_id, reason or None, moment),
            )
        return self.get_task(task_id)

    def add_evidence(
        self,
        task_id: str,
        evidence_type: str,
        object_ref: str,
        submitted_by: str,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not evidence_type or not object_ref:
            raise TaskStoreError("证据必须包含 evidence_type 与 object_ref（脱敏单号/截图引用等）")
        moment = now_iso()
        with self._connection() as conn:
            evidence_id = self.db.insert_returning_id(
                conn,
                """insert into task_evidence(task_id,evidence_type,object_ref,note,submitted_by,submitted_at)
                   values(?,?,?,?,?,?)""",
                (task_id, evidence_type, object_ref, note, submitted_by, moment),
            )
            conn.execute(
                """insert into task_events(task_id,from_status,to_status,actor_id,reason,created_at)
                   values(?,?,?,?,?,?)""",
                (task_id, task["status"], task["status"], submitted_by, f"提交证据：{evidence_type}", moment),
            )
        return {"evidence_id": evidence_id, "task_id": task_id, "submitted_at": moment}

    # ---------------- 演示期会话绑定 ----------------

    def bind_session(self, session_id: str, user_id: str, user_role: str) -> dict[str, Any]:
        """把会话绑定到身份；同一会话不允许中途换成别人（避免前端直传 user_id 冒充）。"""
        moment = now_iso()
        with self._connection() as conn:
            existing = conn.execute(
                "select * from agent_sessions where session_id=?", (session_id,)
            ).fetchone()
            if existing and existing["user_id"] != user_id:
                raise TaskStoreError(
                    f"会话 {session_id} 已绑定到 {existing['user_id']}，不能切换为 {user_id}"
                )
            if existing:
                conn.execute(
                    "update agent_sessions set last_message_at=? where session_id=?",
                    (moment, session_id),
                )
            else:
                conn.execute(
                    """insert into agent_sessions(session_id,user_id,user_role,created_at,last_message_at)
                       values(?,?,?,?,?)""",
                    (session_id, user_id, user_role, moment, moment),
                )
        return self.session(session_id)

    def session(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("select * from agent_sessions where session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None
