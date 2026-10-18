# -*- coding: utf-8 -*-
"""任务反馈与结果归因：记录店长/负责人的采纳与驳回，并计算 D+7 / D+28 的结果窗口。

范围与边界（本模块只做"记录 + 计算"，不做因果宣称）：

  * `task_feedback`：同一任务同一人**只保留一条最终决策**（主键
    `tenant_id, store_id, task_id, actor_id`）。重复提交完全相同的反馈是幂等的
    （`created=False` 且 `created_at` 不变）；改判会覆盖该条记录（`created=False, updated=True`）；
    驳回/误报必须给出原因，用于统计误报率。
  * `task_outcomes`：按 `(task_id, window_days, metric_key)` 保存一次计算结果。基线窗口是
    `[closed_on - window_days, closed_on - 1]`，当前窗口是 `[closed_on + 1, closed_on + window_days]`，
    两侧等长，比较的是同口径的日均值（单店首版，不做对照与因果判断）。
  * 缺数一律不补零：窗口内任一天拿不到指标即 `status='no_data'`，baseline/current/delta 全为 None；
    窗口尚未走完（`closed_on + window_days > today`）为 `status='pending'`，且不去读指标。
  * 所有查询都强制 `tenant_id + store_id` 作用域；PostgreSQL 侧还会为这两张表开启行级安全。

指标来源通过 `metric_reader` 注入（`metric_reader(store_id, day, metric_key) -> float | None`），
因此本模块不依赖具体的数据接入实现；`closed_on` 由调用方从任务验收事件（task_store）读取。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from db import STORE_TZ, Database

DECISIONS = ("accepted", "rejected", "false_positive")
REASON_REQUIRED = ("rejected", "false_positive")
OUTCOME_STATUSES = ("computed", "no_data", "pending")

MetricReader = Callable[[str, str, str], Optional[float]]

# 与 schema.sql 的其它租户表保持同样的落库形状；PostgreSQL 侧补开关行级安全，
# 因为本批次不允许改 schema.sql（表由本模块自建）。
_TABLES_DDL = """
create table if not exists task_feedback (
  tenant_id text not null default '',
  store_id text not null,
  task_id text not null,
  actor_id text not null,
  decision text not null check (decision in ('accepted','rejected','false_positive')),
  reason text not null default '',
  created_at text not null,
  primary key (tenant_id, store_id, task_id, actor_id)
);
create index if not exists idx_task_feedback_store_decision
  on task_feedback(store_id, decision, created_at);

create table if not exists task_outcomes (
  tenant_id text not null default '',
  store_id text not null,
  task_id text not null,
  window_days integer not null,
  metric_key text not null,
  baseline_value real,
  current_value real,
  delta real,
  guardrail_json text not null default '{}',
  status text not null check (status in ('computed','no_data','pending')),
  computed_at text not null,
  primary key (tenant_id, store_id, task_id, window_days, metric_key)
);
create index if not exists idx_task_outcomes_store_status
  on task_outcomes(store_id, status, window_days);
"""

_PG_RLS_DDL = """
alter table task_feedback enable row level security;
drop policy if exists p_task_feedback_tenant on task_feedback;
create policy p_task_feedback_tenant on task_feedback
  using (tenant_id = current_setting('app.tenant_id', true))
  with check (tenant_id = current_setting('app.tenant_id', true));
alter table task_outcomes enable row level security;
drop policy if exists p_task_outcomes_tenant on task_outcomes;
create policy p_task_outcomes_tenant on task_outcomes
  using (tenant_id = current_setting('app.tenant_id', true))
  with check (tenant_id = current_setting('app.tenant_id', true));
"""

FEEDBACK_COLUMNS = ("tenant_id", "store_id", "task_id", "actor_id", "decision", "reason", "created_at")
OUTCOME_COLUMNS = ("tenant_id", "store_id", "task_id", "window_days", "metric_key", "baseline_value",
                   "current_value", "delta", "guardrail_json", "status", "computed_at")


class OutcomeTrackerError(ValueError):
    """非法决策、参数缺失、指标源缺失等业务错误。"""


def now_iso() -> str:
    """门店本地时间字符串（与 task_store / db.normalize_value 的口径一致）。"""
    return datetime.now(STORE_TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def _parse_day(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise OutcomeTrackerError(f"{field} 必须是 YYYY-MM-DD，收到：{value!r}") from exc


class OutcomeTracker:
    """任务反馈 + 结果归因的存储与计算（SQLite / PostgreSQL 双引擎）。"""

    def __init__(self, db_target: Any, tenant_id: str = "", metric_reader: Optional[MetricReader] = None):
        self.db = Database(db_target, tenant_id=tenant_id)
        self.tenant_id = self.db.tenant_id
        self.metric_reader = metric_reader
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---------------- 基础设施 ----------------

    def _connection(self):
        """统一走 Database：提交/回滚后必定关闭连接（详见 db.py）。"""
        return self.db.connection()

    def _init_schema(self) -> None:
        script = _TABLES_DDL if self.db.dialect == "sqlite" else _TABLES_DDL + _PG_RLS_DDL
        with self._connection() as conn:
            conn.executescript(script)

    @staticmethod
    def _require_scope(store_id: Any) -> str:
        value = str(store_id or "").strip()
        if not value:
            raise OutcomeTrackerError("store_id 不能为空：结果归因必须按门店作用域读写")
        return value

    # ---------------- 店长反馈 ----------------

    def record_feedback(self, store_id: str, task_id: str, actor_id: str, decision: str,
                        reason: str = "") -> dict[str, Any]:
        """记录一条任务反馈（同一任务同一人只保留最终决策，重复提交幂等）。"""
        store_id = self._require_scope(store_id)
        task_id = str(task_id or "").strip()
        actor_id = str(actor_id or "").strip()
        decision = str(decision or "").strip()
        if not task_id:
            raise OutcomeTrackerError("task_id 不能为空")
        if not actor_id:
            raise OutcomeTrackerError("actor_id 不能为空")
        if decision not in DECISIONS:
            raise OutcomeTrackerError(f"decision 必须是 {DECISIONS} 之一，收到：{decision!r}")
        reason = str(reason or "").strip()
        if decision in REASON_REQUIRED and not reason:
            raise OutcomeTrackerError(f"decision={decision} 必须填写原因")

        scope = (self.tenant_id, store_id, task_id, actor_id)
        with self._connection() as conn:
            existing = conn.execute(
                "select * from task_feedback where tenant_id=? and store_id=? and task_id=? and actor_id=?",
                scope,
            ).fetchone()
            if existing and existing["decision"] == decision and existing["reason"] == reason:
                return dict(existing, created=False, updated=False)
            created_at = now_iso()
            if existing:
                conn.execute(
                    """update task_feedback set decision=?, reason=?, created_at=?
                       where tenant_id=? and store_id=? and task_id=? and actor_id=?""",
                    (decision, reason, created_at) + scope,
                )
                created, updated = False, True
            else:
                conn.execute(
                    """insert into task_feedback
                       (tenant_id,store_id,task_id,actor_id,decision,reason,created_at)
                       values (?,?,?,?,?,?,?)""",
                    scope + (decision, reason, created_at),
                )
                created, updated = True, False
            row = conn.execute(
                "select * from task_feedback where tenant_id=? and store_id=? and task_id=? and actor_id=?",
                scope,
            ).fetchone()
        return dict(row, created=created, updated=updated)

    def list_feedback(self, store_id: str, task_id: Optional[str] = None) -> list[dict[str, Any]]:
        """按门店（可选再按任务）列出反馈，时间与任务号排序稳定。"""
        store_id = self._require_scope(store_id)
        sql = "select * from task_feedback where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, store_id]
        if task_id is not None:
            sql += " and task_id=?"
            params.append(str(task_id).strip())
        sql += " order by created_at, task_id, actor_id"
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    # ---------------- 结果归因 ----------------

    def compute_outcome(self, store_id: str, task_id: str, *, metric_key: str, window_days: int,
                        closed_on: Any, today: Any = None, guardrail: Optional[dict] = None,
                        metric_reader: Optional[MetricReader] = None) -> dict[str, Any]:
        """计算并保存某个任务在 D+window_days 的结果窗口（缺数不补零，可重复执行）。"""
        store_id = self._require_scope(store_id)
        task_id = str(task_id or "").strip()
        metric_key = str(metric_key or "").strip()
        if not task_id:
            raise OutcomeTrackerError("task_id 不能为空")
        if not metric_key:
            raise OutcomeTrackerError("metric_key 不能为空")
        if not isinstance(window_days, int) or isinstance(window_days, bool) or window_days <= 0:
            raise OutcomeTrackerError(f"window_days 必须是正整数，收到：{window_days!r}")
        closed = _parse_day(closed_on, "closed_on")
        current_day = _parse_day(today, "today") if today is not None else datetime.now(STORE_TZ).date()
        reader = metric_reader or self.metric_reader
        if reader is None:
            raise OutcomeTrackerError("缺少 metric_reader：无法读取指标，请注入指标源")

        baseline_days = [closed - timedelta(days=offset) for offset in range(window_days, 0, -1)]
        current_days = [closed + timedelta(days=offset) for offset in range(1, window_days + 1)]
        guardrail_json = json.dumps(guardrail, ensure_ascii=False) if guardrail else "{}"
        status, baseline_value, current_value, delta = "pending", None, None, None
        if closed + timedelta(days=window_days) <= current_day:
            baseline_series = self._read_series(reader, store_id, baseline_days, metric_key)
            current_series = self._read_series(reader, store_id, current_days, metric_key)
            if baseline_series is None or current_series is None:
                status = "no_data"
            else:
                baseline_value = round(sum(baseline_series) / window_days, 6)
                current_value = round(sum(current_series) / window_days, 6)
                delta = round(current_value - baseline_value, 6)
                status = "computed"

        values = (self.tenant_id, store_id, task_id, window_days, metric_key, baseline_value,
                  current_value, delta, guardrail_json, status, now_iso())
        with self._connection() as conn:
            existing = conn.execute(
                """select * from task_outcomes
                   where tenant_id=? and store_id=? and task_id=? and window_days=? and metric_key=?""",
                (self.tenant_id, store_id, task_id, window_days, metric_key),
            ).fetchone()
            if existing and self._unchanged(existing, values):
                return dict(existing)                        # 值没变就不刷新 computed_at
            if existing:
                conn.execute(
                    """update task_outcomes set baseline_value=?, current_value=?, delta=?, guardrail_json=?,
                       status=?, computed_at=?
                       where tenant_id=? and store_id=? and task_id=? and window_days=? and metric_key=?""",
                    values[5:] + (self.tenant_id, store_id, task_id, window_days, metric_key),
                )
            else:
                conn.execute(
                    """insert into task_outcomes
                       (tenant_id,store_id,task_id,window_days,metric_key,baseline_value,current_value,
                        delta,guardrail_json,status,computed_at)
                       values (?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
            row = conn.execute(
                """select * from task_outcomes
                   where tenant_id=? and store_id=? and task_id=? and window_days=? and metric_key=?""",
                (self.tenant_id, store_id, task_id, window_days, metric_key),
            ).fetchone()
        return dict(row)

    @staticmethod
    def _unchanged(existing: dict[str, Any], values: tuple) -> bool:
        def same(left: Any, right: Any) -> bool:
            if left is None or right is None:
                return left is None and right is None
            return float(left) == float(right)

        return (same(existing["baseline_value"], values[5]) and same(existing["current_value"], values[6])
                and same(existing["delta"], values[7]) and existing["guardrail_json"] == values[8]
                and existing["status"] == values[9])

    def _read_series(self, reader: MetricReader, store_id: str, window: list[date],
                     metric_key: str) -> Optional[list[float]]:
        """读取窗口内每天的指标；任一天缺数（None）即返回 None，绝不补零。"""
        series: list[float] = []
        for day in window:
            value = reader(store_id, day.isoformat(), metric_key)
            if value is None:
                return None
            try:
                series.append(float(value))
            except (TypeError, ValueError) as exc:
                raise OutcomeTrackerError(f"{day.isoformat()} 的 {metric_key} 不是数字：{value!r}") from exc
        return series

    def list_outcomes(self, store_id: str, task_id: Optional[str] = None,
                      window_days: Optional[int] = None, metric_key: Optional[str] = None,
                      status: Optional[str] = None) -> list[dict[str, Any]]:
        """按门店（可选再按任务/窗口/指标/状态）列出归因结果。"""
        store_id = self._require_scope(store_id)
        sql = "select * from task_outcomes where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, store_id]
        if task_id is not None:
            sql += " and task_id=?"
            params.append(str(task_id).strip())
        if window_days is not None:
            if not isinstance(window_days, int) or isinstance(window_days, bool) or window_days <= 0:
                raise OutcomeTrackerError(f"window_days 必须是正整数，收到：{window_days!r}")
            sql += " and window_days=?"
            params.append(window_days)
        if metric_key is not None:
            sql += " and metric_key=?"
            params.append(str(metric_key).strip())
        if status is not None:
            if status not in OUTCOME_STATUSES:
                raise OutcomeTrackerError(f"status 必须是 {OUTCOME_STATUSES} 之一，收到：{status!r}")
            sql += " and status=?"
            params.append(status)
        sql += " order by task_id, window_days, metric_key"
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def outcome_summary(self, store_id: str) -> dict[str, Any]:
        """门店维度的试点度量：采纳率、误报率、结果缺失情况（无分母时返回 None 而不是 0）。"""
        store_id = self._require_scope(store_id)
        scope = (self.tenant_id, store_id)
        with self._connection() as conn:
            decisions = {row["decision"]: int(row["n"]) for row in conn.execute(
                "select decision, count(*) as n from task_feedback where tenant_id=? and store_id=? "
                "group by decision", scope).fetchall()}
            tasks = int(conn.execute(
                "select count(distinct task_id) as n from task_feedback where tenant_id=? and store_id=?",
                scope).fetchone()["n"])
            grouped = conn.execute(
                "select window_days, status, count(*) as n from task_outcomes "
                "where tenant_id=? and store_id=? group by window_days, status", scope).fetchall()

        total = sum(decisions.values())
        windows: dict[int, dict[str, int]] = {}
        for row in grouped:
            window = windows.setdefault(int(row["window_days"]),
                                        {"window_days": int(row["window_days"]), "total": 0,
                                         "computed": 0, "no_data": 0, "pending": 0})
            window[row["status"]] = int(row["n"])
            window["total"] += int(row["n"])
        outcome_total = sum(window["total"] for window in windows.values())
        return {
            "tenant_id": self.tenant_id,
            "store_id": store_id,
            "feedback": {
                "total": total,
                "tasks": tasks,
                "accepted": decisions.get("accepted", 0),
                "rejected": decisions.get("rejected", 0),
                "false_positive": decisions.get("false_positive", 0),
                "acceptance_rate": round(decisions.get("accepted", 0) / total, 4) if total else None,
                "false_positive_rate": round(decisions.get("false_positive", 0) / total, 4) if total else None,
            },
            "outcomes": {
                "total": outcome_total,
                "computed": sum(window["computed"] for window in windows.values()),
                "no_data": sum(window["no_data"] for window in windows.values()),
                "pending": sum(window["pending"] for window in windows.values()),
                "windows": [windows[key] for key in sorted(windows)],
            },
        }
