#!/usr/bin/env python3
"""巡店独立流程（第 4 期）：可配置检查表 → 巡店任务 → 缺项复核。

边界（与总方案第 4 期一致）：
1. **只支持固定字段**：检查项字段固定为 item_key/title/evidence_required/required/assignee_role/threshold，
   不做自由字段与拖拽编排（编排能力留给后续期）。
2. 巡店任务仍是普通 `task`（`category="inspection"`），复用 `task_store` 的状态机与指纹去重。
3. **缺项不放过**：必查项缺证据时 `review()` 明确返回 `missing` 与 `requires_review=True`，
   由有 `tasks:verify` 权限的管理者复核，不能由执行人自行标记完成。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from db import Database

TEMPLATE_STATUSES = ("draft", "active", "retired")
ITEM_FIELDS = ("item_key", "title", "evidence_required", "required", "assignee_role", "threshold")
INSPECTION_CATEGORY = "inspection"


class InspectionError(ValueError):
    """巡店模板或巡店任务的参数错误。"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class InspectionStore:
    """`inspection_templates` / `inspection_items` 的读写与巡店任务生成。"""

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
                create table if not exists inspection_templates (
                  tenant_id text not null default '',
                  template_id text not null,
                  store_id text not null,
                  template_key text not null,
                  name text not null,
                  version integer not null,
                  status text not null check (status in ('draft','active','retired')),
                  created_by text not null default '',
                  created_at text not null,
                  primary key (tenant_id, template_id),
                  unique (tenant_id, store_id, template_key, version)
                );
                create index if not exists idx_inspection_templates_scope
                  on inspection_templates(tenant_id, store_id, template_key, status);
                create table if not exists inspection_items (
                  tenant_id text not null default '',
                  template_id text not null,
                  item_key text not null,
                  title text not null,
                  evidence_required text not null default '[]',
                  required integer not null default 1,
                  assignee_role text not null default 'store_manager',
                  threshold_json text not null default '{}',
                  sort_order integer not null default 0,
                  primary key (tenant_id, template_id, item_key)
                );
                """
            )

    def _require_scope(self, store_id: Any) -> str:
        value = str(store_id or "").strip()
        if not value:
            raise InspectionError("store_id 不能为空")
        return value

    # ---------------- 模板 ----------------

    def save_template(self, store_id: str, template_key: str, name: str,
                      items: Iterable[dict[str, Any]], *, created_by: str = "",
                      status: str = "draft") -> dict[str, Any]:
        """新建一个模板版本（版本号 = 同 key 最大值 + 1）。固定字段，未列出的字段一律忽略。"""
        store_id = self._require_scope(store_id)
        template_key = str(template_key or "").strip()
        name = str(name or "").strip()
        if not template_key or not name:
            raise InspectionError("template_key 与 name 不能为空")
        if status not in TEMPLATE_STATUSES:
            raise InspectionError(f"status 必须是 {'、'.join(TEMPLATE_STATUSES)} 之一，收到：{status!r}")
        normalized = self._normalize_items(items)
        moment = datetime.now().isoformat(timespec="seconds")
        with self._connection() as conn:
            row = conn.execute(
                "select coalesce(max(version), 0) as v from inspection_templates "
                "where tenant_id=? and store_id=? and template_key=?",
                (self.tenant_id, store_id, template_key)).fetchone()
            version = int(row["v"]) + 1
            template_id = str(uuid.uuid4())
            conn.execute(
                """insert into inspection_templates
                   (tenant_id,template_id,store_id,template_key,name,version,status,created_by,created_at)
                   values (?,?,?,?,?,?,?,?,?)""",
                (self.tenant_id, template_id, store_id, template_key, name, version,
                 status, str(created_by or ""), moment))
            for order, item in enumerate(normalized):
                conn.execute(
                    """insert into inspection_items
                       (tenant_id,template_id,item_key,title,evidence_required,required,
                        assignee_role,threshold_json,sort_order)
                       values (?,?,?,?,?,?,?,?,?)""",
                    (self.tenant_id, template_id, item["item_key"], item["title"],
                     _json(item["evidence_required"]), 1 if item["required"] else 0,
                     item["assignee_role"], _json(item["threshold"]), order))
        return self.get_template(template_id)

    @staticmethod
    def _normalize_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in items or []:
            item = {key: (raw or {}).get(key) for key in ITEM_FIELDS}
            item_key = str(item["item_key"] or "").strip()
            title = str(item["title"] or "").strip()
            if not item_key or not title:
                raise InspectionError("每个检查项都必须有 item_key 与 title")
            if item_key in seen:
                raise InspectionError(f"检查项 item_key 重复：{item_key}")
            seen.add(item_key)
            evidence = item["evidence_required"] or []
            if isinstance(evidence, str):
                raise InspectionError("evidence_required 必须是数组")
            normalized.append({
                "item_key": item_key,
                "title": title,
                "evidence_required": [str(entry) for entry in evidence],
                "required": bool(item["required"]) if item["required"] is not None else True,
                "assignee_role": str(item["assignee_role"] or "store_manager"),
                "threshold": dict(item["threshold"] or {}),
            })
        if not normalized:
            raise InspectionError("模板至少要有一个检查项")
        return normalized

    def list_templates(self, store_id: str, template_key: Optional[str] = None,
                       limit: int = 100) -> list[dict[str, Any]]:
        store_id = self._require_scope(store_id)
        sql = "select * from inspection_templates where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, store_id]
        if template_key:
            sql += " and template_key=?"
            params.append(str(template_key))
        sql += " order by template_key, version desc limit ?"
        params.append(int(limit))
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._template(conn=None, row=row) for row in rows]

    def get_template(self, template_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "select * from inspection_templates where tenant_id=? and template_id=?",
                (self.tenant_id, str(template_id))).fetchone()
            if not row:
                raise InspectionError(f"巡店模板不存在：{template_id}")
            return self._template(conn, row)

    def active_template(self, store_id: str, template_key: str) -> dict[str, Any]:
        store_id = self._require_scope(store_id)
        with self._connection() as conn:
            row = conn.execute(
                "select * from inspection_templates where tenant_id=? and store_id=? "
                "and template_key=? and status='active' order by version desc limit 1",
                (self.tenant_id, store_id, str(template_key))).fetchone()
            if not row:
                raise InspectionError(f"门店 {store_id} 没有生效的巡店模板：{template_key}")
            return self._template(conn, row)

    def activate(self, template_id: str, actor_id: str = "") -> dict[str, Any]:
        """把某个模板版本置为 active，同 key 其它 active 版本自动 retired。"""
        template = self.get_template(template_id)
        with self._connection() as conn:
            conn.execute(
                "update inspection_templates set status='retired' "
                "where tenant_id=? and store_id=? and template_key=? and status='active' "
                "and template_id<>?",
                (self.tenant_id, template["store_id"], template["template_key"], template["template_id"]))
            conn.execute(
                "update inspection_templates set status='active' where tenant_id=? and template_id=?",
                (self.tenant_id, template["template_id"]))
        record = self.get_template(template["template_id"])
        record["activated_by"] = str(actor_id or "")
        return record

    def _template(self, conn: Any, row: Any) -> dict[str, Any]:
        record = dict(row)
        sql = ("select * from inspection_items where tenant_id=? and template_id=? "
               "order by sort_order")
        params = (self.tenant_id, record["template_id"])
        if conn is None:
            with self._connection() as own:
                items = own.execute(sql, params).fetchall()
        else:
            items = conn.execute(sql, params).fetchall()
        record["items"] = [{
            "item_key": item["item_key"],
            "title": item["title"],
            "evidence_required": _loads(item["evidence_required"], []),
            "required": bool(item["required"]),
            "assignee_role": item["assignee_role"],
            "threshold": _loads(item["threshold_json"], {}),
        } for item in items]
        return record

    # ---------------- 巡店任务与缺项复核 ----------------

    def generate_tasks(self, store_id: str, template_id: str, day: str, *,
                       assignee: str = "店长") -> list[dict[str, Any]]:
        """按模板的检查项生成巡店任务（仍是普通 task，category=inspection）。"""
        template = self.get_template(template_id)
        if template["store_id"] != store_id:
            raise InspectionError("模板不属于当前门店")
        tasks: list[dict[str, Any]] = []
        for item in template["items"]:
            tasks.append({
                "task_id": f"{day}-inspection-{template['template_key']}-{item['item_key']}",
                "category": INSPECTION_CATEGORY,
                "title": f"巡店：{item['title']}",
                "assignee": assignee,
                "priority": "medium",
                "due_at": f"{day}T18:00:00+08:00",
                "target": {
                    "template_id": template["template_id"],
                    "template_key": template["template_key"],
                    "template_version": template["version"],
                    "item_key": item["item_key"],
                    "evidence_required": item["evidence_required"],
                    "threshold": item["threshold"],
                },
                "evidence_required": item["evidence_required"],
                "reason": f"巡店模板「{template['name']}」v{template['version']} 的检查项",
                "requires_approval": False,
                "rule_key": "",
                "rule_version": "",
            })
        return tasks

    def missing_items(self, task: dict[str, Any], submitted_types: Iterable[str]) -> list[str]:
        """必查证据项中还没提交的部分（缺项复核的依据）。"""
        required = (task.get("target") or {}).get("evidence_required") or []
        submitted = {str(item) for item in submitted_types}
        return [str(item) for item in required if str(item) not in submitted]

    def review(self, task_store: Any, task_id: str) -> dict[str, Any]:
        """缺项复核：返回缺项清单与是否需要管理者复核（缺项时不允许直接标记完成）。"""
        task = task_store.get_task(task_id)
        evidence = task_store.evidence(task_id)
        missing = self.missing_items(task, [item.get("evidence_type") for item in evidence])
        return {
            "task_id": task_id,
            "category": task.get("category"),
            "status": task.get("status"),
            "submitted": [item.get("evidence_type") for item in evidence],
            "missing": missing,
            "requires_review": bool(missing),
        }