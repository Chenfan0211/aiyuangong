#!/usr/bin/env python3
"""SOP 图定义与版本（第 7 期）。

边界（与总方案第 7 期一致）：
1. **4 类节点**：`agent`（数字员工/规则执行器）、`human`（人工提交）、`approval`（人工审批）、
   `condition`（条件分支）。**固定字段，不做拖拽画布**（只提供最小可用的图校验与预览）。
2. 版本化：同一 `sop_key` 每次保存递增版本；`activate()` 保证同一 key 只有一个生效版本。
3. 边界约定：`task` 是"派发给人的工作项"，`sop_run` 是"跨角色流程实例"，任务挂在 `sop_step_run` 上。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from db import Database

NODE_TYPES = ("agent", "human", "approval", "condition")
SOP_STATUSES = ("draft", "active", "retired")
TERMINAL_NODE_TYPES = ("end",)
DEFAULT_MAX_HOURS = 24.0


class SopError(ValueError):
    """SOP 定义或状态错误。"""


def validate_graph(nodes: Iterable[dict[str, Any]], edges: Iterable[dict[str, Any]],
                   start_node: str) -> None:
    """图校验：节点类型/唯一 id/边引用/入口/分支出口。校验不过一律拒绝保存。"""
    node_list = list(nodes or [])
    edge_list = list(edges or [])
    if not node_list:
        raise SopError("SOP 至少要有一个节点")
    ids: list[str] = []
    for node in node_list:
        node_id = str((node or {}).get("node_id") or "").strip()
        node_type = str((node or {}).get("node_type") or "").strip()
        if not node_id:
            raise SopError("每个节点都必须有 node_id")
        if node_id in ids:
            raise SopError(f"节点 id 重复：{node_id}")
        if node_type not in NODE_TYPES and node_type not in TERMINAL_NODE_TYPES:
            raise SopError(f"节点 {node_id} 的类型非法：{node_type!r}（可用：{'、'.join(NODE_TYPES)}、end）")
        ids.append(node_id)
    start = str(start_node or "").strip()
    if start not in ids:
        raise SopError(f"入口节点不存在：{start_node}")
    known = set(ids)
    for edge in edge_list:
        source = str((edge or {}).get("from_node") or "").strip()
        target = str((edge or {}).get("to_node") or "").strip()
        if source not in known or target not in known:
            raise SopError(f"边引用了不存在的节点：{source} → {target}")
    by_id = {str(node["node_id"]): node for node in node_list}
    outgoing: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in ids}
    for edge in edge_list:
        outgoing[str(edge["from_node"])].append(edge)
    for node_id, node in by_id.items():
        node_type = str(node.get("node_type") or "")
        exits = outgoing[node_id]
        if node_type == "condition":
            if len(exits) < 2:
                raise SopError(f"条件分支节点 {node_id} 至少要有两个出口")
            if not any(str(edge.get("when") or "default") == "default" for edge in exits):
                raise SopError(f"条件分支节点 {node_id} 需要一个 default 出口")
        elif node_type not in TERMINAL_NODE_TYPES and not exits:
            raise SopError(f"节点 {node_id} 没有出口（终止节点请用 end 类型）")


class SopStore:
    """`sops` / `sop_versions` 的读写与版本管理。"""

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
                create table if not exists sops (
                  tenant_id text not null default '',
                  sop_id text not null,
                  store_id text not null,
                  sop_key text not null,
                  name text not null,
                  version integer not null,
                  status text not null check (status in ('draft','active','retired')),
                  max_hours real not null default 24,
                  created_by text not null default '',
                  created_at text not null,
                  primary key (tenant_id, sop_id),
                  unique (tenant_id, store_id, sop_key, version)
                );
                create table if not exists sop_versions (
                  tenant_id text not null default '',
                  version_id text not null,
                  sop_id text not null,
                  version integer not null,
                  nodes_json text not null,
                  edges_json text not null,
                  start_node text not null,
                  created_at text not null,
                  primary key (tenant_id, version_id),
                  unique (tenant_id, sop_id, version)
                );
                create index if not exists idx_sop_versions_sop on sop_versions(tenant_id, sop_id, version);
                """)

    def _require_text(self, value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise SopError(f"{field} 不能为空")
        return text

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def save_sop(self, store_id: str, sop_key: str, name: str, nodes: Iterable[dict[str, Any]],
                 edges: Iterable[dict[str, Any]], *, start_node: str, created_by: str = "",
                 max_hours: float = DEFAULT_MAX_HOURS, status: str = "draft") -> dict[str, Any]:
        """保存一个新版本（版本号 = 同 key 最大值 + 1）。"""
        store_id = self._require_text(store_id, "store_id")
        sop_key = self._require_text(sop_key, "sop_key")
        name = self._require_text(name, "name")
        if status not in SOP_STATUSES:
            raise SopError(f"status 必须是 {'、'.join(SOP_STATUSES)} 之一，收到：{status!r}")
        max_hours = float(max_hours)
        if max_hours <= 0:
            raise SopError("max_hours 必须大于 0")
        node_list = [dict(node) for node in (nodes or [])]
        edge_list = [dict(edge) for edge in (edges or [])]
        validate_graph(node_list, edge_list, start_node)
        moment = self._now()
        sop_id = f"sop-{uuid.uuid4().hex[:12]}"
        with self._connection() as conn:
            row = conn.execute(
                "select coalesce(max(version), 0) as v from sops where tenant_id=? and store_id=? and sop_key=?",
                (self.tenant_id, store_id, sop_key)).fetchone()
            version = int(row["v"]) + 1
            conn.execute(
                """insert into sops (tenant_id,sop_id,store_id,sop_key,name,version,status,max_hours,
                   created_by,created_at) values (?,?,?,?,?,?,?,?,?,?)""",
                (self.tenant_id, sop_id, store_id, sop_key, name, version, status, max_hours,
                 str(created_by or ""), moment))
            conn.execute(
                """insert into sop_versions (tenant_id,version_id,sop_id,version,nodes_json,edges_json,
                   start_node,created_at) values (?,?,?,?,?,?,?,?)""",
                (self.tenant_id, f"sopv-{uuid.uuid4().hex[:12]}", sop_id, version,
                 _dump(node_list), _dump(edge_list), str(start_node), moment))
        return self.get_sop(sop_id)

    def get_sop(self, sop_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from sops where tenant_id=? and sop_id=?",
                               (self.tenant_id, str(sop_id))).fetchone()
            if not row:
                raise SopError(f"SOP 不存在：{sop_id}")
            record = dict(row)
            version = conn.execute(
                "select * from sop_versions where tenant_id=? and sop_id=? and version=?",
                (self.tenant_id, record["sop_id"], record["version"])).fetchone()
        record["graph"] = _graph_of(version)
        return record

    def version_graph(self, sop_id: str, version: int) -> dict[str, Any]:
        """按版本取图（旧运行仍能解释自己所用的版本）。"""
        with self._connection() as conn:
            row = conn.execute(
                "select * from sop_versions where tenant_id=? and sop_id=? and version=?",
                (self.tenant_id, str(sop_id), int(version))).fetchone()
            if not row:
                raise SopError(f"SOP 版本不存在：{sop_id} v{version}")
            record = dict(row)
            sop = conn.execute("select * from sops where tenant_id=? and sop_id=?",
                               (self.tenant_id, str(sop_id))).fetchone()
        return {"sop_id": str(sop_id), "version": int(version), "sop_key": sop["sop_key"] if sop else "",
                "name": sop["name"] if sop else "", "graph": _graph_of(record)}

    def list_sops(self, store_id: str, sop_key: Optional[str] = None) -> list[dict[str, Any]]:
        store_id = self._require_text(store_id, "store_id")
        sql = "select * from sops where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, store_id]
        if sop_key:
            sql += " and sop_key=?"
            params.append(str(sop_key))
        sql += " order by sop_key, version desc"
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def active(self, store_id: str, sop_key: str) -> dict[str, Any]:
        store_id = self._require_text(store_id, "store_id")
        with self._connection() as conn:
            row = conn.execute(
                "select * from sops where tenant_id=? and store_id=? and sop_key=? and status='active' "
                "order by version desc limit 1",
                (self.tenant_id, store_id, str(sop_key))).fetchone()
        if not row:
            raise SopError(f"门店 {store_id} 没有生效的 SOP：{sop_key}")
        return self.get_sop(row["sop_id"])

    def activate(self, sop_id: str, actor_id: str = "") -> dict[str, Any]:
        sop = self.get_sop(sop_id)
        with self._connection() as conn:
            conn.execute(
                "update sops set status='retired' where tenant_id=? and store_id=? and sop_key=? "
                "and status='active' and sop_id<>?",
                (self.tenant_id, sop["store_id"], sop["sop_key"], sop["sop_id"]))
            conn.execute("update sops set status='active' where tenant_id=? and sop_id=?",
                         (self.tenant_id, sop["sop_id"]))
        record = self.get_sop(sop["sop_id"])
        record["activated_by"] = str(actor_id or "")
        return record


def _dump(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def _graph_of(row: Any) -> dict[str, Any]:
    import json
    nodes = json.loads(row["nodes_json"]) if row else []
    edges = json.loads(row["edges_json"]) if row else []
    return {"nodes": nodes, "edges": edges, "start_node": row["start_node"] if row else ""}