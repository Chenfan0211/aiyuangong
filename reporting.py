#!/usr/bin/env python3
"""经营看板与周/月报（二期批次 11）。

输入全部来自已落库数据（`platform_metrics` / `package_metrics` / `action_tasks` / `approvals` /
`audit_events`），因此"看板上的每个数字都能追溯到导入批次"，不使用演示值。

三件产物：
  * `dashboard()`      —— 看板数据：KPI + 环比 + 每日趋势 + 漏斗 + 套餐榜 + 任务/审批/数据质量
  * `weekly_report()`  —— 周报文本（近 7 天，含最好/最差一天与下周建议）
  * `monthly_report()` —— 月报文本（自然月，含与上月对比与套餐表现）

权限约定：看板与报表都按"是否含财务口径"输出，调用方按 `metrics:read:financial` 决定；
本模块不判断角色，只提供 `include_financial` 开关（与 agent_tools.metric_summary 一致）。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from platform_data import PLATFORMS, PlatformMetricStore
from snapshot_builder import parse_target_date
from task_store import OPEN_STATUSES, TaskStore

FUNNEL = ("impressions", "detail_views", "paid_orders", "verified_orders")
FINANCIAL = ("gmv", "refunds")
FINANCIAL_RATES = ("refund_rate",)
DASHBOARD_TOP_PACKAGES = 5


def _sum(rows: list[dict[str, Any]], metric: str) -> float:
    total = 0.0
    for row in rows:
        value = row.get(metric)
        if value is not None:
            total += float(value)
    return total


def _round(value: float, digits: int = 2) -> float:
    return round(value, digits)


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把若干天的平台行聚合成统一口径（与 period_summary 的派生率一致）。"""
    impressions = _sum(rows, "impressions")
    views = _sum(rows, "detail_views")
    paid = _sum(rows, "paid_orders")
    verified = _sum(rows, "verified_orders")
    gmv = _sum(rows, "gmv")
    refunds = _sum(rows, "refunds")
    reviews = _sum(rows, "review_count")
    negatives = _sum(rows, "negative_review_count")
    ratings = [float(row["avg_rating"]) for row in rows if row.get("avg_rating") is not None]
    return {
        "row_count": len(rows),
        "impressions": int(impressions),
        "detail_views": int(views),
        "paid_orders": int(paid),
        "verified_orders": int(verified),
        "gmv": _round(gmv),
        "refunds": int(refunds),
        "review_count": int(reviews),
        "negative_review_count": int(negatives),
        "avg_rating": _round(sum(ratings) / len(ratings), 1) if ratings else None,
        "view_rate": _round(views / impressions * 100) if impressions else None,
        "pay_rate": _round(paid / views * 100) if views else None,
        "verify_rate": _round(verified / paid * 100) if paid else None,
        "refund_rate": _round(refunds / paid * 100) if paid else None,
        "negative_rate": _round(negatives / reviews * 100) if reviews else None,
    }


def _strip_financial(payload: dict[str, Any]) -> dict[str, Any]:
    """无财务权限时从数据结构里删掉 GMV/退款字段（不是文案隐藏）。"""
    cleaned = dict(payload)
    for key in FINANCIAL + FINANCIAL_RATES:
        cleaned.pop(key, None)
    return cleaned


def _change(current: float, previous: float) -> Optional[float]:
    if not previous:
        return None
    return _round((current - previous) / previous * 100, 1)


class StoreReporter:
    def __init__(
        self,
        config: dict[str, Any],
        metric_store: PlatformMetricStore,
        task_store: TaskStore,
        approval_store: Optional[Any] = None,
        config_path: Optional[Path] = None,
        ops_store: Optional[Any] = None,
    ):
        self.config = config
        self.metric_store = metric_store
        self.task_store = task_store
        self.approval_store = approval_store
        self.ops_store = ops_store
        self.config_path = Path(config_path) if config_path else None
        self.store_id = str(config.get("store_id") or "")
        self.store_name = str(config.get("name") or self.store_id)
        if not self.store_id:
            raise ValueError("门店配置缺少 store_id")

    # ---------------- 门店侧看板卡片 ----------------

    def ops_cards(self, start: date, end: date, include_financial: bool = True) -> dict[str, Any]:
        """门店侧两张卡片（时段分布 / 库存预警）。

        默认关闭：只有 `store_config.json` 的 `ops_cards` 显式打开才返回数据，
        避免新维度一上线就把看板堆满。未接入数据源或还没导入时如实回报状态，
        不用空列表冒充"没有异常"。
        """
        flags = self.config.get("ops_cards") or {}
        window_days = min(max((end - start).days + 1, 1), 90)
        cards: dict[str, Any] = {
            "time_slots": {
                "enabled": bool(flags.get("time_slots")),
                "state": "disabled",
                "window_days": window_days,
                "turnover_rate": None,
                "items": [],
            },
            "inventory_alerts": {
                "enabled": bool(flags.get("inventory_alerts")),
                "state": "disabled",
                "latest_stock_date": None,
                "items": [],
            },
        }
        if not (cards["time_slots"]["enabled"] or cards["inventory_alerts"]["enabled"]):
            return cards
        if self.ops_store is None:
            for card in cards.values():
                if card["enabled"]:
                    card["state"] = "not_connected"
            return cards
        summary = self.ops_store.summary(self.store_id, days=window_days, today=end)
        if cards["time_slots"]["enabled"]:
            items = [dict(item) for item in summary.time_slots]
            if not include_financial:
                for item in items:
                    item.pop("revenue", None)
            cards["time_slots"].update({
                "state": "ok" if items else "no_data",
                "turnover_rate": summary.traffic.get("turnover_rate"),
                "items": items,
            })
        if cards["inventory_alerts"]["enabled"]:
            cards["inventory_alerts"].update({
                "state": "ok" if summary.inventory.get("has_data") else "no_data",
                "latest_stock_date": summary.inventory.get("latest_date"),
                "items": summary.alerts,
            })
        return cards

    # ---------------- 取数 ----------------

    def _rows(self, start: date, end: date) -> list[dict[str, Any]]:
        return self.metric_store.read_daily_metrics(self.store_id, start.isoformat(), end.isoformat())

    def _platform_rows(self, rows: list[dict[str, Any]], platform: str) -> list[dict[str, Any]]:
        return [row for row in rows if row["platform"] == platform]

    def _task_stats(self, days: int = 7) -> dict[str, Any]:
        since = (date.today() - timedelta(days=days - 1)).isoformat()
        tasks = [item for item in self.task_store.list_tasks(self.store_id, limit=500)
                 if str(item.get("created_at", ""))[:10] >= since]
        counts: dict[str, int] = {}
        categories: dict[str, int] = {}
        for task in tasks:
            counts[task["status"]] = counts.get(task["status"], 0) + 1
            # 第 4 期：巡店任务（category=inspection）与普通任务在看板可区分
            category = str(task.get("category") or "unknown")
            categories[category] = categories.get(category, 0) + 1
        total = len(tasks)
        verified = counts.get("verified", 0)
        return {
            "days": days,
            "total": total,
            "count_by_status": counts,
            "count_by_category": categories,
            "inspection_count": categories.get("inspection", 0),
            "open_count": sum(counts.get(status, 0) for status in OPEN_STATUSES),
            "overdue_count": counts.get("overdue", 0),
            "completion_rate": _round(verified / total * 100, 1) if total else None,
            "open_tasks": [task for task in self.task_store.list_tasks(self.store_id, status="open", limit=20)],
        }

    def _approval_stats(self) -> dict[str, int]:
        if self.approval_store is None:
            return {}
        return self.approval_store.count_by_status(self.store_id)

    # ---------------- 看板 ----------------

    def dashboard(self, days: int = 7, include_financial: bool = True, today: Optional[date] = None) -> dict[str, Any]:
        if days < 1 or days > 90:
            raise ValueError("看板周期必须在 1 到 90 天之间")
        end = today or date.today()
        end = min(end, max([end, date.today()]))          # 允许回看历史锚点
        start = end - timedelta(days=days - 1)
        previous_start = start - timedelta(days=days)
        previous_end = start - timedelta(days=1)

        rows = self._rows(start, end)
        previous_rows = self._rows(previous_start, previous_end)
        current = aggregate(rows)
        previous = aggregate(previous_rows)

        by_platform = {}
        for key, label in PLATFORMS.items():
            platform_rows = self._platform_rows(rows, key)
            if not platform_rows:
                by_platform[key] = {"label": label, "state": "not_connected", "metrics": None}
                continue
            metrics = aggregate(platform_rows)
            if not include_financial:
                metrics = _strip_financial(metrics)
            by_platform[key] = {"label": label, "state": "ok", "metrics": metrics,
                                "latest_date": max(row["metric_date"] for row in platform_rows)}

        # 每日趋势（跨平台合计；无财务权限时移除金额）
        daily: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = daily.setdefault(row["metric_date"], {"date": row["metric_date"], "impressions": 0,
                                                            "detail_views": 0, "paid_orders": 0,
                                                            "verified_orders": 0, "gmv": 0.0})
            for metric in FUNNEL:
                bucket[metric] += int(row[metric] or 0)
            bucket["gmv"] += float(row["gmv"] or 0)
        trend = []
        for key in sorted(daily):
            item = daily[key]
            item["gmv"] = _round(item["gmv"])
            trend.append(item if include_financial else _strip_financial(item))

        # 套餐榜（近 N 天按支付订单排序，毛利率来自 store_config）
        package_rows = self.metric_store.read_package_metrics(self.store_id, start.isoformat(), end.isoformat())
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in package_rows:
            key = (row["platform"], row["package_id"])
            item = grouped.setdefault(key, {"package_id": row["package_id"], "name": row["package_name"],
                                            "platform": row["platform"], "detail_views": 0,
                                            "paid_orders": 0, "verified_orders": 0, "gmv": 0.0})
            item["name"] = row["package_name"] or item["name"]
            item["detail_views"] += int(row["detail_views"] or 0)
            item["paid_orders"] += int(row["paid_orders"] or 0)
            item["verified_orders"] += int(row["verified_orders"] or 0)
            item["gmv"] += float(row["gmv"] or 0)
        packages = []
        for item in grouped.values():
            item["gmv"] = _round(item["gmv"])
            item["conversion_rate"] = _round(item["paid_orders"] / item["detail_views"] * 100, 2) if item["detail_views"] else None
            item["gross_margin_rate"] = self._margin_rate(item["platform"], item["package_id"])
            packages.append(_strip_financial(item) if not include_financial else item)
        packages.sort(key=lambda entry: entry["paid_orders"], reverse=True)
        packages = packages[:DASHBOARD_TOP_PACKAGES]

        quality = self.metric_store.data_quality(
            self.store_id, days=min(max(days, 30), 365), fresh_days=int((self.config.get("rules") or {}).get("fresh_days", 2)),
            jump_alert_percent=float((self.config.get("rules") or {}).get("jump_alert_percent", 30)),
        )
        task_stats = self._task_stats(days)

        kpis = {
            metric: current[metric] for metric in
            ("impressions", "detail_views", "paid_orders", "verified_orders", "gmv", "refunds",
             "review_count", "negative_review_count", "avg_rating", "view_rate", "pay_rate",
             "verify_rate", "refund_rate", "negative_rate")
        }
        changes = {metric: _change(float(current[metric] or 0), float(previous[metric] or 0))
                   for metric in ("impressions", "detail_views", "paid_orders", "verified_orders", "gmv")}
        if not include_financial:
            kpis = _strip_financial(kpis)
            changes = {key: value for key, value in changes.items() if key not in FINANCIAL}

        issues = list(quality["summary"]["issues"])
        if task_stats["overdue_count"]:
            issues.append(f"有 {task_stats['overdue_count']} 项任务已逾期，请安排处理")
        pending = self._approval_stats().get("pending", 0)
        if pending:
            issues.append(f"有 {pending} 张审批单待决策（改价/赔付/外发等高风险动作）")

        ops_cards = self.ops_cards(start, end, include_financial=include_financial)

        return {
            "store_id": self.store_id,
            "store_name": self.store_name,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
            "previous_window": {"start": previous_start.isoformat(), "end": previous_end.isoformat()},
            "has_data": current["row_count"] > 0,
            "financial_included": include_financial,
            "kpis": kpis,
            "change_percent": changes,
            "previous_kpis": _strip_financial(previous) if not include_financial else previous,
            "platforms": by_platform,
            "trend": trend,
            "packages": packages,
            "tasks": {key: value for key, value in task_stats.items() if key != "open_tasks"},
            "open_tasks": [
                {"task_id": t["task_id"], "title": t["title"], "priority": t["priority"],
                 "due_at": t["due_at"], "status": t["status"]}
                for t in task_stats["open_tasks"][:5]
            ],
            "approvals": self._approval_stats(),
            "data_quality": {"missing_day_count": quality["summary"]["missing_day_count"],
                             "anomaly_count": quality["summary"]["anomaly_count"],
                             "freshness": {k: v["state"] for k, v in quality["freshness"].items()}},
            "issues": issues,
            "sources": self._sources(start, end),
            "time_slots": ops_cards["time_slots"],
            "inventory_alerts": ops_cards["inventory_alerts"],
        }

    def _margin_rate(self, platform: str, package_id: str) -> Optional[float]:
        for item in self.config.get("package_costs") or []:
            if item.get("platform") == platform and str(item.get("package_id")) == str(package_id):
                price = float(item.get("sale_price") or 0)
                cost = float(item.get("variable_cost") or 0)
                if price > 0:
                    return _round((price - cost) / price, 4)
        return None

    def _sources(self, start: date, end: date) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        quality = self.metric_store.data_quality(self.store_id, days=365)
        for batch in quality["batches"][:5]:
            sources.append({"type": "metric_batch", "batch_id": batch["id"], "dataset": batch["dataset"],
                            "label": f"导入批次 #{batch['id']}（{batch['dataset_label']}·{batch['source_name']}）",
                            "date_range": {"start": batch["min_date"], "end": batch["max_date"]}})
        sources.append({"type": "rule", "rule_key": "daily_actions", "version": "1.0", "label": "当日动作规则 v1.0"})
        if self.config_path:
            sources.append({"type": "store_config", "label": f"门店配置 {self.config_path.name}",
                            "path": str(self.config_path)})
        sources.append({"type": "window", "label": f"统计区间 {start.isoformat()} ~ {end.isoformat()}",
                        "date_range": {"start": start.isoformat(), "end": end.isoformat()}})
        return sources

    # ---------------- 周报 / 月报 ----------------

    def weekly_report(self, anchor: Any = "today", include_financial: bool = True) -> dict[str, Any]:
        end = parse_target_date(anchor)
        start = end - timedelta(days=6)
        return self._report("周报", start, end, include_financial, compare_label="上周",
                            period=("上周" if False else None))

    def monthly_report(self, anchor: Any = "today", include_financial: bool = True) -> dict[str, Any]:
        end = parse_target_date(anchor)
        start = end.replace(day=1)
        previous_end = start - timedelta(days=1)
        previous_start = previous_end.replace(day=1)
        return self._report("月报", start, end, include_financial, compare_label="上月",
                            previous=(previous_start, previous_end))

    def _report(self, title: str, start: date, end: date, include_financial: bool,
                compare_label: str, previous: Optional[tuple[date, date]] = None,
                period: Any = None) -> dict[str, Any]:
        rows = self._rows(start, end)
        current = aggregate(rows)
        if previous is None:
            days = (end - start).days + 1
            previous = (start - timedelta(days=days), start - timedelta(days=1))
        previous_rows = self._rows(previous[0], previous[1])
        previous_totals = aggregate(previous_rows)
        quality = self.metric_store.data_quality(self.store_id, days=30)
        tasks = self._task_stats(days=(end - start).days + 1)
        approvals = self._approval_stats()

        lines = [f"{self.store_name} {title}（{start.isoformat()} ~ {end.isoformat()}）"]
        lines.append("\n一、结论摘要")
        if not current["row_count"]:
            lines.append("  本期没有任何已校验的平台数据，无法给出经营结论；请先导出并导入官方报表。")
        else:
            parts = [f"曝光 {current['impressions']}", f"详情访问 {current['detail_views']}",
                     f"支付订单 {current['paid_orders']}", f"核销订单 {current['verified_orders']}"]
            if include_financial:
                parts.append(f"GMV ¥{current['gmv']}")
            lines.append("  本期合计：" + "、".join(parts) + "。")
            rates = [f"访问率 {self._pct(current['view_rate'])}", f"支付转化率 {self._pct(current['pay_rate'])}",
                     f"核销率 {self._pct(current['verify_rate'])}"]
            if include_financial:
                rates.append(f"退款率 {self._pct(current['refund_rate'])}")
            lines.append("  " + "、".join(rates) + "。")
            lines.append(f"  与{compare_label}相比："
                         + self._compare_text(current, previous_totals, include_financial))
            lines.append("  数据完整性：" + ("完整" if not quality["summary"]["missing_day_count"]
                                            else f"缺 {quality['summary']['missing_day_count']} 天"))

        lines.append("\n二、分平台明细")
        for key, label in PLATFORMS.items():
            platform_rows = self._platform_rows(rows, key)
            if not platform_rows:
                lines.append(f"  {label}：未同步，不能分析")
                continue
            metrics = aggregate(platform_rows)
            line = (f"  {label}：曝光 {metrics['impressions']}、访问 {metrics['detail_views']}、"
                    f"支付 {metrics['paid_orders']}、核销 {metrics['verified_orders']}")
            if include_financial:
                line += f"、GMV ¥{metrics['gmv']}"
            lines.append(line)
            lines.append(f"    访问率 {self._pct(metrics['view_rate'])}、支付转化率 {self._pct(metrics['pay_rate'])}、"
                         f"核销率 {self._pct(metrics['verify_rate'])}")

        if rows:
            lines.append("\n三、每日表现")
            daily = {}
            for row in rows:
                bucket = daily.setdefault(row["metric_date"], {"paid": 0, "verified": 0, "gmv": 0.0,
                                                                "impressions": 0})
                bucket["paid"] += int(row["paid_orders"] or 0)
                bucket["verified"] += int(row["verified_orders"] or 0)
                bucket["impressions"] += int(row["impressions"] or 0)
                bucket["gmv"] += float(row["gmv"] or 0)
            for day in sorted(daily):
                item = daily[day]
                text = f"  {day}：曝光 {item['impressions']}、支付 {item['paid']}、核销 {item['verified']}"
                if include_financial:
                    text += f"、GMV ¥{_round(item['gmv'])}"
                lines.append(text)
            best = max(daily.items(), key=lambda pair: (pair[1]["paid"], pair[1]["gmv"]))
            worst = min(daily.items(), key=lambda pair: (pair[1]["paid"], pair[1]["gmv"]))
            lines.append(f"  最好的一天：{best[0]}（支付 {best[1]['paid']} 单）")
            lines.append(f"  最弱的一天：{worst[0]}（支付 {worst[1]['paid']} 单）")

        lines.append("\n四、任务与审批")
        if tasks["total"] == 0:
            lines.append("  本期没有生成任务")
        else:
            labels = "、".join(f"{key} {value}" for key, value in sorted(tasks["count_by_status"].items()))
            lines.append(f"  任务 {tasks['total']} 项（{labels}）；未关闭 {tasks['open_count']} 项，逾期 {tasks['overdue_count']} 项")
            lines.append(f"  验收完成率：{self._pct(tasks['completion_rate'])}")
        if approvals:
            lines.append("  审批单：" + "、".join(f"{key} {value}" for key, value in sorted(approvals.items())))

        lines.append("\n五、数据质量")
        lines.extend([f"  - {issue}" for issue in quality["summary"]["issues"]] or ["  未发现异常"])

        lines.append("\n六、下一步建议")
        suggestions: list[str] = []
        if not current["row_count"]:
            suggestions.append("先把平台后台导出的报表放进 imports/inbox 并执行 platform_sync.py import-dir")
        if quality["summary"]["missing_day_count"]:
            suggestions.append("补齐缺失日期后重新执行 store_cli.py plan，避免漏派任务")
        if quality["summary"]["stale_platforms"]:
            suggestions.append("重新导出最新平台报表，避免按过期数据做决策")
        if quality["summary"]["anomaly_count"]:
            suggestions.append("人工确认跳变指标是真实波动还是导入错误")
        low_conversion = [p for p in self.dashboard(days=min((end - start).days + 1, 30),
                                                    include_financial=include_financial, today=end)["packages"]
                          if (p.get("conversion_rate") or 0) < float((self.config.get("rules") or {})
                                                                     .get("package_conversion_floor", 0.05)) * 100]
        if low_conversion:
            suggestions.append("套餐转化低于阈值的：" + "、".join(p["name"] for p in low_conversion)
                               + "（先核对数据与毛利，再提单变量调整草稿并走审批）")
        if tasks["overdue_count"]:
            suggestions.append(f"处理 {tasks['overdue_count']} 项逾期任务，完成后必须提交证据")
        if approvals.get("pending"):
            suggestions.append(f"决策 {approvals['pending']} 张待审批单（改价/赔付等高风险动作）")
        if not suggestions:
            suggestions.append("数据与执行均正常，按今日待办推进即可")
        lines.extend([f"  {index}. {text}" for index, text in enumerate(suggestions, 1)])

        return {
            "store_id": self.store_id,
            "title": f"{self.store_name}{title}",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "compare_period": {"start": previous[0].isoformat(), "end": previous[1].isoformat(),
                               "label": compare_label},
            "report": "\n".join(lines),
            "summary": {
                "has_data": current["row_count"] > 0,
                "paid_orders": current["paid_orders"],
                "gmv": current["gmv"] if include_financial else None,
                "missing_day_count": quality["summary"]["missing_day_count"],
                "anomaly_count": quality["summary"]["anomaly_count"],
                "open_task_count": tasks["open_count"],
                "overdue_task_count": tasks["overdue_count"],
                "pending_approvals": approvals.get("pending", 0),
            },
            "sources": self._sources(start, end),
        }

    @staticmethod
    def _pct(value: Any) -> str:
        return "缺数" if value is None else f"{value}%"

    @staticmethod
    def _compare_text(current: dict[str, Any], previous: dict[str, Any],
                      include_financial: bool = True) -> str:
        """环比文案；无财务权限时不出现 GMV（与看板/指标摘要的裁剪口径一致）。"""
        metrics = [("impressions", "曝光"), ("detail_views", "访问"),
                   ("paid_orders", "支付"), ("verified_orders", "核销")]
        if include_financial:
            metrics.append(("gmv", "GMV"))
        parts = []
        for metric, label in metrics:
            change = _change(float(current[metric] or 0), float(previous[metric] or 0))
            parts.append(f"{label} {'缺对比' if change is None else f'{change:+.1f}%'}")
        return "，".join(parts)
