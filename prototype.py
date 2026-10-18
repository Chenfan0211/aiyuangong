#!/usr/bin/env python3
"""门店运营系统经营动作引擎的最小可运行样板。

输入是一家门店某日的经营快照；输出是可派发、可验收、可审计的动作清单。
本样板只生成建议和审批草稿，不连接平台、不发送消息、不执行奖惩。
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    }


def generate_daily_plan(snapshot: dict[str, Any]) -> dict[str, Any]:
    """依据透明规则生成每日动作，不调用模型，不产生外部副作用。"""
    store = snapshot["store"]
    date = snapshot["date"]
    owner = snapshot.get("store_manager", "店长")
    rules = snapshot.get("rules", {})
    tasks: list[dict[str, Any]] = []

    reviews = snapshot.get("reviews", {})
    completed_orders = max(0, int(reviews.get("completed_orders_yesterday", 0)))
    eligible_customers = max(0, int(reviews.get("eligible_review_invites_today", completed_orders)))
    current_qualified = max(0, int(reviews.get("qualified_reviews_month", 0)))
    target_qualified = max(current_qualified, int(reviews.get("qualified_review_target_month", current_qualified)))
    days_remaining = max(1, int(reviews.get("days_remaining_in_cycle", 1)))
    review_gap = max(0, target_qualified - current_qualified)
    invite_target = min(eligible_customers, math.ceil(review_gap / days_remaining))

    if invite_target > 0:
        tasks.append(
            _task(
                task_id=f"{date}-review-invite",
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
