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
OPS_WINDOW_DAYS = 7          # 门店侧（时段/开台）看窗，与套餐窗口一致
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


def save_config(path: Path, config: dict[str, Any]) -> None:
    """Validate and atomically replace the JSON configuration file."""
    if not isinstance(config, dict):
        raise SnapshotError("配置必须是 JSON 对象")
    for key in ("tenant_id", "store_id", "name", "review_cycle", "rules"):
        if key not in config:
            raise SnapshotError(f"配置缺少 {key}")
    cycle = config.get("review_cycle")
    if not isinstance(cycle, dict) or not cycle.get("cycle_start") or not cycle.get("cycle_end"):
        raise SnapshotError("review_cycle 必须包含 cycle_start 和 cycle_end")
    if date.fromisoformat(str(cycle["cycle_end"])) < date.fromisoformat(str(cycle["cycle_start"])):
        raise SnapshotError("评价周期结束日不能早于开始日")
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


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
    ops_store: Optional[Any] = None,
    competitor_store: Optional[Any] = None,
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

    # 3.5) 门店侧经营数据（第 1 期）：时段分布与库存预警
    #      这两个维度属于"可选新增数据源"：没有接入或还没导入时不报错、不产出任务，
    #      只把原因写进 rule_readiness，由店长决定什么时候开始用。
    ops_window_start = day - timedelta(days=OPS_WINDOW_DAYS - 1)
    slot_min_days = max(1, int(rules.get("slot_min_days", 3)))
    low_slot_share_floor = float(rules.get("low_slot_share_floor", 0.15))
    ops_payload: dict[str, Any] = {
        "window": {"start": ops_window_start.isoformat(), "end": day_iso, "days": OPS_WINDOW_DAYS},
        "traffic": {"has_data": False, "day_count": 0},
        "time_slots": [],
        "low_slots": [],
        "thresholds": {"slot_min_days": slot_min_days, "low_slot_share_floor": low_slot_share_floor},
        "inventory": {"has_data": False, "latest_date": None, "day_count": 0, "alert_count": 0},
        "inventory_alerts": [],
    }
    if ops_store is None:
        notes.append("未接入门店侧数据（ops_store），时段提效与库存预警规则本次不参与生成")
    else:
        ops_summary = ops_store.summary(store_id, days=OPS_WINDOW_DAYS, today=day)
        low_slots = [
            slot for slot in ops_summary.time_slots
            if slot["day_count"] >= slot_min_days
            and slot["order_share"] is not None
            and slot["order_share"] < low_slot_share_floor
        ]
        ops_payload.update({
            "traffic": ops_summary.traffic,
            "time_slots": ops_summary.time_slots,
            "low_slots": low_slots,
            "inventory": ops_summary.inventory,
            "inventory_alerts": ops_summary.alerts,
        })

    # 3.8) 竞品四维对比（第 11 期，并入原第 2 期 D 批）
    competitor_payload: dict[str, Any] = {
        "has_data": False, "window": {}, "thresholds": {},
        "by_area": [], "price": {}, "packages": {"matches": []},
        "activities": {}, "area_change": {},
        "unmatched_competitor_packages": [], "truncated_matches": 0,
    }
    if competitor_store is None:
        notes.append("未接入竞品数据（competitor_store），竞品对比规则本次不参与生成")
    else:
        comparison = competitor_store.compare(store_id, config, today=day)
        competitor_payload = comparison

    # 4) 规则就绪状态：缺数只阻断依赖该数据的规则，不补零
    rule_readiness: dict[str, dict[str, str]] = {
        "review_invite": {"status": "ready", "reason": ""},
        "negative_review": {"status": "ready", "reason": ""},
        "package": {"status": "ready", "reason": ""},
        "overdue": {"status": "ready", "reason": ""},
        "low_slot": {"status": "ready", "reason": ""},
        "inventory": {"status": "ready", "reason": ""},
        "competitor": {"status": "ready", "reason": ""},
    }
    if day > cycle_end:
        rule_readiness["review_invite"] = {
            "status": "blocked",
            "reason": f"评价周期已于 {cycle_end.isoformat()} 结束，请更新 review_cycle 配置",
        }
    elif not yesterday_rows:
        rule_readiness["review_invite"] = {
            "status": "blocked",
            "reason": f"缺少 {yesterday.isoformat()} 的平台核销数据，无法计算评价邀请上限",
        }
    if not packages:
        rule_readiness["package"] = {
            "status": "degraded",
            "reason": "近 7 天没有可用套餐明细，已跳过套餐调整规则",
        }
    if task_store is None:
        rule_readiness["overdue"] = {
            "status": "degraded",
            "reason": "未接入任务库，已跳过逾期升级规则",
        }
    # 门店侧两维度：一律用 degraded（不阻塞），避免在门店还没开始导入这两类数据时
    # 每天都产出"补齐缺失数据"的任务；缺数原因通过 reason 与 notes 暴露。
    if ops_store is None:
        rule_readiness["low_slot"] = {
            "status": "degraded",
            "reason": "未接入门店侧数据，已跳过低峰时段规则",
        }
        rule_readiness["inventory"] = {
            "status": "degraded",
            "reason": "未接入门店侧数据，已跳过库存预警规则",
        }
    else:
        slots = ops_payload["time_slots"]
        enough = [item for item in slots if item["day_count"] >= slot_min_days]
        if not slots:
            rule_readiness["low_slot"] = {
                "status": "degraded",
                "reason": f"近 {OPS_WINDOW_DAYS} 天没有时段数据，已跳过低峰时段规则",
            }
        elif not enough:
            rule_readiness["low_slot"] = {
                "status": "degraded",
                "reason": f"时段样本天数不足（单时段需 ≥ {slot_min_days} 天），已跳过低峰时段规则",
            }
        if not ops_payload["inventory"]["has_data"]:
            rule_readiness["inventory"] = {
                "status": "degraded",
                "reason": "没有库存盘点数据，已跳过库存预警规则",
            }
    # 竞品同理用 degraded（不阻塞）：门店还没开始采样时，不该天天产出"补竞品数据"的任务
    if competitor_store is None:
        rule_readiness["competitor"] = {
            "status": "degraded", "reason": "未接入竞品数据，已跳过竞品对比规则",
        }
    elif not competitor_payload.get("has_data"):
        rule_readiness["competitor"] = {
            "status": "degraded",
            "reason": f"近 {int((rules.get('competitor_window_days') or 28))} 天没有竞品采样数据，已跳过竞品对比规则",
        }
    elif not competitor_payload.get("packages", {}).get("matches"):
        rule_readiness["competitor"] = {
            "status": "degraded",
            "reason": "没有已确认的竞品匹配，无法做价格与套餐对比（请先在竞品匹配里人工确认）",
        }

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
            "days_remaining_in_cycle": max(0, (cycle_end - day).days + 1),
            "open_negative_reviews": negative_items,
        },
        "packages": packages,
        "overdue_staff_tasks": overdue_tasks,
        "ops": ops_payload,
        "competitor": competitor_payload,
        "rules": {
            "negative_review_sla_hours": int(rules.get("negative_review_sla_hours", 4)),
            "package_conversion_floor": float(rules.get("package_conversion_floor", 0.05)),
            "package_min_views": int(rules.get("package_min_views", 50)),
            "package_margin_floor": float(rules.get("package_margin_floor", 0.25)),
        },
        "_meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": "platform_metrics/package_metrics/reviews + "
                      "store_traffic_daily/time_slot_metrics/inventory_items + store_config.json",
            "cycle": {"start": cycle_start.isoformat(), "end": cycle_end.isoformat(), "target_qualified": target_qualified},
            "package_window": {"start": package_start.isoformat(), "end": day_iso, "days": PACKAGE_WINDOW_DAYS},
            "counts": {
                "yesterday_rows": len(yesterday_rows),
                "cycle_reviews": len(cycle_reviews),
                "qualified_reviews": len(qualified_reviews),
                "open_negatives": len(negative_items),
                "packages": len(packages),
                "overdue_tasks": len(overdue_tasks),
                "ops_time_slots": len(ops_payload["time_slots"]),
                "ops_low_slots": len(ops_payload["low_slots"]),
                "ops_inventory_alerts": len(ops_payload["inventory_alerts"]),
                "competitor_areas": len(competitor_payload.get("by_area") or []),
                "competitor_matches": len(competitor_payload.get("packages", {}).get("matches") or []),
            },
            "rule_readiness": rule_readiness,
            "notes": notes,
        },
    }
