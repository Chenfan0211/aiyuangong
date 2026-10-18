#!/usr/bin/env python3
"""经营快照组装层：把数据库真实数据 + store_config.json 组装成规则引擎的输入。

作用：替代手填的 `sample_input.json`。除"平台拿不到的字段"（成本、月度目标、责任人）
集中在 `store_config.json` 外，快照里的经营数字全部来自已落库的平台数据与评价明细。

对应关系（快照字段 → 自动来源）：
  reviews.completed_orders_yesterday        ← 前一日的核销订单合计
  reviews.eligible_review_invites_today     ← 默认等于上者（平台不提供"可联系顾客数"），可用 daily_invite_cap 收紧
  reviews.qualified_reviews_month           ← 本周期 rating>=4 且已回复/已解决的评价条数
  reviews.days_remaining_in_cycle           ← cycle_end - 目标日期（不再手填）
  reviews.open_negative_reviews[*].age_hours ← rating<=2 且 status=open 的评价，按创建时间算等待小时数
  packages[*]                               ← 近 7 天套餐明细聚合 + store_config 的售价/成本算毛利
  overdue_staff_tasks                       ← 任务库中已逾期且未关闭的任务
  rules                                     ← store_config.rules
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

from platform_data import PlatformMetricStore

PACKAGE_WINDOW_DAYS = 7
NEGATIVE_RATING_MAX = 2
QUALIFIED_RATING_MIN = 4
RESOLVED_REVIEW_STATUSES = ("replied", "resolved")


class SnapshotError(ValueError):
    """配置缺失、日期非法等无法组装快照的情况。"""


def load_config(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SnapshotError(f"门店配置不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"门店配置不是合法 JSON：{path}（{exc}）") from exc


def parse_target_date(value: date | str | None) -> date:
    if value is None or value == "today":
        return date.today()
    if value == "yesterday":
        return date.today() - timedelta(days=1)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise SnapshotError(f"日期必须是 YYYY-MM-DD / today / yesterday：{value}") from exc


def _cycle(config: dict[str, Any], target_date: date) -> tuple[date, date, int]:
    cycle = config.get("review_cycle") or {}
    start_raw = cycle.get("cycle_start")
    end_raw = cycle.get("cycle_end")
    start = date.fromisoformat(str(start_raw)) if start_raw else target_date.replace(day=1)
    if end_raw:
        end = date.fromisoformat(str(end_raw))
    else:
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if end < start:
        raise SnapshotError(f"评价周期结束日早于开始日：{start} ~ {end}")
    return start, end, int(cycle.get("target_qualified_month") or 0)


def _margin_rate(config: dict[str, Any], platform: str, package_id: str) -> float | None:
    """返回毛利率；缺售价或成本时返回 None（不产生调价建议）。"""
    for item in config.get("package_costs") or []:
        if item.get("platform") == platform and str(item.get("package_id")) == str(package_id):
            price = float(item.get("sale_price") or 0)
            cost = float(item.get("variable_cost") or 0)
            if price <= 0:
                return None
            return round((price - cost) / price, 4)
    return None


def build_snapshot(
    config: dict[str, Any],
    metric_store: PlatformMetricStore,
    target_date: date | str | None = None,
    task_store: Optional[Any] = None,
) -> dict[str, Any]:
    """组装 generate_daily_plan() 的输入快照（额外包含 _meta 诊断信息，规则引擎会忽略）。"""
    day = parse_target_date(target_date)
    day_iso = day.isoformat()
    store_id = str(config.get("store_id") or "")
    if not store_id:
        raise SnapshotError("store_config.json 缺少 store_id")
    people = config.get("people") or {}
    rules = config.get("rules") or {}
    notes: list[str] = []

    cycle_start, cycle_end, target_qualified = _cycle(config, day)

    # 1) 昨日核销订单 → 今日评价邀请上限
    yesterday = day - timedelta(days=1)
    yesterday_rows = metric_store.read_daily_metrics(store_id, yesterday.isoformat(), yesterday.isoformat())
    completed_orders_yesterday = sum(int(row["verified_orders"] or 0) for row in yesterday_rows)
    if not yesterday_rows:
        notes.append(f"缺少 {yesterday.isoformat()} 的平台数据，昨日核销订单按 0 计，可能不生成评价邀请任务")
    invite_cap = (config.get("review_cycle") or {}).get("daily_invite_cap")
    eligible_invites = (
        completed_orders_yesterday if invite_cap in (None, "")
        else min(completed_orders_yesterday, int(invite_cap))
    )

    # 2) 评价明细 → 有效评价数、待处理差评
    cycle_reviews = metric_store.read_reviews(store_id, since=cycle_start.isoformat(), until=day_iso)
    qualified_reviews = [
        row for row in cycle_reviews
        if float(row["rating"] or 0) >= QUALIFIED_RATING_MIN and row["status"] in RESOLVED_REVIEW_STATUSES
    ]
    open_negatives = [
        row for row in metric_store.read_reviews(store_id)
        if row["rating"] is not None and float(row["rating"]) <= NEGATIVE_RATING_MAX and row["status"] == "open"
    ]
    reference_time = datetime.now() if day == date.today() else datetime.combine(day, time(23, 59, 59))
    negative_items = [
        {
            "review_id": row["review_id"],
            "age_hours": max(0, int((reference_time - datetime.fromisoformat(row["created_at"])).total_seconds() // 3600)),
            "assignee": people.get("store_manager"),
            "compensation_requested": False,  # 是否赔付由店长在审批时标记，不由系统推断
            "due_at": None,
        }
        for row in open_negatives
    ]

    # 3) 近 7 天套餐明细 → 转化与毛利
    package_start = day - timedelta(days=PACKAGE_WINDOW_DAYS - 1)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in metric_store.read_package_metrics(store_id, package_start.isoformat(), day_iso):
        key = (row["platform"], row["package_id"])
        item = grouped.setdefault(key, {
            "package_id": row["package_id"],
            "name": row["package_name"],
            "platform": row["platform"],
            "detail_views_7d": 0,
            "paid_orders_7d": 0,
            "verified_orders_7d": 0,
            "gmv_7d": 0.0,
        })
        item["name"] = row["package_name"] or item["name"]
        item["detail_views_7d"] += int(row["detail_views"] or 0)
        item["paid_orders_7d"] += int(row["paid_orders"] or 0)
        item["verified_orders_7d"] += int(row["verified_orders"] or 0)
        item["gmv_7d"] += float(row["gmv"] or 0)

    packages: list[dict[str, Any]] = []
    for (platform, package_id), item in sorted(grouped.items()):
        margin = _margin_rate(config, platform, package_id)
        if margin is None:
            notes.append(
                f"套餐「{item['name']}」（{platform} {package_id}）缺少售价或成本，已跳过套餐调整规则，不生成调价建议"
            )
            continue
        packages.append({
            "package_id": item["package_id"],
            "name": item["name"],
            "platform": platform,
            "detail_views_7d": item["detail_views_7d"],
            "paid_orders_7d": item["paid_orders_7d"],
            "verified_orders_7d": item["verified_orders_7d"],
            "gmv_7d": round(item["gmv_7d"], 2),
            "gross_margin_rate": margin,
        })

    # 4) 逾期任务 → 升级复核草稿
    if task_store is not None:
        overdue_tasks = task_store.overdue_tasks(store_id)
    else:
        overdue_tasks = []
        notes.append("未接入任务库（task_store），逾期升级规则本次不参与生成")

    return {
        "store": {
            "tenant_id": config.get("tenant_id"),
            "store_id": store_id,
            "name": config.get("name"),
        },
        "date": day_iso,
        "store_manager": people.get("store_manager", "店长"),
        "review_operator": people.get("review_operator", people.get("store_manager", "店长")),
        "online_operator": people.get("online_operator", people.get("store_manager", "店长")),
        "area_manager": people.get("area_manager", "区域负责人"),
        "reviews": {
            "completed_orders_yesterday": completed_orders_yesterday,
            "eligible_review_invites_today": eligible_invites,
            "qualified_reviews_month": len(qualified_reviews),
            "qualified_review_target_month": max(len(qualified_reviews), target_qualified),
            "days_remaining_in_cycle": max(1, (cycle_end - day).days + 1),
            "open_negative_reviews": negative_items,
        },
        "packages": packages,
        "overdue_staff_tasks": overdue_tasks,
        "rules": {
            "negative_review_sla_hours": int(rules.get("negative_review_sla_hours", 4)),
            "package_conversion_floor": float(rules.get("package_conversion_floor", 0.05)),
            "package_min_views": int(rules.get("package_min_views", 50)),
            "package_margin_floor": float(rules.get("package_margin_floor", 0.25)),
        },
        "_meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": "sqlite(platform_metrics/package_metrics/reviews) + store_config.json",
            "cycle": {"start": cycle_start.isoformat(), "end": cycle_end.isoformat(), "target_qualified": target_qualified},
            "package_window": {"start": package_start.isoformat(), "end": day_iso, "days": PACKAGE_WINDOW_DAYS},
            "counts": {
                "yesterday_rows": len(yesterday_rows),
                "cycle_reviews": len(cycle_reviews),
                "qualified_reviews": len(qualified_reviews),
                "open_negatives": len(negative_items),
                "packages": len(packages),
                "overdue_tasks": len(overdue_tasks),
            },
            "notes": notes,
        },
    }
