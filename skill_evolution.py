#!/usr/bin/env python3
"""技能蒸馏与反思提案（第 9 期）。

两条硬约定：
1. **只出提案，不改行为**：提案写进 `evolution_proposals`，`decide()` 只改提案状态；
   未人工确认时系统行为完全不变（与第 3 期、第 6 期同一哲学）。
2. **不做规则侧改动**：规则调参已在第 3 期完成；如提案涉及规则，只记录"建议复用第 3 期的规则版本机制"，
   本模块不写 `action_rules`。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Database

PROPOSAL_STATUSES = ("pending", "approved", "rejected")
PROPOSAL_TYPES = ("sop_step", "memory_consolidation", "reflection")
DECISIONS = ("approved", "rejected")
MIN_EVIDENCE = 2


class EvolutionError(ValueError):
    """提案的参数或状态错误。"""


class SkillEvolution:
    """`evolution_proposals` 的读写与人工确认。"""

    def __init__(self, db_target: Any, tenant_id: str = "", *, memory_store: Any = None):
        self.db = Database(db_target, tenant_id=tenant_id)
        self.tenant_id = tenant_id
        self.memory_store = memory_store
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
                create table if not exists evolution_proposals (
                  tenant_id text not null default '',
                  proposal_id text not null,
                  store_id text not null,
                  proposal_type text not null check (proposal_type in
                    ('sop_step','memory_consolidation','reflection')),
                  title text not null,
                  payload_json text not null default '{}',
                  evidence_json text not null default '[]',
                  status text not null default 'pending' check (status in ('pending','approved','rejected')),
                  decided_by text,
                  decision_note text not null default '',
                  created_at text not null,
                  decided_at text,
                  primary key (tenant_id, proposal_id)
                );
                create index if not exists idx_evolution_scope
                  on evolution_proposals(tenant_id, store_id, status);
                """)

    def _require_text(self, value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise EvolutionError(f"{field} 不能为空")
        return text

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def propose(self, store_id: str, proposal_type: str, title: str, *,
                payload: Optional[dict[str, Any]] = None, evidence: Optional[list[Any]] = None) -> dict[str, Any]:
        store_id = self._require_text(store_id, "store_id")
        if proposal_type not in PROPOSAL_TYPES:
            raise EvolutionError(f"proposal_type 必须是 {'、'.join(PROPOSAL_TYPES)} 之一，收到：{proposal_type!r}")
        title = self._require_text(title, "title")
        moment = self._now()
        proposal_id = f"evo-{uuid.uuid4().hex[:12]}"
        with self._connection() as conn:
            conn.execute(
                """insert into evolution_proposals (tenant_id,proposal_id,store_id,proposal_type,title,
                   payload_json,evidence_json,status,created_at)
                   values (?,?,?,?,?,?,?,'pending',?)""",
                (self.tenant_id, proposal_id, store_id, proposal_type, title,
                 json.dumps(dict(payload or {}), ensure_ascii=False),
                 json.dumps(list(evidence or []), ensure_ascii=False), moment))
        return self.get(proposal_id)

    def distill_from_memories(self, store_id: str, user_id: Optional[str] = None, *,
                              min_evidence: int = MIN_EVIDENCE) -> list[dict[str, Any]]:
        """把重复出现的偏好蒸馏成 SOP 步骤提案（证据不足时不出提案）。"""
        if self.memory_store is None:
            raise EvolutionError("未接入记忆库")
        records = self.memory_store.recall(store_id, user_id, kind="preference", limit=50)
        proposals = []
        for record in records:
            if int(record.get("hits") or 0) < int(min_evidence):
                continue
            proposals.append(self.propose(
                store_id, "sop_step",
                f"把偏好「{record['value']}」固化为 SOP 步骤",
                payload={"memory_id": record["memory_id"], "kind": record["kind"],
                         "source": "memory_consolidation",
                         "note": "如涉及规则调整，复用第 3 期的规则版本机制"},
                evidence=[record["memory_id"]]))
        return proposals

    def reflect(self, store_id: str, *, note: str, evidence: Optional[list[Any]] = None) -> dict[str, Any]:
        """反思提案：由人（或定时任务）把一条观察写成待确认的改进提案。"""
        return self.propose(store_id, "reflection", note, evidence=evidence)

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from evolution_proposals where tenant_id=? and proposal_id=?",
                               (self.tenant_id, str(proposal_id))).fetchone()
        if not row:
            raise EvolutionError(f"提案不存在：{proposal_id}")
        return self._decode(dict(row))

    @staticmethod
    def _decode(record: dict[str, Any]) -> dict[str, Any]:
        for field, target in (("payload_json", "payload"), ("evidence_json", "evidence")):
            value = record.get(field)
            if isinstance(value, str):
                try:
                    record[target] = json.loads(value)
                except json.JSONDecodeError:
                    record[target] = {} if target == "payload" else []
        return record

    def list(self, store_id: str, status: Optional[str] = None) -> list[dict[str, Any]]:
        store_id = self._require_text(store_id, "store_id")
        if status is not None and status not in PROPOSAL_STATUSES:
            raise EvolutionError(f"status 必须是 {'、'.join(PROPOSAL_STATUSES)} 之一，收到：{status!r}")
        sql = "select * from evolution_proposals where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, store_id]
        if status:
            sql += " and status=?"
            params.append(status)
        sql += " order by created_at desc, proposal_id"
        with self._connection() as conn:
            return [self._decode(dict(row)) for row in conn.execute(sql, tuple(params)).fetchall()]

    def decide(self, proposal_id: str, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        """人工确认：只改提案状态，**不改任何运行行为**。"""
        proposal = self.get(proposal_id)
        if proposal["status"] != "pending":
            raise EvolutionError(f"提案 {proposal_id} 当前状态为 {proposal['status']}，不能重复确认")
        if decision not in DECISIONS:
            raise EvolutionError(f"decision 必须是 {'、'.join(DECISIONS)} 之一，收到：{decision!r}")
        actor = self._require_text(actor, "actor")
        moment = self._now()
        with self._connection() as conn:
            conn.execute(
                "update evolution_proposals set status=?, decided_by=?, decision_note=?, decided_at=? "
                "where tenant_id=? and proposal_id=?",
                (decision, actor, str(note or ""), moment, self.tenant_id, str(proposal_id)))
        return self.get(proposal_id)