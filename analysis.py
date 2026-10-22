#!/usr/bin/env python3
"""周期对比与经营归因（AI 店长"能问明白"第一阶段底层模块）。

只读模块：所有数字都来自 platform_data 已落库的门店日指标，缺数就报缺数，不补齐、不编造。

- ``compare()``：默认单店，按平台锚定"最新完整数据日"（auto），输出 day / week 两组对比；
  门店汇总使用共同锚点，只有共同窗口覆盖完整的平台才参与汇总。
- ``diagnose()``：确定性规则给出"可能原因 + 证据 + 建议动作"，不表述成确定因果；
  存在数据质量阻断时只降低结论置信度，绝不编造原因。

口径复用 ``reporting.aggregate`` 的派生率（view_rate / pay_rate / verify_rate / refund_rate /
negative_rate），财务口径沿用 ``include_financial`` 开关（与 reporting / agent_tools 一致）：
关闭时 GMV / 退款 / 退款率会从所有数据结构里真正删除，而不是只隐藏文案。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from platform_data import METRICS, PLATFORMS, PlatformMetricStore
from reporting import aggregate
from snapshot_builder import load_config, parse_target_date

RULE_VERSION = "1.0"
DIAGNOSIS_RULE_VERSION = "1.0"

# 第 3 期第 5 步：分析类规则的版本号优先读规则库（approved 版本），读不到才回退上面的代码常量。
ANALYSIS_RULE_KEYS = (("period_comparison", RULE_VERSION), ("store_diagnosis", DIAGNOSIS_RULE_VERSION))


def load_rule_versions(rule_store: Any) -> dict[str, str]:
    """解析分析类规则的生效版本号：只采纳 approved 版本，缺失或异常一律回退代码常量。"""
    versions: dict[str, str] = {}
    for key, fallback in ANALYSIS_RULE_KEYS:
        record = None
        if rule_store is not None:
            try:
                record = rule_store.get_rule(key)
            except Exception:
                record = None      # 规则库不可用时不能阻断分析，回退代码常量
        versions[key] = str(record.get("version")) if record and record.get("status") == "approved" else fallback
    return versions

WINDOW_DAYS = {"day": 1, "week": 7}
WINDOW_LABELS = {"day": "日", "week": "近 7 天"}
FINANCIAL_KEYS = ("gmv", "refunds", "refund_rate")
RATE_METRICS = ("view_rate", "pay_rate", "verify_rate", "refund_rate", "negative_rate")
COMPARED_METRICS = tuple(METRICS) + RATE_METRICS

# 归因阈值：计数值看百分比变化，比率看百分点变化
CHANGE_THRESHOLD_PERCENT = 10.0
RATE_THRESHOLD_POINTS = 1.0
RATING_THRESHOLD = 0.2

METRIC_LABELS = {
    "impressions": "曝光量",
    "detail_views": "访问量",
    "paid_orders": "支付订单",
    "verified_orders": "核销订单",
    "gmv": "成交金额",
    "refunds": "退款单数",
    "review_count": "评价数",
    "negative_review_count": "差评数",
    "avg_rating": "评分",
    "view_rate": "访问转化率",
    "pay_rate": "支付转化率",
    "verify_rate": "核销率",
    "refund_rate": "退款率",
    "negative_rate": "差评率",
}

WEEKEND_DAYS = (5, 6)  # 周六、周日


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _date_range(start: date, end: date) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


def _ranges(anchor_value: Optional[str], days: int) -> dict[str, Optional[dict[str, str]]]:
    """当前窗口 = 锚点往前 days 天；上期窗口 = 再往前 days 天。"""
    if not anchor_value:
        return {"current": None, "previous": None}
    anchor = date.fromisoformat(anchor_value)
    return {
        "current": _date_range(anchor - timedelta(days=days - 1), anchor),
        "previous": _date_range(anchor - timedelta(days=2 * days - 1), anchor - timedelta(days=days)),
    }


def _dates(date_range: Optional[dict[str, str]]) -> list[str]:
    if not date_range:
        return []
    start = date.fromisoformat(date_range["start"])
    end = date.fromisoformat(date_range["end"])
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def _coverage(date_range: Optional[dict[str, str]], present: set[str]) -> dict[str, Any]:
    expected = _dates(date_range)
    missing = [value for value in expected if value not in present]
    return {
        "expected_days": len(expected),
        "actual_days": len(expected) - len(missing),
        "complete": bool(expected) and not missing,
        "missing_dates": missing,
    }


def _weekend_days(date_range: Optional[dict[str, str]]) -> int:
    return sum(1 for value in _dates(date_range) if date.fromisoformat(value).weekday() in WEEKEND_DAYS)


def _rows_in(rows: list[dict[str, Any]], date_range: Optional[dict[str, str]]) -> list[dict[str, Any]]:
    values = set(_dates(date_range))
    return [row for row in rows if row["metric_date"] in values]


def _change_maps(current: Optional[dict[str, Any]],
                 previous: Optional[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """变化率（上期为 0 时为 None）与绝对值变化；row_count 不参与对比。"""
    change_percent: dict[str, Any] = {}
    absolute_change: dict[str, Any] = {}
    if not current or not previous:
        return change_percent, absolute_change
    for metric in COMPARED_METRICS:
        cur = current.get(metric)
        prev = previous.get(metric)
        if cur is None or prev is None:
            continue
        absolute_change[metric] = _round(float(cur) - float(prev))
        change_percent[metric] = None if not prev else _round((float(cur) - float(prev)) / float(prev) * 100, 1)
    return change_percent, absolute_change


def _strip_financial(value: Any) -> Any:
    """递归删除财务字段（不是隐藏文案）。"""
    if isinstance(value, dict):
        return {key: _strip_financial(item) for key, item in value.items() if key not in FINANCIAL_KEYS}
    if isinstance(value, list):
        return [_strip_financial(item) for item in value]
    return value


def _confidence(kind: str, value: Optional[float]) -> str:
    magnitude = abs(float(value)) if value is not None else 0.0
    if kind == "points":
        if magnitude >= 3 * RATE_THRESHOLD_POINTS:
            return "high"
        return "medium" if magnitude >= 1.5 * RATE_THRESHOLD_POINTS else "low"
    if magnitude >= 3 * CHANGE_THRESHOLD_PERCENT:
        return "high"
    return "medium" if magnitude >= 1.5 * CHANGE_THRESHOLD_PERCENT else "low"


def _evidence(metric: str, metrics: Optional[dict[str, Any]], previous_metrics: Optional[dict[str, Any]],
              changes: dict[str, Any], absolute: dict[str, Any],
              window: dict[str, Any]) -> dict[str, Any]:
    return {
        "metric": metric,
        "label": METRIC_LABELS.get(metric, metric),
        "current": (metrics or {}).get(metric),
        "previous": (previous_metrics or {}).get(metric),
        "change_percent": changes.get(metric),
        "absolute_change": absolute.get(metric),
        "current_range": window.get("current"),
        "previous_range": window.get("previous"),
    }


class StoreAnalysis:
    """单店只读的周期对比与经营归因。

    ``config`` 可以是门店配置 dict，也可以是 store_config.json 路径（内部走 load_config）。
    """

    def __init__(self, config: Any, metric_store: PlatformMetricStore, config_path: Optional[Path] = None,
                 rule_store: Any = None):
        if isinstance(config, (str, Path)):
            path = Path(config)
            config = load_config(path)
            config_path = config_path or path
        self.config: dict[str, Any] = dict(config or {})
        self.metric_store = metric_store
        self.config_path = Path(config_path) if config_path else None
        self.store_id = str(self.config.get("store_id") or "")
        self.store_name = str(self.config.get("name") or self.store_id)
        if not self.store_id:
            raise ValueError("门店配置缺少 store_id")
        rules = self.config.get("rules") or {}
        self.fresh_days = int(rules.get("fresh_days", 2))
        self.jump_alert_percent = float(rules.get("jump_alert_percent", 30.0))
        self.rule_versions = load_rule_versions(rule_store)

    # ---------------- 周期对比 ----------------

    def compare(self, anchor: Any = "auto", windows: Any = ("day", "week"), platform: Any = None,
                metric: Any = None, include_financial: bool = True) -> dict[str, Any]:
        """周期对比：本期 / 上期指标、变化率、覆盖率、新鲜度与数据质量阻断。"""
        window_keys = self._normalize_windows(windows)
        platform_keys = self._normalize_platforms(platform)
        metric_keys = self._normalize_metrics(metric)

        mode = "auto" if str(anchor).strip().lower() == "auto" else "explicit"
        explicit = None if mode == "auto" else parse_target_date(anchor)

        quality = self.metric_store.data_quality(
            self.store_id, days=self._quality_days(window_keys),
            fresh_days=self.fresh_days, jump_alert_percent=self.jump_alert_percent)
        freshness = quality["freshness"]

        span_days = 2 * max(WINDOW_DAYS[key] for key in window_keys) - 1
        anchors: dict[str, Optional[str]] = {}
        rows_by_platform: dict[str, list[dict[str, Any]]] = {}
        for key in platform_keys:
            latest = freshness.get(key, {}).get("max_date")
            # 显式锚点只对已接入的平台生效；没有任何数据的平台一律按"未接入"处理
            anchor_value = (explicit.isoformat() if explicit is not None else latest) if latest else None
            anchors[key] = anchor_value
            if not anchor_value:
                rows_by_platform[key] = []
                continue
            start = date.fromisoformat(anchor_value) - timedelta(days=span_days)
            rows_by_platform[key] = [
                row for row in self.metric_store.read_daily_metrics(
                    self.store_id, start.isoformat(), anchor_value)
                if row["platform"] == key
            ]

        available = [value for value in anchors.values() if value]
        common_anchor = explicit.isoformat() if explicit is not None else (max(available) if available else None)

        window_results: dict[str, dict[str, Any]] = {}
        for key in window_keys:
            window_results[key] = self._build_window(
                key, platform_keys, rows_by_platform, anchors, freshness, quality, common_anchor)

        has_metrics = any(
            window["platforms"][key]["metrics"] for window in window_results.values() for key in platform_keys)
        sources = self._sources(window_results, quality, has_metrics)
        for window in window_results.values():
            window["sources"] = sources

        result: dict[str, Any] = {
            "store_id": self.store_id,
            "store_name": self.store_name,
            "anchor": {
                "requested": "auto" if mode == "auto" else str(anchor),
                "mode": mode,
                "common": common_anchor,
                "platforms": anchors,
                "basis": "auto 按各平台最新数据日锚定；门店汇总使用共同锚点（各平台最新日的最大值）",
            },
            "include_financial": bool(include_financial),
            "parameters": {"windows": list(window_keys), "platform": platform, "metric": metric},
            "windows": window_results,
            "blocked": [item for key in window_keys for item in window_results[key]["blocked"]],
            "freshness": freshness,
            "data_quality": quality,
            "sources": sources,
        }
        if metric_keys:
            result = self._filter_metrics(result, metric_keys)
        if not include_financial:
            result = _strip_financial(result)
        return result

    def _build_window(self, window_key: str, platform_keys: tuple[str, ...],
                      rows_by_platform: dict[str, list[dict[str, Any]]],
                      anchors: dict[str, Optional[str]], freshness: dict[str, Any],
                      quality: dict[str, Any], common_anchor: Optional[str]) -> dict[str, Any]:
        days = WINDOW_DAYS[window_key]
        common_ranges = _ranges(common_anchor, days)

        platforms: dict[str, Any] = {}
        blocked: list[dict[str, Any]] = []
        for key in platform_keys:
            entry = self._platform_window(window_key, key, rows_by_platform.get(key, []),
                                          anchors.get(key), freshness.get(key) or {})
            platforms[key] = entry
            for reason in entry["blocked"]:
                blocked.append({"window": window_key, "platform": key, "platform_label": entry["label"],
                                "scope": "platform", **reason})

        # 门店汇总口径：只有共同锚点窗口覆盖完整（当前与上期都不缺日）的平台参与
        participants: list[str] = []
        summary_coverage: dict[str, Any] = {}
        summary_missing: dict[str, dict[str, list[str]]] = {"current": {}, "previous": {}}
        for key in platform_keys:
            present = {row["metric_date"] for row in rows_by_platform.get(key, [])}
            if not common_ranges["current"] or not anchors.get(key):
                summary_coverage[key] = {"current": None, "previous": None}
                summary_missing["current"][key] = []
                summary_missing["previous"][key] = []
                continue
            current_coverage = _coverage(common_ranges["current"], present)
            previous_coverage = _coverage(common_ranges["previous"], present)
            summary_coverage[key] = {
                "current": {k: v for k, v in current_coverage.items() if k != "missing_dates"},
                "previous": {k: v for k, v in previous_coverage.items() if k != "missing_dates"},
            }
            summary_missing["current"][key] = current_coverage["missing_dates"]
            summary_missing["previous"][key] = previous_coverage["missing_dates"]
            if current_coverage["complete"] and previous_coverage["complete"]:
                participants.append(key)
            elif current_coverage["missing_dates"] or previous_coverage["missing_dates"]:
                missing = current_coverage["missing_dates"] + previous_coverage["missing_dates"]
                blocked.append({
                    "window": window_key, "platform": key, "platform_label": platforms[key]["label"],
                    "scope": "store_summary", "code": "missing_days",
                    "label": f"{platforms[key]['label']}在门店汇总的共同窗口缺 {len(missing)} 天数据，不参与汇总",
                    "missing_dates": missing,
                })

        if participants:
            metrics = aggregate([row for key in participants
                                 for row in _rows_in(rows_by_platform[key], common_ranges["current"])])
            previous_metrics = aggregate([row for key in participants
                                          for row in _rows_in(rows_by_platform[key], common_ranges["previous"])])
            change_percent, absolute_change = _change_maps(metrics, previous_metrics)
        else:
            metrics = previous_metrics = None
            change_percent, absolute_change = {}, {}
            blocked.append({
                "window": window_key, "platform": "store", "platform_label": self.store_name,
                "scope": "store_summary", "code": "no_complete_platform",
                "label": "没有覆盖完整的平台参与门店汇总，本期只返回分平台结果",
            })

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in blocked:
            identity = (str(item["platform"]), str(item["code"]))
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(item)

        return {
            "window": window_key,
            "label": WINDOW_LABELS.get(window_key, window_key),
            "days": days,
            "current": common_ranges["current"],
            "previous": common_ranges["previous"],
            "metrics": metrics,
            "previous_metrics": previous_metrics,
            "change_percent": change_percent,
            "absolute_change": absolute_change,
            "coverage": {
                "current": {key: platforms[key]["coverage"]["current"] for key in platform_keys},
                "previous": {key: platforms[key]["coverage"]["previous"] for key in platform_keys},
                "summary_anchor": common_anchor,
                "summary": summary_coverage,
                "summary_platforms": participants,
                "summary_complete": bool(participants),
            },
            "missing_dates": {
                "current": {key: platforms[key]["missing_dates"]["current"] for key in platform_keys},
                "previous": {key: platforms[key]["missing_dates"]["previous"] for key in platform_keys},
            },
            "missing_dates_summary": summary_missing,
            "freshness": freshness,
            "data_quality": {**quality["summary"], "evaluated_at": quality["evaluated_at"],
                             "checked": quality["window"]},
            "platforms": platforms,
            "blocked": deduped,
        }

    def _platform_window(self, window_key: str, platform: str, rows: list[dict[str, Any]],
                         anchor_value: Optional[str], fresh: dict[str, Any]) -> dict[str, Any]:
        label = PLATFORMS.get(platform, platform)
        days = WINDOW_DAYS[window_key]
        entry: dict[str, Any] = {
            "platform": platform,
            "label": label,
            "state": "ok" if anchor_value else "not_connected",
            "anchor": anchor_value,
            "current": None,
            "previous": None,
            "metrics": None,
            "previous_metrics": None,
            "change_percent": {},
            "absolute_change": {},
            "coverage": {"current": None, "previous": None},
            "missing_dates": {"current": [], "previous": []},
            "freshness": fresh or None,
            "blocked": [],
        }
        if not anchor_value:
            entry["blocked"] = [{"code": "not_connected", "label": f"{label}未接入任何数据"}]
            return entry

        ranges = _ranges(anchor_value, days)
        entry["current"] = ranges["current"]
        entry["previous"] = ranges["previous"]
        present = {row["metric_date"] for row in rows}
        for name in ("current", "previous"):
            coverage = _coverage(ranges[name], present)
            entry["coverage"][name] = {k: v for k, v in coverage.items() if k != "missing_dates"}
            entry["missing_dates"][name] = coverage["missing_dates"]
            period_rows = _rows_in(rows, ranges[name])
            if name == "current" and period_rows:
                entry["metrics"] = aggregate(period_rows)
            if name == "previous" and period_rows:
                entry["previous_metrics"] = aggregate(period_rows)
        entry["change_percent"], entry["absolute_change"] = _change_maps(entry["metrics"], entry["previous_metrics"])

        reasons: list[dict[str, Any]] = []
        if fresh.get("state") == "stale":
            reasons.append({"code": "stale",
                            "label": f"{label}数据已 {fresh.get('age_days')} 天未更新（阈值 {self.fresh_days} 天）"})
        for name, chinese in (("current", "本期"), ("previous", "上期")):
            if entry["missing_dates"][name]:
                reasons.append({"code": "missing_days",
                                "label": f"{label}{chinese}窗口缺 {len(entry['missing_dates'][name])} 天数据",
                                "missing_dates": entry["missing_dates"][name]})
        if entry["metrics"] is None:
            reasons.append({"code": "no_data", "label": f"{label}本期窗口没有任何数据"})
        if entry["previous_metrics"] is None:
            reasons.append({"code": "no_previous", "label": f"{label}上期窗口没有任何数据，无法计算变化"})
        entry["blocked"] = reasons
        return entry

    def _sources(self, window_results: dict[str, Any], quality: dict[str, Any],
                 has_metrics: bool) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for key, window in window_results.items():
            if not window["current"] or not window["previous"]:
                continue
            sources.append({
                "type": "window", "window": key,
                "label": (f"{WINDOW_LABELS.get(key, key)}对比窗口 "
                          f"{window['current']['start']} ~ {window['current']['end']} vs "
                          f"{window['previous']['start']} ~ {window['previous']['end']}"),
                "date_range": {"current": window["current"], "previous": window["previous"]},
            })
        for batch in quality["batches"]:
            sources.append({
                "type": "metric_batch", "batch_id": batch["id"], "dataset": batch["dataset"],
                "dataset_label": batch["dataset_label"], "source_name": batch["source_name"],
                "label": f"导入批次 #{batch['id']}（{batch['dataset_label']}·{batch['source_name']}）",
                "date_range": {"start": batch["min_date"], "end": batch["max_date"]},
            })
        period_version = self.rule_versions["period_comparison"]
        sources.append({"type": "rule", "rule_key": "period_comparison", "version": period_version,
                        "label": f"周期对比规则 v{period_version}"})
        if self.config_path:
            sources.append({"type": "store_config", "label": f"门店配置 {self.config_path.name}",
                            "path": str(self.config_path)})
        if not has_metrics:
            sources.append({"type": "no_data",
                            "label": "该门店在对比窗口内没有可用平台指标：请先导入美团/抖音报表后重试"})
        return sources

    # ---------------- 经营归因 ----------------

    def diagnose(self, comparison: Optional[dict[str, Any]] = None, **kwargs: Any) -> dict[str, Any]:
        """确定性归因：返回可能原因、置信度、证据与建议动作（不表述成确定因果）。"""
        if comparison is None:
            comparison = self.compare(**kwargs)
        window_keys = list(comparison.get("windows") or {})
        if not window_keys:
            raise ValueError("comparison 里没有任何对比窗口")
        primary = "day" if "day" in window_keys else window_keys[0]
        window = comparison["windows"][primary]
        include_financial = bool(comparison.get("include_financial", True))
        blocked = list(window.get("blocked") or [])
        metrics = window.get("metrics")
        previous_metrics = window.get("previous_metrics")
        changes = window.get("change_percent") or {}
        absolute = window.get("absolute_change") or {}

        causes: list[dict[str, Any]] = []
        caveats: list[str] = []
        if blocked:
            causes.append(self._data_quality_cause(blocked))
            caveats.append("存在数据质量阻断：门店汇总只使用覆盖完整的平台，结论置信度已下调")
        if not include_financial:
            caveats.append("未包含财务口径（GMV/退款/退款率），退款相关结论不参与判断")

        if metrics is None or previous_metrics is None:
            causes.append({
                "code": "insufficient_evidence",
                "label": "证据不足：对比窗口内没有可比的完整数据，无法给出可能原因",
                "confidence": "low",
                "evidence": [{"type": "missing", "window": primary,
                              "current_range": window.get("current"), "previous_range": window.get("previous")}],
                "actions": ["先补齐两个对比窗口的平台数据", "确认美团/抖音授权与导出是否正常"],
            })
            return self._diagnosis(comparison, primary, causes, blocked, caveats, window)

        def cause(code: str, label: str, metric: str, kind: str, value: Optional[float],
                  actions: list[str], extra_metrics: tuple[str, ...] = ()) -> dict[str, Any]:
            evidence = [_evidence(metric, metrics, previous_metrics, changes, absolute, window)]
            for extra in extra_metrics:
                evidence.append(_evidence(extra, metrics, previous_metrics, changes, absolute, window))
            return {"code": code, "label": label, "confidence": _confidence(kind, value),
                    "evidence": evidence, "actions": actions}

        fired = False

        # 2) 日期类型差异（周末 / 工作日结构不同）
        current_weekend = _weekend_days(window.get("current"))
        previous_weekend = _weekend_days(window.get("previous"))
        if current_weekend != previous_weekend:
            causes.append({
                "code": "date_type",
                "label": "本期与上期的日期类型不同（周末/工作日天数不等），变化可能来自日历结构差异",
                "confidence": "low",
                "evidence": [{"type": "calendar", "current_weekend_days": current_weekend,
                              "previous_weekend_days": previous_weekend,
                              "current_range": window.get("current"), "previous_range": window.get("previous")}],
                "actions": ["用同类型日期（同为周末或同为工作日）再对比一次，确认差异是否仍然存在"],
            })

        # 3) 流量 / 曝光
        impressions_change = changes.get("impressions")
        if impressions_change is not None and impressions_change <= -CHANGE_THRESHOLD_PERCENT:
            causes.append(cause("traffic", "曝光量下降，可能是进店与成交减少的上游原因", "impressions",
                                "percent", impressions_change,
                                ["检查美团/抖音的曝光入口（列表、推荐、搜索）流量变化",
                                 "核对是否有活动结束或投放暂停，并确认是否与曝光下降同期"]))
            fired = True

        # 4)(5)(6) 访问转化 / 支付转化 / 履约核销
        for code, metric, label, actions in (
            ("visit_conversion", "view_rate", "访问转化率下降，进店人群的意向可能变弱",
             ["检查主图、套餐标题与价格带是否有改动", "对比同期竞品曝光与到店权益变化"]),
            ("pay_conversion", "pay_rate", "支付转化率下降，成交环节可能出了问题",
             ["检查套餐价格、库存与下单链路是否异常", "核对是否出现差评前置或评价分数下滑影响下单"]),
            ("fulfillment", "verify_rate", "核销率下降，履约环节可能受影响",
             ["检查预约排期、到店体验与核销提醒是否到位", "核对是否有退款或改期集中发生"]),
        ):
            delta = absolute.get(metric)
            if delta is not None and delta <= -RATE_THRESHOLD_POINTS:
                causes.append(cause(code, label, metric, "points", delta, list(actions)))
                fired = True

        # 7) 退款
        if include_financial:
            refund_points = absolute.get("refund_rate")
            refund_change = changes.get("refunds")
            trigger_points = refund_points is not None and refund_points >= RATE_THRESHOLD_POINTS
            trigger_change = refund_change is not None and refund_change >= CHANGE_THRESHOLD_PERCENT
            if trigger_points or trigger_change:
                metric = "refund_rate" if trigger_points else "refunds"
                value = refund_points if trigger_points else refund_change
                kind = "points" if trigger_points else "percent"
                causes.append(cause("refund", "退款上升，履约质量或商品描述可能与顾客预期不符", metric, kind, value,
                                    ["逐单核对退款原因，区分平台判责与顾客主动退款",
                                     "核对套餐内容、到店时长与描述是否与页面一致"],
                                    extra_metrics=("refunds",) if metric != "refunds" else ()))
                fired = True

        # 8) 评价口碑
        negative_points = absolute.get("negative_rate")
        rating_delta = absolute.get("avg_rating")
        trigger_negative = negative_points is not None and negative_points >= RATE_THRESHOLD_POINTS
        trigger_rating = rating_delta is not None and rating_delta <= -RATING_THRESHOLD
        if trigger_negative or trigger_rating:
            metric = "negative_rate" if trigger_negative else "avg_rating"
            value = negative_points if trigger_negative else rating_delta
            causes.append(cause("reputation", "口碑下滑（差评增多或评分下降），可能持续影响曝光与转化", metric,
                                "points", value,
                                ["按差评 SLA 在规定时间内回复并回访顾客",
                                 "把高频差评原因流转成门店整改任务并跟踪关闭"],
                                extra_metrics=("negative_rate", "avg_rating")))
            fired = True

        # 9) 增长 / 稳定
        if not fired:
            movers = [(m, v) for m, v in changes.items() if v is not None and v >= CHANGE_THRESHOLD_PERCENT]
            rate_movers = [(m, absolute[m]) for m in RATE_METRICS
                           if absolute.get(m) is not None and absolute[m] >= RATE_THRESHOLD_POINTS]
            if movers or rate_movers:
                movers.sort(key=lambda item: item[1], reverse=True)
                rate_movers.sort(key=lambda item: item[1], reverse=True)
                metric, value = (rate_movers or movers)[0]
                kind = "points" if metric in RATE_METRICS else "percent"
                causes.append(cause("growth", "指标向好，增长可能来自近期动作或流量红利", metric, kind, value,
                                    ["复盘本期有效的动作并沉淀为标准流程",
                                     "确认增长是否可持续，避免只靠单次活动拉量"]))
            else:
                causes.append({
                    "code": "stable",
                    "label": "经营指标基本稳定，暂未发现明显变化（可能原因：波动在正常范围内）",
                    "confidence": "medium",
                    "evidence": [_evidence(m, metrics, previous_metrics, changes, absolute, window)
                                 for m in ("impressions", "detail_views", "paid_orders", "verified_orders")
                                 if (metrics or {}).get(m) is not None],
                    "actions": ["保持当前经营动作，继续观察下一周期", "如门店体感异常，请补充人工备注再做判断"],
                })

        return self._diagnosis(comparison, primary, causes, blocked, caveats, window)

    def _data_quality_cause(self, blocked: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "code": "data_quality",
            "label": "数据质量存在缺口，以下结论只能作为参考，不构成确定原因",
            "confidence": "low",
            "evidence": [{"type": "blocked", "reason": item.get("code"), "platform": item.get("platform"),
                          "platform_label": item.get("platform_label"), "window": item.get("window"),
                          "scope": item.get("scope"), "detail": item.get("label")} for item in blocked],
            "actions": ["先补齐缺失日期的平台报表并重新导入", "确认美团/抖音后台是否仍在正常导出",
                        "数据恢复完整后再做归因判断"],
        }

    def _diagnosis(self, comparison: dict[str, Any], window_key: str, causes: list[dict[str, Any]],
                   blocked: list[dict[str, Any]], caveats: list[str],
                   window: dict[str, Any]) -> dict[str, Any]:
        if blocked:
            causes = [{**item, "confidence": "medium" if item.get("confidence") == "high" else item.get("confidence")}
                      for item in causes]
        sources = list(comparison.get("sources") or [])
        diagnosis_version = self.rule_versions["store_diagnosis"]
        sources.append({"type": "rule", "rule_key": "store_diagnosis", "version": diagnosis_version,
                        "label": f"经营归因规则 v{diagnosis_version}"})
        return {
            "store_id": comparison.get("store_id") or self.store_id,
            "anchor": comparison.get("anchor"),
            "window": window_key,
            "rule_version": diagnosis_version,
            "causes": causes,
            "blocked": blocked,
            "caveats": caveats,
            "data_quality": window.get("data_quality"),
            "sources": sources,
            "disclaimer": "以下为基于数据的可能原因与证据，不代表确定因果；请结合门店实际经营动作核实。",
        }

    # ---------------- 参数与输出整理 ----------------

    @staticmethod
    def _normalize_windows(windows: Any) -> tuple[str, ...]:
        if isinstance(windows, str):
            windows = (windows,)
        keys = [str(item).strip().lower() for item in windows]
        if not keys:
            raise ValueError("windows 不能为空")
        for key in keys:
            if key not in WINDOW_DAYS:
                raise ValueError(f"windows 只支持 {'/'.join(WINDOW_DAYS)}，收到：{key}")
        return tuple(dict.fromkeys(keys))

    @staticmethod
    def _normalize_platforms(platform: Any) -> tuple[str, ...]:
        if platform is None:
            return tuple(PLATFORMS)
        labels = {label: key for key, label in PLATFORMS.items()}
        items = [platform] if isinstance(platform, str) else list(platform)
        keys: list[str] = []
        for item in items:
            raw = str(item).strip()
            key = raw if raw in PLATFORMS else labels.get(raw, raw.lower())
            if key not in PLATFORMS:
                raise ValueError(f"platform 只支持 {'/'.join(PLATFORMS)} 或中文名，收到：{item}")
            keys.append(key)
        return tuple(dict.fromkeys(keys))

    @staticmethod
    def _normalize_metrics(metric: Any) -> tuple[str, ...]:
        if metric is None:
            return ()
        items = [metric] if isinstance(metric, str) else list(metric)
        keys = [str(item).strip() for item in items]
        for key in keys:
            if key not in COMPARED_METRICS:
                raise ValueError(f"metric 只支持 {'/'.join(COMPARED_METRICS)}，收到：{key}")
        return tuple(dict.fromkeys(keys))

    @staticmethod
    def _quality_days(window_keys: tuple[str, ...]) -> int:
        return max(30, 2 * max(WINDOW_DAYS[key] for key in window_keys))

    @staticmethod
    def _filter_metrics(result: dict[str, Any], metric_keys: tuple[str, ...]) -> dict[str, Any]:
        allowed = set(metric_keys)

        def keep(entry: dict[str, Any]) -> None:
            for name in ("metrics", "previous_metrics", "change_percent", "absolute_change"):
                payload = entry.get(name)
                if isinstance(payload, dict):
                    entry[name] = {key: value for key, value in payload.items() if key in allowed}

        for window in result["windows"].values():
            keep(window)
            for entry in window["platforms"].values():
                keep(entry)
        return result
