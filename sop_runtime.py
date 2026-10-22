#!/usr/bin/env python3
"""SOP 执行状态机（第 7 期）：8 态状态机 + 断点快照 + 恢复 + 超时。

三条硬约定（与总方案第 7 期一致）：
1. **8 态状态机**，非法转换一律拒绝（`ALLOWED_RUN_TRANSITIONS`）。
2. **断点续跑**：每个节点都写 `sop_step_runs`，上下文快照写在 `sop_runs.context_json`；
   `resume()` / `submit_human()` 只从库里恢复，不依赖进程内状态。
3. **边界隔离**：`task` 是派给具体人的工作项（挂在 `sop_step_run` 上），
   `sop_run` 是跨角色流程实例；**task 的状态变更不会直接改写 sop_run 的状态**，
   必须经 `submit_human()`（由任务完成事件调用）才推进流程。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from db import Database
from sop import DEFAULT_MAX_HOURS, SopError, SopStore

RUN_STATUSES = ("pending", "running", "waiting_human", "waiting_approval",
                "completed", "failed", "cancelled", "timed_out")
TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")
ALLOWED_RUN_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "cancelled", "timed_out"},
    "running": {"waiting_human", "waiting_approval", "completed", "failed", "cancelled", "timed_out"},
    "waiting_human": {"running", "failed", "cancelled", "timed_out"},
    "waiting_approval": {"running", "failed", "cancelled", "timed_out"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "timed_out": {"running"},
}
STEP_STATUSES = ("running", "waiting", "completed", "timeout", "failed")


class SopRuntimeError(ValueError):
    """SOP 运行的参数或状态错误。"""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class SopRuntime:
    """`sop_runs` / `sop_step_runs` 的状态机与执行器。"""

    def __init__(self, db_target: Any, tenant_id: str = "", *, task_store: Any = None,
                 approval_store: Any = None, message_store: Any = None,
                 agent_executor: Optional[Callable[[dict[str, Any], dict[str, Any]], Any]] = None):
        self.db = Database(db_target, tenant_id=tenant_id)
        self.tenant_id = tenant_id
        self.task_store = task_store
        self.approval_store = approval_store
        self.message_store = message_store
        self.agent_executor = agent_executor
        self.sops = SopStore(db_target, tenant_id=tenant_id)
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
                create table if not exists sop_runs (
                  tenant_id text not null default '',
                  run_id text not null,
                  sop_id text not null,
                  store_id text not null,
                  sop_version integer not null,
                  status text not null check (status in
                    ('pending','running','waiting_human','waiting_approval','completed','failed','cancelled','timed_out')),
                  current_node text,
                  context_json text not null default '{}',
                  started_at text not null,
                  updated_at text not null,
                  finished_at text,
                  max_hours real not null default 24,
                  error text,
                  primary key (tenant_id, run_id)
                );
                create table if not exists sop_step_runs (
                  tenant_id text not null default '',
                  step_run_id text not null,
                  run_id text not null,
                  node_id text not null,
                  node_type text not null,
                  seq integer not null default 0,
                  status text not null check (status in ('running','waiting','completed','timeout','failed')),
                  task_id text,
                  approval_id text,
                  payload_json text not null default '{}',
                  entered_at text not null,
                  finished_at text,
                  error text,
                  primary key (tenant_id, step_run_id)
                );
                create index if not exists idx_sop_step_runs_run
                  on sop_step_runs(tenant_id, run_id, entered_at);
                """)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ---------------- 读 ----------------

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from sop_runs where tenant_id=? and run_id=?",
                               (self.tenant_id, str(run_id))).fetchone()
        if not row:
            raise SopRuntimeError(f"SOP 运行不存在：{run_id}")
        record = dict(row)
        record["context"] = _loads(record.get("context_json"), {})
        record["steps"] = self.steps(record["run_id"])
        return record

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "select * from sop_step_runs where tenant_id=? and run_id=? order by seq, entered_at, step_run_id",
                (self.tenant_id, str(run_id))).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.get("payload_json"), {})
            items.append(item)
        return items

    # ---------------- 状态机 ----------------

    def transition(self, run_id: str, to_status: str, *, error: Optional[str] = None) -> dict[str, Any]:
        """显式状态转换：非法转换直接拒绝。"""
        if to_status not in RUN_STATUSES:
            raise SopRuntimeError(f"未知状态：{to_status}（可用：{'、'.join(RUN_STATUSES)}）")
        run = self.get_run(run_id)
        allowed = ALLOWED_RUN_TRANSITIONS.get(run["status"], set())
        if to_status not in allowed:
            raise SopRuntimeError(
                f"SOP 运行 {run_id} 不能从 {run['status']} 变更为 {to_status}"
                f"（允许：{'、'.join(sorted(allowed)) or '无'}）")
        moment = self._now()
        finished = moment if to_status in TERMINAL_RUN_STATUSES or to_status == "timed_out" else None
        with self._connection() as conn:
            conn.execute(
                "update sop_runs set status=?, updated_at=?, finished_at=?, error=? where tenant_id=? and run_id=?",
                (to_status, moment, finished, error, self.tenant_id, str(run_id)))
        return self.get_run(run_id)

    # ---------------- 启动与推进 ----------------

    def start(self, sop_id: str, *, store_id: str, context: Optional[dict[str, Any]] = None,
              actor: str = "") -> dict[str, Any]:
        sop = self.sops.get_sop(sop_id)
        if sop["store_id"] != store_id:
            raise SopRuntimeError("SOP 不属于当前门店")
        moment = self._now()
        run_id = f"sopr-{uuid.uuid4().hex[:12]}"
        with self._connection() as conn:
            conn.execute(
                """insert into sop_runs (tenant_id,run_id,sop_id,store_id,sop_version,status,
                   current_node,context_json,started_at,updated_at,max_hours)
                   values (?,?,?,?,?,'running',?,?,?,?,?)""",
                (self.tenant_id, run_id, sop["sop_id"], store_id, sop["version"],
                 sop["graph"]["start_node"], _dump(dict(context or {})), moment, moment,
                 float(sop.get("max_hours") or DEFAULT_MAX_HOURS)))
        return self._advance(run_id, sop["graph"]["start_node"], actor=actor)

    def submit_human(self, run_id: str, *, payload: Optional[dict[str, Any]] = None,
                     actor: str = "", node_id: Optional[str] = None) -> dict[str, Any]:
        """人工节点提交：写回上下文并继续（这是唯一能让 human 节点推进的入口）。"""
        run = self.get_run(run_id)
        if run["status"] != "waiting_human":
            raise SopRuntimeError(f"运行 {run_id} 当前状态为 {run['status']}，不能提交人工节点")
        node = run["current_node"] if node_id is None else str(node_id)
        step = self._current_step(run_id, node)
        if step:
            self._finish_step(step["step_run_id"], "completed", payload={"actor": actor,
                                                                        **dict(payload or {})})
        return self._advance(run_id, node, payload=dict(payload or {}), actor=actor, resume_human=True)

    def resume(self, run_id: str, *, decision: str = "approved",
               payload: Optional[dict[str, Any]] = None, actor: str = "") -> dict[str, Any]:
        """审批恢复（免登录链接或回调都走这里）：rejected 直接失败，approved 继续。"""
        run = self.get_run(run_id)
        if run["status"] != "waiting_approval":
            raise SopRuntimeError(f"运行 {run_id} 当前状态为 {run['status']}，不能恢复审批节点")
        if decision not in ("approved", "rejected"):
            raise SopRuntimeError(f"decision 必须是 approved / rejected，收到：{decision!r}")
        node_id = run["current_node"]
        step = self._current_step(run_id, node_id)
        if step:
            self._finish_step(step["step_run_id"], "completed" if decision == "approved" else "failed",
                              payload={"decision": decision, **dict(payload or {})})
        if decision == "rejected":
            return self.transition(run_id, "failed", error="审批被驳回")
        return self._advance(run_id, node_id, payload=dict(payload or {}), actor=actor,
                             resume_approval=True)

    def cancel(self, run_id: str, *, reason: str = "", actor: str = "") -> dict[str, Any]:
        return self.transition(run_id, "cancelled", error=reason or f"由 {actor or 'system'} 取消")

    def timeout_check(self, run_id: Optional[str] = None) -> dict[str, Any]:
        """超时检查：SOP 级超过 max_hours → timed_out；步骤级超时 → 追加超时出口。"""
        runs = [self.get_run(run_id)] if run_id else self._open_runs()
        results = []
        for run in runs:
            result = self._check_one(run)
            if result:
                results.append(result)
        return {"checked": len(runs), "timed_out": results}

    def _open_runs(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "select run_id from sop_runs where tenant_id=? and status in "
                "('pending','running','waiting_human','waiting_approval')", (self.tenant_id,)).fetchall()
        return [self.get_run(row["run_id"]) for row in rows]

    def _check_one(self, run: dict[str, Any]) -> Optional[dict[str, Any]]:
        started = datetime.fromisoformat(run["started_at"])
        deadline = started + timedelta(hours=float(run.get("max_hours") or DEFAULT_MAX_HOURS))
        if datetime.now() >= deadline:
            self.transition(run["run_id"], "timed_out", error="SOP 级最长执行时长已到")
            return {"run_id": run["run_id"], "reason": "sop_timeout"}
        node = self._node(run, run.get("current_node"))
        minutes = node.get("timeout_minutes") if node else None
        if not minutes:
            return None
        step = self._current_step(run["run_id"], run["current_node"])
        if not step:
            return None
        entered = datetime.fromisoformat(step["entered_at"])
        if datetime.now() < entered + timedelta(minutes=float(minutes)):
            return None
        timeout_step = self._write_step(run["run_id"], node, "timeout",
                                        payload={"reason": "步骤超时"})
        edges = [edge for edge in self._edges(run)
                 if edge["from_node"] == run["current_node"] and str(edge.get("when") or "") == "timeout"]
        if edges:
            self._set_current_node(run["run_id"], run["current_node"])
            self._advance(run["run_id"], edges[0]["to_node"], actor="system:timeout")
            return {"run_id": run["run_id"], "reason": "step_timeout",
                    "step_run_id": timeout_step["step_run_id"], "exit": edges[0]["to_node"]}
        self.transition(run["run_id"], "timed_out", error="步骤超时且没有超时出口")
        return {"run_id": run["run_id"], "reason": "step_timeout_no_exit",
                "step_run_id": timeout_step["step_run_id"]}

    # ---------------- 执行循环 ----------------

    def _advance(self, run_id: str, node_id: str, *, payload: Optional[dict[str, Any]] = None,
                 actor: str = "", resume_human: bool = False,
                 resume_approval: bool = False) -> dict[str, Any]:
        context_add = dict(payload or {})
        if context_add:
            self._merge_context(run_id, context_add)
        if self.get_run(run_id)["status"] in ("waiting_human", "waiting_approval"):
            self.transition(run_id, "running")
        while True:
            run = self.get_run(run_id)
            if run["status"] in TERMINAL_RUN_STATUSES:
                break
            graph = self.sops.version_graph(run["sop_id"], run["sop_version"])["graph"]
            node = next((item for item in graph["nodes"] if str(item.get("node_id")) == str(node_id)), None)
            if node is None:
                raise SopRuntimeError(f"节点不存在：{node_id}")
            node_type = str(node.get("node_type") or "")
            if node_type == "end":
                self._write_step(run_id, node, "completed", payload={"actor": actor})
                self.transition(run_id, "completed")
                break
            if node_type == "agent":
                result = self._run_agent(node, self.get_run(run_id))
                self._write_step(run_id, node, "completed", payload=result)
                if isinstance(result, dict) and result:
                    self._merge_context(run_id, result)
                node_id = self._next_node(graph, node_id, self.get_run(run_id)["context"])
                continue
            if node_type == "human":
                if resume_human:
                    resume_human = False
                    node_id = self._next_node(graph, node_id, self.get_run(run_id)["context"])
                    continue
                step = self._write_step(run_id, node, "waiting", payload={"actor": actor})
                task_id = self._ensure_task(run, node, step)
                self._set_current_node(run_id, node_id, task_id=task_id, step_id=step["step_run_id"])
                self.transition(run_id, "waiting_human")
                break
            if node_type == "approval":
                if resume_approval:
                    resume_approval = False
                    node_id = self._next_node(graph, node_id, self.get_run(run_id)["context"])
                    continue
                step = self._write_step(run_id, node, "waiting", payload={"actor": actor})
                approval_id = self._ensure_approval(run, node, step, actor=actor)
                self._set_current_node(run_id, node_id, approval_id=approval_id,
                                       step_id=step["step_run_id"])
                self.transition(run_id, "waiting_approval")
                break
            if node_type == "condition":
                chosen = self._next_node(graph, node_id, self.get_run(run_id)["context"])
                self._write_step(run_id, node, "completed", payload={"chose": chosen})
                node_id = chosen
                continue
            raise SopRuntimeError(f"不支持的节点类型：{node_type}")
        return self.get_run(run_id)

    def _node(self, run: dict[str, Any], node_id: Any) -> Optional[dict[str, Any]]:
        if not node_id:
            return None
        graph = self.sops.version_graph(run["sop_id"], run["sop_version"])["graph"]
        return next((item for item in graph["nodes"] if str(item.get("node_id")) == str(node_id)), None)

    def _edges(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        return self.sops.version_graph(run["sop_id"], run["sop_version"])["graph"]["edges"]

    def _next_node(self, graph: dict[str, Any], node_id: str, context: dict[str, Any]) -> str:
        edges = [edge for edge in graph["edges"] if str(edge["from_node"]) == str(node_id)]
        node = next((item for item in graph["nodes"] if str(item["node_id"]) == str(node_id)), {})
        if str(node.get("node_type")) == "condition":
            for edge in edges:
                when = str(edge.get("when") or "default").strip()
                if when == "default":
                    continue
                key, _, expected = when.partition("=")
                actual = context.get(key.strip())
                if str(actual) == expected.strip():
                    return str(edge["to_node"])
            default = next((edge for edge in edges if str(edge.get("when") or "default") == "default"), None)
            if default is None:
                raise SopRuntimeError(f"条件节点 {node_id} 没有匹配出口且缺少 default")
            return str(default["to_node"])
        if not edges:
            raise SopRuntimeError(f"节点 {node_id} 没有出口")
        return str(edges[0]["to_node"])

    def _run_agent(self, node: dict[str, Any], run: dict[str, Any]) -> Any:
        """agent 节点：默认由注入的执行器处理（规则节点即 prototype 执行器的一种）。"""
        if self.agent_executor is not None:
            return self.agent_executor(node, run)
        return {"agent": node.get("agent") or node.get("node_id"), "ok": True}

    def _ensure_task(self, run: dict[str, Any], node: dict[str, Any], step: dict[str, Any]) -> str:
        day = str(run["started_at"])[:10]
        task_id = f"{day}-sop-{run['run_id']}-{node['node_id']}"
        if self.task_store is None:
            return task_id
        self.task_store.save_plan({"date": day, "tasks": [{
            "task_id": task_id, "category": "sop",
            "title": f"SOP 人工节点：{node.get('title') or node['node_id']}",
            "assignee": node.get("assignee") or "店长", "priority": "medium",
            "due_at": f"{day}T18:00:00+08:00",
            "target": {"sop_run_id": run["run_id"], "step_run_id": step["step_run_id"],
                       "node_id": node["node_id"]},
            "evidence_required": node.get("evidence_required") or [],
            "reason": f"SOP 运行 {run['run_id']} 的人工节点",
            "requires_approval": False, "rule_key": "", "rule_version": ""}]}, run["store_id"])
        return task_id

    def _ensure_approval(self, run: dict[str, Any], node: dict[str, Any],
                         step: dict[str, Any], *, actor: str = "") -> Optional[str]:
        if self.approval_store is None:
            return None
        record = self.approval_store.request(
            run["store_id"], f"sop:{node.get('node_id')}",
            {"sop_run_id": run["run_id"], "step_run_id": step["step_run_id"], "node_id": node["node_id"]},
            requested_by=actor or "system:sop", task_id=None)
        return record.get("approval_id")

    # ---------------- 步骤与上下文 ----------------

    def _write_step(self, run_id: str, node: dict[str, Any], status: str,
                    *, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if status not in STEP_STATUSES:
            raise SopRuntimeError(f"未知步骤状态：{status}")
        moment = self._now()
        step_run_id = f"sops-{uuid.uuid4().hex[:12]}"
        finished = moment if status in ("completed", "timeout", "failed") else None
        with self._connection() as conn:
            row = conn.execute(
                "select coalesce(max(seq), 0) as s from sop_step_runs where tenant_id=? and run_id=?",
                (self.tenant_id, str(run_id))).fetchone()
            seq = int(row["s"]) + 1
            conn.execute(
                """insert into sop_step_runs (tenant_id,step_run_id,run_id,node_id,node_type,seq,status,
                   payload_json,entered_at,finished_at) values (?,?,?,?,?,?,?,?,?,?)""",
                (self.tenant_id, step_run_id, str(run_id), str(node.get("node_id")),
                 str(node.get("node_type")), seq, status, _dump(dict(payload or {})), moment, finished))
        return self._step(step_run_id)

    def _step(self, step_run_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from sop_step_runs where tenant_id=? and step_run_id=?",
                               (self.tenant_id, str(step_run_id))).fetchone()
        if not row:
            raise SopRuntimeError(f"步骤不存在：{step_run_id}")
        item = dict(row)
        item["payload"] = _loads(item.get("payload_json"), {})
        return item

    def _current_step(self, run_id: str, node_id: Any) -> Optional[dict[str, Any]]:
        if node_id is None:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "select * from sop_step_runs where tenant_id=? and run_id=? and node_id=? "
                "and status='waiting' order by entered_at desc limit 1",
                (self.tenant_id, str(run_id), str(node_id))).fetchone()
        return dict(row) if row else None

    def _finish_step(self, step_run_id: str, status: str, *,
                     payload: Optional[dict[str, Any]] = None) -> None:
        with self._connection() as conn:
            conn.execute(
                "update sop_step_runs set status=?, finished_at=?, payload_json=? "
                "where tenant_id=? and step_run_id=?",
                (status, self._now(), _dump(dict(payload or {})), self.tenant_id, str(step_run_id)))

    def _set_current_node(self, run_id: str, node_id: str, *, task_id: Optional[str] = None,
                          step_id: Optional[str] = None, approval_id: Optional[str] = None) -> None:
        with self._connection() as conn:
            conn.execute("update sop_runs set current_node=?, updated_at=? where tenant_id=? and run_id=?",
                         (str(node_id), self._now(), self.tenant_id, str(run_id)))
            if step_id:
                if task_id:
                    conn.execute("update sop_step_runs set task_id=? where tenant_id=? and step_run_id=?",
                                 (task_id, self.tenant_id, str(step_id)))
                if approval_id:
                    conn.execute("update sop_step_runs set approval_id=? where tenant_id=? and step_run_id=?",
                                 (approval_id, self.tenant_id, str(step_id)))

    def _merge_context(self, run_id: str, extra: dict[str, Any]) -> None:
        run = self.get_run(run_id)
        context = dict(run["context"] or {})
        context.update(extra)
        with self._connection() as conn:
            conn.execute("update sop_runs set context_json=?, updated_at=? where tenant_id=? and run_id=?",
                         (_dump(context), self._now(), self.tenant_id, str(run_id)))