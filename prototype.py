#!/usr/bin/env python3
"""门店运营系统经营动作引擎的最小可运行样板。

输入是一家门店某日的经营快照；输出是可派发、可验收、可审计的动作清单。
本样板只生成建议和审批草稿，不连接平台、不发送消息、不执行奖惩。
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


MAX_INVENTORY_ITEMS_IN_TASK = 10      # 单个补货草稿最多列出多少项物料，避免任务描述无限膨胀
OPS_CATEGORY = "operations"          # 门店侧经营动作（时段提效 / 库存补货）
OPS_COMPETITOR_CATEGORY = "competitor"  # 竞品对比动作（第 11 期）
BUILTIN_RULE_VERSION = "1"             # 内置规则（未外置时）等同规则库 v1，供任务追溯


def _slug(value: str) -> str:
    """把时段名等文本转成稳定的 task_id 片段（中文保留，其余符号折叠为短横线）。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", str(value).strip()).strip("-")
    return cleaned or "unknown"


def _pct(value: Any) -> str:
    return "未知" if value is None else f"{float(value):.1%}"


def _task(
    *,
    task_id: str,
    category: str,
    title: str,
    assignee: str,
    priority: str,
    due_at: str,
    target: dict[str, Any],
    evidence: list[str],
    reason: str,
    requires_approval: bool = False,
    rule_key: str = "",
    rule_version: str = "",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "category": category,
        "title": title,
        "assignee": assignee,
        "priority": priority,
        "due_at": due_at,
        "target": target,
        "evidence_required": evidence,
        "reason": reason,
        "requires_approval": requires_approval,
        "status": "proposed" if requires_approval else "ready",
        # 第 6 步：任务必须能追溯到产生它的规则与版本（便于结果归因回灌）
        "rule_key": rule_key,
        "rule_version": rule_version,
    }


def generate_daily_plan(snapshot: dict[str, Any], allow_degraded: bool = False,
                        rules: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """依据透明规则生成每日动作，不调用模型，不产生外部副作用。

    默认（`rules=None`）使用本模块内置规则，行为保持不变；传入规则定义时改用
    `rule_eval` 表驱动执行器。规则不可用时是否回退内置由调用方决定，本函数不静默降级。
    """
    if rules is not None:
        from rule_eval import evaluate_tasks  # 延迟导入：rule_eval 复用本模块的任务构造
        return _plan_payload(snapshot["store"], snapshot["date"],
                             evaluate_tasks(snapshot, rules, allow_degraded))
    store = snapshot["store"]
    date = snapshot["date"]
    owner = snapshot.get("store_manager", "店长")
    rules = snapshot.get("rules", {})
    readiness = (snapshot.get("_meta") or {}).get("rule_readiness") or {}
    blocked = {key: value for key, value in readiness.items() if value.get("status") == "blocked"}
    tasks: list[dict[str, Any]] = []

    reviews = snapshot.get("reviews", {})
    completed_orders = max(0, int(reviews.get("completed_orders_yesterday", 0)))
    eligible_customers = max(0, int(reviews.get("eligible_review_invites_today", completed_orders)))
    current_qualified = max(0, int(reviews.get("qualified_reviews_month", 0)))
    target_qualified = max(current_qualified, int(reviews.get("qualified_review_target_month", current_qualified)))
    days_remaining = max(1, int(reviews.get("days_remaining_in_cycle", 1)))
    if allow_degraded and days_remaining == 0:
        days_remaining = 1
    review_gap = max(0, target_qualified - current_qualified)
    invite_target = min(eligible_customers, math.ceil(review_gap / days_remaining))

    if invite_target > 0 and (not blocked.get("review_invite") or allow_degraded):
        tasks.append(
            _task(
                task_id=f"{date}-review-invite",
                rule_key="review_invite", rule_version=BUILTIN_RULE_VERSION,
                category="reputation",
                title=f"向 {invite_target} 位真实到店且符合条件的顾客发出评价邀请",
                assignee=snapshot.get("review_operator", owner),
                priority="medium",
                due_at=f"{date}T21:00:00+08:00",
                target={"qualified_review_invites": invite_target},
                evidence=["平台订单/核销号（脱敏）", "邀请时间", "执行员工"],
                reason=f"本周期目标尚差 {review_gap} 条真实有效评价，剩余 {days_remaining} 天；目标受今日可联系真实顾客数上限约束。",
            )
        )

    negative_sla_hours = max(1, int(rules.get("negative_review_sla_hours", 4)))
    for item in reviews.get("open_negative_reviews", []):
        review_id = str(item.get("review_id", "unknown"))
        age_hours = max(0, int(item.get("age_hours", 0)))
        overdue = age_hours >= negative_sla_hours
        tasks.append(
            _task(
                task_id=f"{date}-negative-{review_id}",
                rule_key="negative_review", rule_version=BUILTIN_RULE_VERSION,
                category="reputation",
                title="处理一条待回复差评并提交补救记录",
                assignee=item.get("assignee") or owner,
                priority="urgent" if overdue else "high",
                due_at=item.get("due_at") or f"{date}T18:00:00+08:00",
                target={"review_id": review_id, "sla_hours": negative_sla_hours},
                evidence=["平台回复截图/记录", "问题分类", "补救结果", "是否需要店长复盘"],
                reason=f"该差评已等待 {age_hours} 小时；不得在任务中暴露手机号，仅可使用已授权的平台联系能力。",
                requires_approval=bool(item.get("compensation_requested", False)),
            )
        )

    min_conversion = float(rules.get("package_conversion_floor", 0.05))
    min_views = max(1, int(rules.get("package_min_views", 50)))
    min_margin = float(rules.get("package_margin_floor", 0.25))
    for package in snapshot.get("packages", []):
        views = max(0, int(package.get("detail_views_7d", 0)))
        orders = max(0, int(package.get("paid_orders_7d", 0)))
        margin = float(package.get("gross_margin_rate", 0))
        conversion = orders / views if views else 0.0
        if views >= min_views and conversion < min_conversion:
            package_id = str(package.get("package_id", "unknown"))
            tasks.append(
                _task(
                    task_id=f"{date}-package-{package_id}",
                    rule_key="package", rule_version=BUILTIN_RULE_VERSION,
                    category="package",
                    title=f"复核套餐“{package.get('name', package_id)}”并提交单变量调整草稿",
                    assignee=snapshot.get("online_operator", owner),
                    priority="high",
                    due_at=f"{date}T17:00:00+08:00",
                    target={
                        "current_conversion": round(conversion, 4),
                        "conversion_floor": min_conversion,
                        "gross_margin_rate": margin,
                        "margin_floor": min_margin,
                        "change_one_variable_only": True,
                    },
                    evidence=["调整前页面快照", "价格与毛利复核", "唯一变更变量", "审批记录"],
                    reason=f"近 7 天详情访问 {views}、支付 {orders}，转化率 {conversion:.1%}，低于规则阈值 {min_conversion:.1%}。",
                    requires_approval=True,
                )
            )

    for overdue_task in snapshot.get("overdue_staff_tasks", []):
        missed_count = max(1, int(overdue_task.get("missed_count", 1)))
        tasks.append(
            _task(
                task_id=f"{date}-escalation-{overdue_task.get('task_id', 'unknown')}",
                rule_key="escalation", rule_version=BUILTIN_RULE_VERSION,
                category="management",
                title="复核逾期任务并决定是否发送内部提醒/警告",
                assignee=snapshot.get("area_manager", "区域负责人"),
                priority="high" if missed_count >= 2 else "medium",
                due_at=f"{date}T19:00:00+08:00",
                target={
                    "source_task_id": overdue_task.get("task_id"),
                    "employee_id": overdue_task.get("employee_id"),
                    "missed_count": missed_count,
                },
                evidence=["原任务要求", "截止时间", "员工提交记录", "负责人复核结论"],
                reason="系统只生成升级草稿；纪律警告和奖惩必须由有权限的管理者确认，且需保留申诉入口。",
                requires_approval=True,
            )
        )

    # ---- 门店侧规则一：低峰时段提效（数据前提：时段分布导入） ----
    ops = snapshot.get("ops") or {}
    ops_window_days = int((ops.get("window") or {}).get("days") or 7)
    ops_thresholds = ops.get("thresholds") or {}
    low_slot_share_floor = float(
        ops_thresholds.get("low_slot_share_floor", rules.get("low_slot_share_floor", 0.15))
    )
    if not blocked.get("low_slot") or allow_degraded:
        for slot in ops.get("low_slots") or []:
            slot_name = str(slot.get("slot") or "未命名时段")
            orders = int(slot.get("orders") or 0)
            sample_days = int(slot.get("day_count") or 0)
            tasks.append(
                _task(
                    task_id=f"{date}-low-slot-{_slug(slot_name)}",
                    rule_key="low_slot", rule_version=BUILTIN_RULE_VERSION,
                    category=OPS_CATEGORY,
                    title=f"复核低峰时段「{slot_name}」并提交提效草稿",
                    assignee=snapshot.get("online_operator", owner),
                    priority="medium",
                    due_at=f"{date}T17:00:00+08:00",
                    target={
                        "slot": slot_name,
                        "orders": orders,
                        "order_share": slot.get("order_share"),
                        "share_floor": low_slot_share_floor,
                        "sample_days": sample_days,
                        "change_one_variable_only": True,
                    },
                    evidence=["时段经营数据截图", "低峰原因分析", "唯一变更变量", "审批记录"],
                    reason=(
                        f"近 {ops_window_days} 天该时段共 {orders} 单，占全店 {_pct(slot.get('order_share'))}，"
                        f"低于规则阈值 {_pct(low_slot_share_floor)}（样本 {sample_days} 天）。"
                        "系统只生成提效草稿；涉及价格、时段或投放的调整必须审批。"
                    ),
                    requires_approval=True,
                )
            )

    # ---- 门店侧规则二：库存/物料预警（数据前提：库存盘点导入） ----
    inventory_alerts = ops.get("inventory_alerts") or []
    if inventory_alerts and (not blocked.get("inventory") or allow_degraded):
        inventory = ops.get("inventory") or {}
        critical = [item for item in inventory_alerts if item.get("level") == "critical"]
        ranked = critical + [item for item in inventory_alerts if item.get("level") != "critical"]
        top = ranked[:MAX_INVENTORY_ITEMS_IN_TASK]
        tasks.append(
            _task(
                task_id=f"{date}-inventory-restock",
                rule_key="inventory", rule_version=BUILTIN_RULE_VERSION,
                category=OPS_CATEGORY,
                title=f"复核 {len(inventory_alerts)} 项库存预警并提交补货草稿",
                assignee=snapshot.get("store_manager", owner),
                priority="urgent" if critical else "high",
                due_at=f"{date}T12:00:00+08:00",
                target={
                    "alert_count": len(inventory_alerts),
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
                    "truncated": max(0, len(inventory_alerts) - len(top)),
                },
                evidence=["盘点结果截图", "补货数量与预计到货日期", "采购审批记录"],
                reason=(
                    f"最新盘点（{inventory.get('latest_date') or '未记录日期'}）有 {len(inventory_alerts)} 项物料"
                    f"低于安全库存，其中 {len(critical)} 项严重不足。"
                    "系统只生成补货草稿，采购与支出必须由有权限的管理者确认。"
                ),
                requires_approval=True,
            )
        )

    # ---- 竞品规则：每个商圈最多一条（第 11 期，并入原第 2 期 D 批） ----
    competitor = snapshot.get("competitor") or {}
    if not blocked.get("competitor") or allow_degraded:
        matches = (competitor.get("packages") or {}).get("matches") or []
        for area in competitor.get("by_area") or []:
            area_id = str(area.get("area_id") or "unknown")
            area_matches = [item for item in matches if item.get("area_id") == area_id]
            price_block = competitor.get("price") or {}
            p25, p75 = price_block.get("p25"), price_block.get("p75")
            outside = [
                item for item in area_matches
                if p25 is not None and p75 is not None
                and (float(item["store_price"]) < float(p25)
                     or float(item["store_price"]) > float(p75))
            ]
            if not area.get("shifted") and not outside:
                continue
            reasons = []
            if area.get("shifted"):
                reasons.append(
                    f"该商圈价格带中位数环比 {area.get('change_percent'):+.1f}%，"
                    f"达到阈值 ±{competitor.get('thresholds', {}).get('area_shift_percent', 10)}%"
                )
            if outside:
                names = "、".join(f"{item['competitor_package_name']}（本店 {item['store_price']} vs 竞品 "
                                  f"{item['competitor_price']}）" for item in outside[:3])
                reasons.append(f"{len(outside)} 个已确认匹配的套餐落在商圈价格带之外：{names}")
            tasks.append(
                _task(
                    task_id=f"{date}-competitor-{_slug(area_id)}",
                    rule_key="competitor", rule_version=BUILTIN_RULE_VERSION,
                    category=OPS_COMPETITOR_CATEGORY,
                    title=f"复核商圈「{area_id}」竞品变化并提交单变量调整草稿",
                    assignee=snapshot.get("online_operator", owner),
                    priority="high" if outside else "medium",
                    due_at=f"{date}T17:00:00+08:00",
                    target={
                        "area_id": area_id,
                        "sample_size": area.get("sample_size"),
                        "current_median": area.get("current_median"),
                        "previous_median": area.get("previous_median"),
                        "change_percent": area.get("change_percent"),
                        "shifted": bool(area.get("shifted")),
                        "outside_band_count": len(outside),
                        "change_one_variable_only": True,
                    },
                    evidence=["竞品采样截图", "采样时间与采集方式", "唯一变更变量", "审批记录"],
                    reason="；".join(reasons) + "。系统只生成调整草稿；改价、赔付与公开回复必须审批。",
                    requires_approval=True,
                )
            )

    if blocked and not allow_degraded:
        reasons = "；".join(f"{name}: {item.get('reason', '数据不可用')}" for name, item in blocked.items())
        tasks.append(
            _task(
                task_id=f"{date}-data-repair",
                category="management",
                title="补齐缺失数据并修复配置后重新生成计划",
                assignee=owner,
                priority="high",
                due_at=f"{date}T12:00:00+08:00",
                target={"blocked_rules": sorted(blocked), "reasons": reasons},
                evidence=["补导报表文件", "配置修正记录", "重新执行 plan 的结果"],
                reason=f"系统检测到依赖数据不可用，已跳过相关规则：{reasons}",
                requires_approval=False,
            )
        )

    return _plan_payload(store, date, tasks)


def _plan_payload(store: dict[str, Any], date: str,
                  tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """统一收口排序与返回结构：内置分支与表驱动分支共用，保证产出形态一致。"""
    priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    tasks.sort(key=lambda item: (priority_order.get(item["priority"], 9), item["task_id"]))
    return {
        "schema_version": "1.0",
        "store": store,
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "draft_only",
        "summary": {
            "task_count": len(tasks),
            "ready_count": sum(not task["requires_approval"] for task in tasks),
            "approval_count": sum(task["requires_approval"] for task in tasks),
        },
        "tasks": tasks,
        "guardrails": [
            "不得购买、诱导或伪造评价，只面向真实到店顾客合规邀请",
            "不得抓取或在任务中暴露顾客手机号等敏感信息",
            "套餐价格、补偿、消息发送、纪律警告和奖惩必须人工审批",
            "20% 流量提升是待验证目标，不是本引擎的保证结果",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="根据门店经营快照生成每日动作 JSON")
    parser.add_argument("input", type=Path, help="输入 JSON 文件")
    parser.add_argument("-o", "--output", type=Path, help="输出 JSON 文件；不传则打印到标准输出")
    args = parser.parse_args()
    snapshot = json.loads(args.input.read_text(encoding="utf-8"))
    result = generate_daily_plan(snapshot)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
