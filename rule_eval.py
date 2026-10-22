#!/usr/bin/env python3
"""表驱动规则执行器（第 3 期第 3 步）。

把 `action_rules` 里的规则定义（条件 + 动作模板）执行成任务清单。本模块**不读库、不写库、
不产生副作用**：规则由调用方（`RuleStore`）传入，因此可以先做"表驱动 vs 内置"的逐字段对拍，
再决定第 4 步是否接线。

三条设计边界：
1. **阈值来自规则定义**（`condition`），动作字段来自 `action_template`；判定逻辑与
   `prototype.generate_daily_plan()` 保持等价，由 `test_rule_eval.py` 逐字段锁定。
2. 快照里已有的同源阈值（`ops.thresholds` / `competitor.thresholds`）优先，与内置引擎一致；
   快照缺省时才回落到规则定义里的阈值。
3. 规则缺失时抛 `RuleEvalError`，**不静默降级为"无规则"**；是否回退内置默认规则由调用方决定。

复用 `prototype` 的 `_task` / `_slug` / `_pct` 是为了保证产出结构（含文案格式化）与内置引擎
逐字段一致——这是本步的验收锚点。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from prototype import (
    MAX_INVENTORY_ITEMS_IN_TASK,
    OPS_CATEGORY,
    OPS_COMPETITOR_CATEGORY,
    _pct,
    _slug,
    _task,
)

RULE_ORDER: tuple[str, ...] = (
    "review_invite", "negative_review", "package", "escalation",
    "low_slot", "inventory", "competitor",
)
PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
DEFAULT_AREA_MANAGER = "区域负责人"


class RuleEvalError(ValueError):
    """规则定义缺失或不可用。"""


def _parts(rule: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """兼容两种规则表达：种子定义（condition）与 RuleStore 记录（condition_json）。"""
    condition = rule.get("condition") or rule.get("condition_json") or {}
    template = rule.get("action_template") or rule.get("action_template_json") or {}
    return dict(condition), dict(template)


class _Context:
    """把快照、规则定义与降级开关收敛成执行上下文。"""

    def __init__(self, snapshot: Mapping[str, Any], rules: Mapping[str, Any],
                 allow_degraded: bool) -> None:
        self.snapshot = snapshot
        self.rules = rules
        self.allow_degraded = allow_degraded
        self.date = snapshot["date"]
        self.owner = snapshot.get("store_manager", "店长")
        readiness = (snapshot.get("_meta") or {}).get("rule_readiness") or {}
        # 保持配置里的插入顺序：data-repair 的 reason 文案依赖这个顺序
        self.blocked = {key: value for key, value in readiness.items()
                        if value.get("status") == "blocked"}

    def enabled(self, rule_key: str) -> bool:
        return not self.blocked.get(rule_key) or self.allow_degraded

    def condition(self, rule_key: str) -> dict[str, Any]:
        return _parts(self.rules[rule_key])[0]

    def template(self, rule_key: str) -> dict[str, Any]:
        return _parts(self.rules[rule_key])[1]

    def due_at(self, rule_key: str) -> str:
        return f"{self.date}{self.template(rule_key)['due_at']}"

    def evidence(self, rule_key: str) -> list[str]:
        return list(self.template(rule_key).get("evidence") or [])

    def approval(self, rule_key: str, default: bool = False) -> bool:
        return bool(self.template(rule_key).get("requires_approval", default))

    def rule_version(self, rule_key: str) -> str:
        """该规则定义所在的版本号（供任务追溯与结果归因）。"""
        return str((self.rules.get(rule_key) or {}).get("version") or "")


def evaluate_tasks(snapshot: Mapping[str, Any], rules: Mapping[str, Any],
                   allow_degraded: bool = False) -> list[dict[str, Any]]:
    """按规则定义产出与内置引擎一致的任务列表（含排序）。"""
    missing = [key for key in RULE_ORDER if not rules.get(key)]
    if missing:
        raise RuleEvalError("缺少规则定义：" + "、".join(missing))
    ctx = _Context(snapshot, rules, allow_degraded)
    tasks: list[dict[str, Any]] = []
    tasks += _review_invite(ctx)
    tasks += _negative_review(ctx)
    tasks += _package(ctx)
    tasks += _escalation(ctx)
    tasks += _low_slot(ctx)
    tasks += _inventory(ctx)
    tasks += _competitor(ctx)
    tasks += _data_repair(ctx)
    tasks.sort(key=lambda item: (PRIORITY_ORDER.get(item["priority"], 9), item["task_id"]))
    return tasks


def _review_invite(ctx: _Context) -> list[dict[str, Any]]:
    if not ctx.enabled("review_invite"):
        return []
    reviews = ctx.snapshot.get("reviews", {})
    completed_orders = max(0, int(reviews.get("completed_orders_yesterday", 0)))
    eligible = max(0, int(reviews.get("eligible_review_invites_today", completed_orders)))
    current = max(0, int(reviews.get("qualified_reviews_month", 0)))
    target = max(current, int(reviews.get("qualified_review_target_month", current)))
    days_remaining = max(1, int(reviews.get("days_remaining_in_cycle", 1)))
    review_gap = max(0, target - current)
    invite_target = min(eligible, math.ceil(review_gap / days_remaining))
    if invite_target <= 0:
        return []
    template = ctx.template("review_invite")
    return [_task(
        task_id=f"{ctx.date}-review-invite",
        rule_key="review_invite", rule_version=ctx.rule_version("review_invite"),
        category=template["category"],
        title=f"向 {invite_target} 位真实到店且符合条件的顾客发出评价邀请",
        assignee=ctx.snapshot.get("review_operator", ctx.owner),
        priority=template["priority"],
        due_at=ctx.due_at("review_invite"),
        target={"qualified_review_invites": invite_target},
        evidence=ctx.evidence("review_invite"),
        reason=(f"本周期目标尚差 {review_gap} 条真实有效评价，剩余 {days_remaining} 天；"
                "目标受今日可联系真实顾客数上限约束。"),
        requires_approval=ctx.approval("review_invite"),
    )]


def _negative_review(ctx: _Context) -> list[dict[str, Any]]:
    condition = ctx.condition("negative_review")
    template = ctx.template("negative_review")
    sla_hours = max(1, int(condition.get("sla_hours", 4)))
    approval_when = template.get("requires_approval_when")
    tasks: list[dict[str, Any]] = []
    reviews = ctx.snapshot.get("reviews", {})
    for item in reviews.get("open_negative_reviews", []):
        review_id = str(item.get("review_id", "unknown"))
        age_hours = max(0, int(item.get("age_hours", 0)))
        overdue = age_hours >= sla_hours
        requires_approval = (bool(item.get(approval_when, False)) if approval_when
                             else ctx.approval("negative_review"))
        tasks.append(_task(
            task_id=f"{ctx.date}-negative-{review_id}",
            rule_key="negative_review", rule_version=ctx.rule_version("negative_review"),
            category=template["category"],
            title="处理一条待回复差评并提交补救记录",
            assignee=item.get("assignee") or ctx.owner,
            priority=template.get("priority_when_overdue") if overdue else template["priority"],
            due_at=item.get("due_at") or ctx.due_at("negative_review"),
            target={"review_id": review_id, "sla_hours": sla_hours},
            evidence=ctx.evidence("negative_review"),
            reason=(f"该差评已等待 {age_hours} 小时；不得在任务中暴露手机号，"
                    "仅可使用已授权的平台联系能力。"),
            requires_approval=requires_approval,
        ))
    return tasks


def _package(ctx: _Context) -> list[dict[str, Any]]:
    condition = ctx.condition("package")
    template = ctx.template("package")
    min_conversion = float(condition.get("conversion_floor", 0.05))
    min_views = max(1, int(condition.get("min_views", 50)))
    min_margin = float(condition.get("margin_floor", 0.25))
    tasks: list[dict[str, Any]] = []
    for package in ctx.snapshot.get("packages", []):
        views = max(0, int(package.get("detail_views_7d", 0)))
        orders = max(0, int(package.get("paid_orders_7d", 0)))
        margin = float(package.get("gross_margin_rate", 0))
        conversion = orders / views if views else 0.0
        if views < min_views or conversion >= min_conversion:
            continue
        package_id = str(package.get("package_id", "unknown"))
        tasks.append(_task(
            task_id=f"{ctx.date}-package-{package_id}",
            rule_key="package", rule_version=ctx.rule_version("package"),
            category=template["category"],
            title=f"复核套餐“{package.get('name', package_id)}”并提交单变量调整草稿",
            assignee=ctx.snapshot.get("online_operator", ctx.owner),
            priority=template["priority"],
            due_at=ctx.due_at("package"),
            target={
                "current_conversion": round(conversion, 4),
                "conversion_floor": min_conversion,
                "gross_margin_rate": margin,
                "margin_floor": min_margin,
                "change_one_variable_only": bool(template.get("change_one_variable_only", True)),
            },
            evidence=ctx.evidence("package"),
            reason=(f"近 7 天详情访问 {views}、支付 {orders}，转化率 {conversion:.1%}，"
                    f"低于规则阈值 {min_conversion:.1%}。"),
            requires_approval=ctx.approval("package", default=True),
        ))
    return tasks


def _escalation(ctx: _Context) -> list[dict[str, Any]]:
    condition = ctx.condition("escalation")
    template = ctx.template("escalation")
    high_at = int(condition.get("high_when_missed_at", 2))
    tasks: list[dict[str, Any]] = []
    for overdue_task in ctx.snapshot.get("overdue_staff_tasks", []):
        missed_count = max(1, int(overdue_task.get("missed_count", 1)))
        tasks.append(_task(
            task_id=f"{ctx.date}-escalation-{overdue_task.get('task_id', 'unknown')}",
            rule_key="escalation", rule_version=ctx.rule_version("escalation"),
            category=template["category"],
            title="复核逾期任务并决定是否发送内部提醒/警告",
            assignee=ctx.snapshot.get("area_manager", DEFAULT_AREA_MANAGER),
            priority=template.get("priority_when_repeat") if missed_count >= high_at else template["priority"],
            due_at=ctx.due_at("escalation"),
            target={
                "source_task_id": overdue_task.get("task_id"),
                "employee_id": overdue_task.get("employee_id"),
                "missed_count": missed_count,
            },
            evidence=ctx.evidence("escalation"),
            reason="系统只生成升级草稿；纪律警告和奖惩必须由有权限的管理者确认，且需保留申诉入口。",
            requires_approval=ctx.approval("escalation", default=True),
        ))
    return tasks


def _low_slot(ctx: _Context) -> list[dict[str, Any]]:
    if not ctx.enabled("low_slot"):
        return []
    ops = ctx.snapshot.get("ops") or {}
    window_days = int((ops.get("window") or {}).get("days") or 7)
    thresholds = ops.get("thresholds") or {}
    condition = ctx.condition("low_slot")
    share_floor = float(thresholds.get(
        "low_slot_share_floor", condition.get("share_floor", 0.15)))
    template = ctx.template("low_slot")
    tasks: list[dict[str, Any]] = []
    for slot in ops.get("low_slots") or []:
        slot_name = str(slot.get("slot") or "未命名时段")
        orders = int(slot.get("orders") or 0)
        sample_days = int(slot.get("day_count") or 0)
        tasks.append(_task(
            task_id=f"{ctx.date}-low-slot-{_slug(slot_name)}",
            rule_key="low_slot", rule_version=ctx.rule_version("low_slot"),
            category=template["category"],
            title=f"复核低峰时段「{slot_name}」并提交提效草稿",
            assignee=ctx.snapshot.get("online_operator", ctx.owner),
            priority=template["priority"],
            due_at=ctx.due_at("low_slot"),
            target={
                "slot": slot_name,
                "orders": orders,
                "order_share": slot.get("order_share"),
                "share_floor": share_floor,
                "sample_days": sample_days,
                "change_one_variable_only": bool(template.get("change_one_variable_only", True)),
            },
            evidence=ctx.evidence("low_slot"),
            reason=(f"近 {window_days} 天该时段共 {orders} 单，占全店 {_pct(slot.get('order_share'))}，"
                    f"低于规则阈值 {_pct(share_floor)}（样本 {sample_days} 天）。"
                    "系统只生成提效草稿；涉及价格、时段或投放的调整必须审批。"),
            requires_approval=ctx.approval("low_slot", default=True),
        ))
    return tasks


def _inventory(ctx: _Context) -> list[dict[str, Any]]:
    ops = ctx.snapshot.get("ops") or {}
    alerts = ops.get("inventory_alerts") or []
    if not alerts or not ctx.enabled("inventory"):
        return []
    inventory = ops.get("inventory") or {}
    critical = [item for item in alerts if item.get("level") == "critical"]
    ranked = critical + [item for item in alerts if item.get("level") != "critical"]
    top = ranked[:MAX_INVENTORY_ITEMS_IN_TASK]
    template = ctx.template("inventory")
    return [_task(
        task_id=f"{ctx.date}-inventory-restock",
        rule_key="inventory", rule_version=ctx.rule_version("inventory"),
        category=template["category"],
        title=f"复核 {len(alerts)} 项库存预警并提交补货草稿",
        assignee=ctx.snapshot.get("store_manager", ctx.owner),
        priority=template.get("priority_when_critical") if critical else template["priority"],
        due_at=ctx.due_at("inventory"),
        target={
            "alert_count": len(alerts),
            "critical_count": len(critical),
            "latest_stock_date": inventory.get("latest_date"),
            "items": [
                {
                    "item_id": item.get("item_id"),
                    "name": item.get("name"),
                    "unit": item.get("unit"),
                    "quantity": item.get("quantity"),
                    "safety_stock": item.get("safety_stock"),
                    "level": item.get("level"),
                }
                for item in top
            ],
            "truncated": max(0, len(alerts) - len(top)),
        },
        evidence=ctx.evidence("inventory"),
        reason=(f"最新盘点（{inventory.get('latest_date') or '未记录日期'}）有 {len(alerts)} 项物料"
                f"低于安全库存，其中 {len(critical)} 项严重不足。"
                "系统只生成补货草稿，采购与支出必须由有权限的管理者确认。"),
        requires_approval=ctx.approval("inventory", default=True),
    )]


def _competitor(ctx: _Context) -> list[dict[str, Any]]:
    if not ctx.enabled("competitor"):
        return []
    competitor = ctx.snapshot.get("competitor") or {}
    matches = (competitor.get("packages") or {}).get("matches") or []
    thresholds = competitor.get("thresholds") or {}
    condition = ctx.condition("competitor")
    shift_display = thresholds.get("area_shift_percent",
                                   condition.get("area_shift_percent", 10))
    template = ctx.template("competitor")
    price_block = competitor.get("price") or {}
    p25, p75 = price_block.get("p25"), price_block.get("p75")
    tasks: list[dict[str, Any]] = []
    for area in competitor.get("by_area") or []:
        area_id = str(area.get("area_id") or "unknown")
        area_matches = [item for item in matches if item.get("area_id") == area_id]
        outside = [
            item for item in area_matches
            if p25 is not None and p75 is not None
            and (float(item["store_price"]) < float(p25)
                 or float(item["store_price"]) > float(p75))
        ]
        if not area.get("shifted") and not outside:
            continue
        reasons: list[str] = []
        if area.get("shifted"):
            reasons.append(
                f"该商圈价格带中位数环比 {area.get('change_percent'):+.1f}%，"
                f"达到阈值 ±{shift_display}%"
            )
        if outside:
            names = "、".join(f"{item['competitor_package_name']}（本店 {item['store_price']} vs 竞品 "
                              f"{item['competitor_price']}）" for item in outside[:3])
            reasons.append(f"{len(outside)} 个已确认匹配的套餐落在商圈价格带之外：{names}")
        tasks.append(_task(
            task_id=f"{ctx.date}-competitor-{_slug(area_id)}",
            rule_key="competitor", rule_version=ctx.rule_version("competitor"),
            category=template["category"],
            title=f"复核商圈「{area_id}」竞品变化并提交单变量调整草稿",
            assignee=ctx.snapshot.get("online_operator", ctx.owner),
            priority=template.get("priority_when_outside") if outside else template["priority"],
            due_at=ctx.due_at("competitor"),
            target={
                "area_id": area_id,
                "sample_size": area.get("sample_size"),
                "current_median": area.get("current_median"),
                "previous_median": area.get("previous_median"),
                "change_percent": area.get("change_percent"),
                "shifted": bool(area.get("shifted")),
                "outside_band_count": len(outside),
                "change_one_variable_only": bool(template.get("change_one_variable_only", True)),
            },
            evidence=ctx.evidence("competitor"),
            reason="；".join(reasons) + "。系统只生成调整草稿；改价、赔付与公开回复必须审批。",
            requires_approval=ctx.approval("competitor", default=True),
        ))
    return tasks


def _data_repair(ctx: _Context) -> list[dict[str, Any]]:
    """兜底任务：不是表里的业务规则，规则被数据阻塞时由引擎直接生成。"""
    if not ctx.blocked or ctx.allow_degraded:
        return []
    reasons = "；".join(f"{name}: {item.get('reason', '数据不可用')}"
                        for name, item in ctx.blocked.items())
    return [_task(
        task_id=f"{ctx.date}-data-repair",
        category="management",
        title="补齐缺失数据并修复配置后重新生成计划",
        assignee=ctx.owner,
        priority="high",
        due_at=f"{ctx.date}T12:00:00+08:00",
        target={"blocked_rules": sorted(ctx.blocked), "reasons": reasons},
        evidence=["补导报表文件", "配置修正记录", "重新执行 plan 的结果"],
        reason=f"系统检测到依赖数据不可用，已跳过相关规则：{reasons}",
    )]