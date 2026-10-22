#!/usr/bin/env python3
"""规则效果归因（第 3 期第 6 步）：把任务结果按规则版本聚合成"调参提案"。

边界（与总方案第 3 期一致）：
1. **只出提案，不改规则**：本模块从不写 `action_rules`；提案落在 `rule_outcomes.suggestion_json`，
   必须人工确认（`/api/rules/{rule_key}/decide`）后才可能生效。未确认时系统行为完全不变。
2. **样本不足不下结论**：样本数低于 `MIN_SAMPLES` 时 verdict 固定为 `insufficient_data`，不产出数值建议。
3. **缺数不编造**：没有结果窗口数据时 `avg_delta` 为 None，不会因为缺数而升级成"改阈值"。

数据来源：`action_tasks.rule_key/rule_version`（任务追溯）+ `task_feedback`（人工反馈）
+ `task_outcomes`（D+7/D+28 结果窗口）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from db import Database

MIN_SAMPLES = 3
REJECT_RATIO_TO_RAISE = 0.5
RAISE_FACTOR = 1.2
VERDICTS = ("keep", "raise_threshold", "lower_threshold", "insufficient_data")

# 只有这些规则的 condition 里有可调阈值；其余规则只给 verdict，不编数值建议
THRESHOLD_FIELDS: dict[str, str] = {
    "package": "conversion_floor",
    "low_slot": "share_floor",
    "competitor": "area_shift_percent",
    "negative_review": "sla_hours",
}


class RuleOutcomeError(ValueError):
    """规则归因的参数或状态错误。"""


def _condition_of(record: Mapping[str, Any]) -> dict[str, Any]:
    return dict(record.get("condition") or record.get("condition_json") or {})


def _attach_threshold(suggestion: dict[str, Any], rule_key: str,
                      rules: Optional[Mapping[str, Any]]) -> None:
    """把"建议由 X 调整为 Y"补进提案；缺少阈值来源时不编数字。"""
    field = THRESHOLD_FIELDS.get(rule_key)
    if not field or not rules:
        return
    record = rules.get(rule_key) or {}
    current = _condition_of(record).get(field)
    if current is None or isinstance(current, bool):
        return
    try:
        number = float(current)
    except (TypeError, ValueError):
        return
    suggested = max(1, int(round(number * RAISE_FACTOR))) if isinstance(current, int) else round(number * RAISE_FACTOR, 4)
    suggestion.update({"field": field, "current": current, "suggested": suggested})


class RuleOutcomeStore:
    """`rule_outcomes` 的读写入口（SQLite / PostgreSQL 双引擎）。"""

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
                create table if not exists rule_outcomes (
                  tenant_id text not null default '',
                  store_id text not null,
                  rule_key text not null,
                  rule_version text not null,
                  window_days integer not null,
                  sample_size integer not null,
                  accepted_count integer not null,
                  rejected_count integer not null,
                  avg_delta real,
                  verdict text not null check (verdict in ('keep','raise_threshold','lower_threshold','insufficient_data')),
                  suggestion_json text not null default '{}',
                  computed_at text not null,
                  primary key (tenant_id, store_id, rule_key, rule_version, window_days)
                );
                create index if not exists idx_rule_outcomes_store_rule
                  on rule_outcomes(store_id, rule_key, window_days);
                """
            )

    def _require_scope(self, store_id: Any) -> str:
        value = str(store_id or "").strip()
        if not value:
            raise RuleOutcomeError("store_id 不能为空")
        return value

    # ---------------- 计算 ----------------

    def compute(self, store_id: str, *, task_store: Any, outcome_tracker: Any,
                rules: Optional[Mapping[str, Any]] = None, window_days: int = 7) -> dict[str, Any]:
        """按规则版本聚合"任务 → 反馈 → 结果窗口"，写入提案（幂等，可重复执行）。"""
        store_id = self._require_scope(store_id)
        window_days = int(window_days)
        if window_days <= 0:
            raise RuleOutcomeError(f"window_days 必须是正整数，收到：{window_days!r}")

        tasks = task_store.list_tasks(store_id, limit=1000)
        feedback = outcome_tracker.list_feedback(store_id)
        outcomes = outcome_tracker.list_outcomes(store_id, window_days=window_days)

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for task in tasks:
            rule_key = str(task.get("rule_key") or "")
            if not rule_key:
                continue          # 兜底任务（如 data-repair）不属于业务规则，不参与归因
            grouped.setdefault((rule_key, str(task.get("rule_version") or "")), []).append(task)

        now = datetime.now().isoformat(timespec="seconds")
        computed: list[dict[str, Any]] = []
        for (rule_key, rule_version), group in sorted(grouped.items()):
            task_ids = {str(task["task_id"]) for task in group}
            decisions = [str(item.get("decision") or "") for item in feedback
                         if str(item.get("task_id")) in task_ids]
            accepted = sum(1 for decision in decisions if decision == "accepted")
            rejected = sum(1 for decision in decisions if decision in ("rejected", "false_positive"))
            deltas = [float(item["delta"]) for item in outcomes
                      if str(item.get("task_id")) in task_ids and item.get("delta") is not None]
            avg_delta = round(sum(deltas) / len(deltas), 4) if deltas else None

            verdict, suggestion = self._verdict(rule_key, len(group), accepted, rejected, avg_delta, rules)
            record = {
                "store_id": store_id, "rule_key": rule_key, "rule_version": rule_version,
                "window_days": window_days, "sample_size": len(group),
                "accepted_count": accepted, "rejected_count": rejected, "avg_delta": avg_delta,
                "verdict": verdict, "suggestion": suggestion, "computed_at": now,
            }
            self._upsert(record)
            computed.append(record)
        return {"store_id": store_id, "window_days": window_days,
                "rule_count": len(computed), "computed": computed}

    @staticmethod
    def _verdict(rule_key: str, sample_size: int, accepted: int, rejected: int,
                 avg_delta: Optional[float], rules: Optional[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
        if sample_size < MIN_SAMPLES:
            return "insufficient_data", {
                "action": "insufficient_data",
                "reason": f"样本仅 {sample_size} 条，低于 {MIN_SAMPLES} 条，不建议调参",
                "requires_approval": True,
            }
        decided = accepted + rejected
        reject_ratio = (rejected / decided) if decided else 0.0
        if reject_ratio >= REJECT_RATIO_TO_RAISE:
            suggestion: dict[str, Any] = {
                "action": "raise_threshold",
                "reason": (f"{decided} 条人工反馈中 {rejected} 条为驳回或误报"
                           f"（{reject_ratio:.0%}），规则可能过松"),
                "requires_approval": True,
            }
            _attach_threshold(suggestion, rule_key, rules)
            return "raise_threshold", suggestion
        note = "结果窗口未见恶化" if avg_delta is None or avg_delta >= 0 else f"结果窗口均值 {avg_delta}，需继续观察"
        return "keep", {
            "action": "keep",
            "reason": f"样本 {sample_size} 条、驳回/误报率 {reject_ratio:.0%}，{note}",
            "requires_approval": False,
        }

    def _upsert(self, record: Mapping[str, Any]) -> None:
        scope = (self.tenant_id, record["store_id"], record["rule_key"],
                 record["rule_version"], int(record["window_days"]))
        with self._connection() as conn:
            conn.execute(
                "delete from rule_outcomes where tenant_id=? and store_id=? and rule_key=? "
                "and rule_version=? and window_days=?", scope)
            conn.execute(
                """insert into rule_outcomes
                   (tenant_id,store_id,rule_key,rule_version,window_days,sample_size,accepted_count,
                    rejected_count,avg_delta,verdict,suggestion_json,computed_at)
                   values (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.tenant_id, record["store_id"], record["rule_key"], record["rule_version"],
                 int(record["window_days"]), int(record["sample_size"]), int(record["accepted_count"]),
                 int(record["rejected_count"]), record["avg_delta"], record["verdict"],
                 json.dumps(record["suggestion"], ensure_ascii=False), record["computed_at"]),
            )

    # ---------------- 读 ----------------

    def list(self, store_id: str, rule_key: Optional[str] = None,
             window_days: Optional[int] = None) -> list[dict[str, Any]]:
        store_id = self._require_scope(store_id)
        sql = "select * from rule_outcomes where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, store_id]
        if rule_key:
            sql += " and rule_key=?"
            params.append(str(rule_key))
        if window_days is not None:
            sql += " and window_days=?"
            params.append(int(window_days))
        sql += " order by rule_key, window_days, rule_version"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("suggestion_json"), str):
                try:
                    item["suggestion"] = json.loads(item["suggestion_json"])
                except json.JSONDecodeError:
                    item["suggestion"] = {}
            items.append(item)
        return items