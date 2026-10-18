#!/usr/bin/env python3
"""治理层（二期批次 9）：审批流、工具调用留痕与审计查询。

三块职责，各一个类：

* `ApprovalStore`  —— 高风险动作的审批单：请求、决策、与任务状态机联动；
                     同一任务同一动作**不会重复开单**（幂等键），被驳回后可重新申请。
* `ToolRunLog`     —— Agent 每次只读工具的调用留痕（谁、在哪个会话、读了什么、结果大小），
                     入参出参一律脱敏后落库。
* `AuditCenter`    —— 只读审计查询：事件流水、工具调用、按事件类型统计。

设计约定：
  * 表在 SQLite 侧由本模块内联 DDL 建立；PostgreSQL 侧执行权威 `schema.sql`（与其它 store 一致）；
  * 所有业务主键由 Python 生成（`apr-` / `run-` 前缀），不依赖数据库自增；
  * 审批决策与任务状态机在 `apply_decision()` 里联动：通过→任务 ready，驳回→任务 rejected；
    若任务已被别人推进（状态不再是 proposed），审批仍然生效，只是跳过任务流转并回报原因。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from auth import redact_payload
from db import Database
from task_store import TaskStore, TaskStoreError

APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "canceled")
DECISIONS = ("approved", "rejected")
TOOL_MODES = ("read", "draft", "execute")
TOOL_STATUSES = ("started", "success", "blocked", "failed")

APPROVAL_DDL = """
create table if not exists approvals (
  approval_id text primary key,
  tenant_id text not null default '',
  store_id text not null,
  task_id text,
  action_type text not null,
  payload_snapshot_json text not null,
  status text not null check (status in ('pending','approved','rejected','expired','canceled')),
  requested_by text not null,
  requested_at text not null,
  decided_by text,
  decided_at text,
  decision_reason text,
  idempotency_key text not null,
  unique (tenant_id, store_id, idempotency_key)
);
create index if not exists idx_approvals_queue on approvals(store_id, status, requested_at);
"""

TOOL_RUN_DDL = """
create table if not exists agent_tool_runs (
  run_id text primary key,
  tenant_id text not null default '',
  store_id text not null,
  session_id text,
  tool_name text not null,
  mode text not null check (mode in ('read','draft','execute')),
  actor_id text not null,
  input_redacted_json text,
  output_redacted_json text,
  status text not null check (status in ('started','success','blocked','failed')),
  error text,
  created_at text not null,
  finished_at text
);
create index if not exists idx_tool_runs_store on agent_tool_runs(store_id, created_at);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(redact_payload(value), ensure_ascii=False)


def _fingerprint(tenant_id: str, store_id: str, task_id: Optional[str], action_type: str,
                 payload: Any = None) -> str:
    """审批单幂等键：任务类只认"门店+任务+动作"，临时请求把载荷也纳入。"""
    material = f"{tenant_id}|{store_id}|{task_id or ''}|{action_type}"
    if not task_id:
        material += "|" + json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ApprovalStore:
    def __init__(self, db_target: Any, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate()

    def _connection(self):
        return self.db.connection()

    def _migrate(self) -> None:
        with self._connection() as conn:
            self.db.ensure_tenant_column(conn, "approvals")

    def _init_schema(self) -> None:
        if self.db.dialect == "postgresql":
            with self._connection() as conn:
                self.db.apply_schema(conn)
            return
        with self._connection() as conn:
            conn.executescript(APPROVAL_DDL)

    # ---------------- 请求 ----------------

    def request(
        self,
        store_id: str,
        action_type: str,
        payload: Optional[dict[str, Any]] = None,
        requested_by: str = "",
        task_id: Optional[str] = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """开一张审批单；同一任务同一动作不会重复开单（返回已有的 pending 单）。"""
        tenant_id = self.db.tenant_id
        payload = payload or {}
        base_key = _fingerprint(tenant_id, store_id, task_id, action_type, payload)
        with self._connection() as conn:
            rows = conn.execute(
                "select * from approvals where tenant_id=? and store_id=? and idempotency_key like ?",
                (tenant_id, store_id, f"{base_key}%"),
            ).fetchall()
            for row in rows:
                if row["status"] == "pending":
                    result = self._row(row)
                    result["skipped"] = True
                    return result

            idempotency_key = base_key if not rows else f"{base_key}-r{len(rows) + 1}"
            approval_id = f"apr-{secrets.token_hex(6)}"
            moment = now_iso()
            conn.execute(
                """insert into approvals(approval_id,tenant_id,store_id,task_id,action_type,
                   payload_snapshot_json,status,requested_by,requested_at,idempotency_key)
                   values(?,?,?,?,?,?,?,?,?,?)""",
                (approval_id, tenant_id, store_id, task_id, action_type, _json(payload),
                 "pending", requested_by, moment, idempotency_key),
            )
        created = self.get(approval_id)
        created["skipped"] = False
        created["reason"] = reason
        return created

    def sync_from_plan(self, plan: dict[str, Any], store_id: str, requested_by: str = "system:daily-plan") -> dict[str, Any]:
        """把计划里所有"需审批"的任务转成待审批单（幂等，可随计划重复执行）。"""
        created = 0
        skipped = 0
        for task in plan.get("tasks") or []:
            if not task.get("requires_approval"):
                continue
            approval = self.request(
                store_id=store_id,
                action_type=f"task:{task['category']}",
                payload={
                    "task_id": task["task_id"],
                    "title": task["title"],
                    "target": task.get("target") or {},
                    "due_at": task["due_at"],
                    "evidence_required": task.get("evidence_required") or [],
                },
                requested_by=requested_by,
                task_id=task["task_id"],
                reason=task.get("reason", ""),
            )
            if approval["skipped"]:
                skipped += 1
            else:
                created += 1
        return {"created": created, "skipped": skipped,
                "pending": len(self.list(store_id, status="pending", limit=500))}

    # ---------------- 决策 ----------------

    def decide(self, approval_id: str, decision: str, decided_by: str, reason: str = "") -> dict[str, Any]:
        if decision not in DECISIONS:
            raise ValueError(f"未知决策：{decision}（可用：{'、'.join(DECISIONS)}）")
        approval = self.get(approval_id)
        if approval["status"] != "pending":
            raise ValueError(f"审批单 {approval_id} 已是 {approval['status']}，不能重复决策")
        if decision == "rejected" and not reason.strip():
            raise ValueError("驳回必须填写原因")
        moment = now_iso()
        with self._connection() as conn:
            conn.execute(
                """update approvals set status=?, decided_by=?, decided_at=?, decision_reason=?
                   where approval_id=? and status='pending'""",
                (decision, decided_by, moment, reason.strip() or None, approval_id),
            )
        return self.get(approval_id)

    def get(self, approval_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from approvals where approval_id=?", (approval_id,)).fetchone()
        if not row:
            raise ValueError(f"审批单不存在：{approval_id}")
        return self._row(row)

    def list(self, store_id: str, status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "select * from approvals where store_id=?"
        params: list[Any] = [store_id]
        if status:
            if status not in APPROVAL_STATUSES:
                raise ValueError(f"未知审批状态：{status}")
            sql += " and status=?"
            params.append(status)
        sql += " order by requested_at desc limit ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def count_by_status(self, store_id: str) -> dict[str, int]:
        with self._connection() as conn:
            rows = conn.execute(
                "select status, count(*) as total from approvals where store_id=? group by status",
                (store_id,),
            ).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["payload_snapshot"] = json.loads(item.pop("payload_snapshot_json") or "{}")
        return item


def apply_decision(
    approval_store: ApprovalStore,
    task_store: TaskStore,
    approval_id: str,
    decision: str,
    decided_by: str,
    reason: str = "",
) -> dict[str, Any]:
    """决策并联动任务状态机：通过 → ready；驳回 → rejected。"""
    approval = approval_store.decide(approval_id, decision, decided_by, reason)
    result: dict[str, Any] = {"approval": approval, "task": None, "task_note": None}
    task_id = approval.get("task_id")
    if not task_id:
        return result
    target_status = "ready" if decision == "approved" else "rejected"
    try:
        result["task"] = task_store.change_status(task_id, target_status, decided_by,
                                                 reason or f"审批{decision}")
    except TaskStoreError as exc:
        result["task_note"] = f"审批已记录，但任务未流转：{exc}"
    return result


def log_tool_call(
    tool_log: Optional["ToolRunLog"],
    store_id: str,
    tool_name: str,
    actor_id: str = "",
    session_id: Optional[str] = None,
    input_payload: Any = None,
    output_payload: Any = None,
    status: str = "success",
    error: Optional[str] = None,
) -> Optional[str]:
    """记录一次工具调用。

    留痕是**旁路**：未配置 tool_log 时什么也不做，写入失败也不影响主流程
    （审计的价值不能以牺牲可用性为代价），因此这里吞掉异常。
    """
    if tool_log is None:
        return None
    try:
        return tool_log.record(
            store_id=store_id, tool_name=tool_name, mode="read", actor_id=actor_id,
            session_id=session_id, input_payload=input_payload, output_payload=output_payload,
            status=status, error=error,
        )
    except Exception:                                  # noqa: BLE001 - 留痕失败不影响业务
        return None


class ToolRunLog:
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
            conn.executescript(TOOL_RUN_DDL)
        with self._connection() as conn:
            self.db.ensure_tenant_column(conn, "agent_tool_runs")

    def record(
        self,
        store_id: str,
        tool_name: str,
        mode: str = "read",
        actor_id: str = "",
        session_id: Optional[str] = None,
        input_payload: Any = None,
        output_payload: Any = None,
        status: str = "success",
        error: Optional[str] = None,
    ) -> str:
        if mode not in TOOL_MODES:
            raise ValueError(f"未知工具模式：{mode}")
        if status not in TOOL_STATUSES:
            raise ValueError(f"未知工具状态：{status}")
        run_id = f"run-{secrets.token_hex(6)}"
        moment = now_iso()
        with self._connection() as conn:
            conn.execute(
                """insert into agent_tool_runs(run_id,tenant_id,store_id,session_id,tool_name,mode,
                   actor_id,input_redacted_json,output_redacted_json,status,error,created_at,finished_at)
                   values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, self.db.tenant_id, store_id, session_id, tool_name, mode, actor_id,
                 _json(input_payload) if input_payload is not None else None,
                 _json(output_payload) if output_payload is not None else None,
                 status, error, moment, moment),
            )
        return run_id

    def list(self, store_id: str, limit: int = 100, tool_name: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "select * from agent_tool_runs where store_id=?"
        params: list[Any] = [store_id]
        if tool_name:
            sql += " and tool_name=?"
            params.append(tool_name)
        if self.db.dialect == "sqlite":
            sql += " order by created_at desc, rowid desc"
        else:
            sql += " order by created_at desc"
        sql += " limit ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._tool_row(row) for row in rows]

    def count_by_tool(self, store_id: str, limit: int = 8) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """select tool_name, count(*) as total from agent_tool_runs
                   where store_id=? group by tool_name order by total desc limit ?""",
                (store_id, limit),
            ).fetchall()
        return [{"tool_name": row["tool_name"], "total": int(row["total"])} for row in rows]

    @staticmethod
    def _tool_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["input_redacted"] = json.loads(item.pop("input_redacted_json") or "null")
        item["output_redacted"] = json.loads(item.pop("output_redacted_json") or "null")
        return item


class AuditCenter:
    """只读审计查询：事件流水、工具调用与统计。"""

    def __init__(self, db_target: Any, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)

    def _connection(self):
        return self.db.connection()

    def list_events(
        self,
        store_id: str,
        limit: int = 100,
        event_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        sql = "select * from audit_events where store_id=?"
        params: list[Any] = [store_id]
        if event_type:
            sql += " and event_type=?"
            params.append(event_type)
        if actor_id:
            sql += " and actor_id=?"
            params.append(actor_id)
        if since:
            sql += " and created_at >= ?"
            params.append(since)
        sql += " order by created_at desc, id desc"
        sql += " limit ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._event_row(row) for row in rows]

    def event_types(self, store_id: str) -> list[str]:
        with self._connection() as conn:
            rows = conn.execute(
                "select distinct event_type from audit_events where store_id=? order by event_type",
                (store_id,),
            ).fetchall()
        return [row["event_type"] for row in rows]

    def summary(self, store_id: str, days: int = 7) -> dict[str, Any]:
        if days < 1 or days > 365:
            raise ValueError("统计周期必须在 1 到 365 天之间")
        since = (datetime.now().timestamp() - days * 86400)
        since_iso = datetime.fromtimestamp(since).isoformat(timespec="seconds")
        with self._connection() as conn:
            rows = conn.execute(
                """select event_type, count(*) as total from audit_events
                   where store_id=? and created_at >= ? group by event_type order by total desc""",
                (store_id, since_iso),
            ).fetchall()
            actors = conn.execute(
                """select actor_id, count(*) as total from audit_events
                   where store_id=? and created_at >= ? group by actor_id order by total desc limit 10""",
                (store_id, since_iso),
            ).fetchall()
        counts = {row["event_type"]: int(row["total"]) for row in rows}
        return {
            "store_id": store_id,
            "days": days,
            "since": since_iso,
            "total": sum(counts.values()),
            "count_by_event": counts,
            "top_actors": [{"actor_id": row["actor_id"], "total": int(row["total"])} for row in actors],
            "permission_denied": counts.get("permission_denied", 0),
            "login_failed": counts.get("login_failed", 0) + counts.get("login_locked", 0),
        }

    @staticmethod
    def _event_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["before"] = json.loads(item.pop("before_json") or "null")
        item["after"] = json.loads(item.pop("after_json") or "null")
        return item
