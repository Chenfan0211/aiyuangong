#!/usr/bin/env python3
"""Agent 只读工具集：把指标库、任务库、配置包装成"可被问答调用"的稳定接口。

设计原则：
  - 全部**只读**，不含任何写操作；改价/赔付/外发仍然只能走人工审批；
  - 每个回答都带**结构化依据**（批次、规则、配置来源），而不是自由文本；
  - 角色可见性在这里统一收口：店员只能看到指派给自己的任务。

写操作（接受任务、提交证据、验收）由 `TaskStore` 承担，HTTP 层再按角色放行。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from analysis import StoreAnalysis
from auth import has_permission
from platform_data import PLATFORMS, PlatformMetricStore
from snapshot_builder import parse_target_date
from task_store import OPEN_STATUSES, TaskStore

DECISION_STATUSES = ("verified", "rejected")
REPORT_STATUS_LABELS = {
    "proposed": "待审批",
    "ready": "可执行",
    "accepted": "进行中",
    "done": "待验收",
    "verified": "已验收",
    "rejected": "已驳回",
    "overdue": "已逾期",
    "canceled": "已取消",
}


class PermissionDenied(PermissionError):
    """越权访问（跨角色、跨负责人、跨门店）。"""


class StoreTools:
    def __init__(
        self,
        config: dict[str, Any],
        metric_store: PlatformMetricStore,
        task_store: TaskStore,
        config_path: Optional[Path] = None,
        ops_store: Optional[Any] = None,
        competitor_store: Optional[Any] = None,
        rule_store: Optional[Any] = None,
        knowledge_store: Optional[Any] = None,
    ):
        self.config = config
        self.metric_store = metric_store
        self.task_store = task_store
        # 第 11 期：门店侧（开台/客流、时段、库存）与竞品数据的只读入口。
        # 用 Optional 保持既有调用方不变；未接入时相关工具会给出明确的"未接入"而不是编数。
        self.ops_store = ops_store
        self.competitor_store = competitor_store
        # 第 6 期：知识库（未接入时相关工具明确说明，而不是编造）
        self.knowledge_store = knowledge_store
        self.config_path = Path(config_path) if config_path else None
        # 第 3 期第 5 步：分析类规则版本优先读规则库（approved），读不到回退代码常量
        self.analysis = StoreAnalysis(self.config, self.metric_store, config_path=self.config_path,
                                      rule_store=rule_store)
        self.store_id = str(config.get("store_id") or "")
        if not self.store_id:
            raise ValueError("门店配置缺少 store_id")

    def search_knowledge(self, user: dict[str, Any], query: str = "",
                         base_id: Optional[str] = None, limit: int = 5) -> dict[str, Any]:
        """检索门店知识库（只读，第 6 期）：命中给可回查引用，没命中就明说。"""
        if not self._can(user, "knowledge:read"):
            raise PermissionDenied("缺少权限：knowledge:read")
        if self.knowledge_store is None:
            return {"has_match": False, "reason": "未接入知识库", "count": 0, "citations": []}
        return self.knowledge_store.search(self.store_id, query, base_id=base_id, limit=limit)

    # ---------------- 角色可见性 ----------------

    @staticmethod
    def _can(user: dict[str, Any], permission: str) -> bool:
        """权限判定统一走 auth 的策略表，不在业务代码里散落角色名。"""
        return has_permission(str(user.get("role") or ""), permission)

    def _is_store_wide(self, user: dict[str, Any]) -> bool:
        return self._can(user, "tasks:read:store")

    def can_read_financial(self, user: dict[str, Any]) -> bool:
        return self._can(user, "metrics:read:financial")

    def can_view_task(self, user: dict[str, Any], task: dict[str, Any]) -> bool:
        if task.get("store_id") != self.store_id:
            return False
        return self._is_store_wide(user) or task.get("assignee_id") == user.get("name")

    def can_submit_evidence(self, user: dict[str, Any], task: dict[str, Any]) -> bool:
        return self.can_view_task(user, task) and self._can(user, "tasks:evidence:submit")

    def can_change_status(self, user: dict[str, Any], task: dict[str, Any], to_status: str) -> bool:
        if not self.can_view_task(user, task):
            return False
        if to_status in DECISION_STATUSES:
            return self._can(user, "tasks:verify")     # 验收/驳回仅管理者
        if not self._can(user, "tasks:accept"):
            return False
        # 接受/完成由"任务负责人"执行；管理者可代办了结（仍会写审计）
        return task.get("assignee_id") == user.get("name") or self._can(user, "tasks:verify")

    # ---------------- 只读查询 ----------------

    def data_status(self) -> dict[str, Any]:
        """各平台数据新鲜度（含门店与配置来源）。"""
        return self.metric_store.status(self.store_id, fresh_days=self.fresh_days)

    @property
    def fresh_days(self) -> int:
        return int((self.config.get("rules") or {}).get("fresh_days", 2))

    @property
    def jump_alert_percent(self) -> float:
        return float((self.config.get("rules") or {}).get("jump_alert_percent", 30))

    FINANCIAL_METRICS = ("gmv", "refunds", "refund_rate")

    def metric_summary(
        self,
        days: int = 7,
        platform: Optional[str] = None,
        include_financial: bool = True,
    ) -> dict[str, Any]:
        """统一口径的经营指标摘要；platform 可为 meituan / douyin / None(全部)。

        include_financial=False 时**从数据结构里删除** GMV/退款类字段（不是只在文案里隐藏），
        因此无财务权限的角色即使直接调接口也拿不到这些数字。
        """
        if platform is not None and platform not in PLATFORMS:
            raise ValueError(f"未知平台：{platform}（可用：{'、'.join(PLATFORMS)}）")
        summary = self.metric_store.period_summary(self.store_id, days)
        if platform and summary.get("platforms"):
            summary = dict(summary)
            summary["platforms"] = {
                key: value for key, value in summary["platforms"].items() if key == platform
            }
            summary["has_data"] = bool(summary["platforms"])
        if not include_financial and summary.get("platforms"):
            summary = dict(summary)
            summary["platforms"] = {
                key: {
                    **value,
                    "metrics": {k: v for k, v in value["metrics"].items() if k not in self.FINANCIAL_METRICS},
                    "change_percent": {k: v for k, v in value["change_percent"].items() if k not in self.FINANCIAL_METRICS},
                }
                for key, value in summary["platforms"].items()
            }
            summary["financial_included"] = False
        else:
            summary["financial_included"] = True
        return summary

    # ---------------- 门店侧与竞品（第 11 期新增的只读工具源） ----------------

    def _require_ops(self) -> Any:
        if self.ops_store is None:
            raise ValueError(
                "未接入门店侧数据源，暂时无法回答开台/客流、时段与库存问题（需配置 ops_store）"
            )
        return self.ops_store

    def _require_competitor(self) -> Any:
        if self.competitor_store is None:
            raise ValueError(
                "未接入竞品数据源，暂时无法回答竞品问题（需配置 competitor_store）"
            )
        return self.competitor_store

    def store_traffic(self, days: int = 7) -> dict[str, Any]:
        """开台 / 客流 / 翻台率（第 1 期数据）。缺数时如实回报，不补零。"""
        summary = self._require_ops().summary(self.store_id, days=days)
        return {"window_days": days, "traffic": summary.traffic}

    def time_slots(self, days: int = 7) -> dict[str, Any]:
        """时段分布（各时段订单占比、客流、营收）。"""
        summary = self._require_ops().summary(self.store_id, days=days)
        return {"window_days": days, "time_slots": summary.time_slots}

    def inventory_alerts(self, limit: int = 50) -> dict[str, Any]:
        """未处理的库存预警（按剩余量 ÷ 安全库存分级）。"""
        ops = self._require_ops()
        alerts = ops.read_alerts(self.store_id, status="open",
                                 limit=max(1, min(int(limit), 200)))
        return {"alert_count": len(alerts), "alerts": alerts}

    def competitor_matches(self) -> dict[str, Any]:
        """竞品匹配台账：已确认 / 待确认 / 已驳回，以及尚未匹配的竞品套餐。"""
        store = self._require_competitor()
        matches = store.list_matches(self.store_id)
        return {
            "confirmed": [item for item in matches if item["status"] == "confirmed"],
            "pending": [item for item in matches if item["status"] == "pending"],
            "rejected": [item for item in matches if item["status"] == "rejected"],
            "unmatched_competitor_packages": store.unmatched_competitor_packages(self.store_id),
            "note": "只有 confirmed 的匹配参与价格与套餐对比。",
        }

    def competitor_compare(self, days: Optional[int] = None) -> dict[str, Any]:
        """竞品四维对比：价格 / 套餐 / 活动 / 商圈变化。"""
        store = self._require_competitor()
        config = dict(self.config)
        if days:
            rules = dict(config.get("rules") or {})
            rules["competitor_window_days"] = int(days)
            config["rules"] = rules
        return store.compare(self.store_id, config)

    def data_quality(self, days: int = 30) -> dict[str, Any]:
        return self.metric_store.data_quality(
            self.store_id,
            days=days,
            fresh_days=self.fresh_days,
            jump_alert_percent=self.jump_alert_percent,
        )

    def period_comparison(
        self,
        user: dict[str, Any],
        anchor: Any = "auto",
        windows: Any = ("day", "week"),
        platform: Optional[str] = None,
        metric: Optional[str] = None,
    ) -> dict[str, Any]:
        """双口径周期对比；财务字段按角色权限真实裁剪。"""
        if not self._can(user, "metrics:read"):
            raise PermissionDenied("无权查看经营指标")
        return self.analysis.compare(
            anchor=anchor, windows=windows, platform=platform, metric=metric,
            include_financial=self.can_read_financial(user),
        )

    def diagnose_store_change(
        self,
        user: dict[str, Any],
        comparison: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """在周期对比结果上做确定性经营归因。"""
        if not self._can(user, "metrics:read"):
            raise PermissionDenied("无权查看经营指标")
        if comparison is None:
            comparison = self.period_comparison(user, **kwargs)
        return self.analysis.diagnose(comparison)

    def tasks_for(
        self,
        user: dict[str, Any],
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按角色过滤任务：店长及以上看全店，店员只看指派给自己的。"""
        tasks = self.task_store.list_tasks(self.store_id, status=status, limit=limit)
        if self._is_store_wide(user):
            return tasks
        return [task for task in tasks if task["assignee_id"] == user.get("name")]

    def task_detail(self, task_id: str, user: dict[str, Any]) -> dict[str, Any]:
        task = self.task_store.get_task(task_id)
        if not self.can_view_task(user, task):
            raise PermissionDenied(f"无权查看任务 {task_id}")
        task["events"] = self.task_store.events(task_id)
        task["evidence"] = self.task_store.evidence(task_id)
        return task

    def task_completion(self, days: int = 7) -> dict[str, Any]:
        """近 N 天创建任务的执行情况（按状态计数 + 验收完成率）。"""
        if days < 1 or days > 365:
            raise ValueError("统计周期必须在 1 到 365 天之间")
        since = (date.today() - timedelta(days=days - 1)).isoformat()
        tasks = [item for item in self.task_store.list_tasks(self.store_id, limit=500)
                 if str(item.get("created_at", ""))[:10] >= since]
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        total = len(tasks)
        verified = counts.get("verified", 0)
        return {
            "store_id": self.store_id,
            "days": days,
            "since": since,
            "total": total,
            "count_by_status": counts,
            "completion_rate": round(verified / total * 100, 1) if total else None,
            "open_count": sum(counts.get(status, 0) for status in OPEN_STATUSES),
        }

    def daily_report(self, target_date: date | str | None = None) -> dict[str, Any]:
        """生成店长日报（纯文本 + 结构化依据），供对话、接口与推送复用。"""
        day = parse_target_date(target_date)
        quality = self.data_quality(days=30)
        summary = self.metric_summary(days=7)
        completion = self.task_completion(days=7)
        open_tasks = self.task_store.list_tasks(self.store_id, status="open", limit=20)
        config = self.config

        lines = [f"{config.get('name') or self.store_id} 经营日报（{day.isoformat()}）"]

        lines.append("\n一、数据状态")
        for key, item in quality["freshness"].items():
            if item["state"] == "not_connected":
                lines.append(f"  {item['label']}：未同步")
            elif item["state"] == "fresh":
                lines.append(f"  {item['label']}：已同步至 {item['max_date']}（最新）")
            else:
                lines.append(f"  {item['label']}：数据停留在 {item['max_date']}，已 {item['age_days']} 天未更新")
        missing = [f"{PLATFORMS[key]} {len(item['missing_dates'])} 天" for key, item in quality["coverage"].items()
                   if item["missing_dates"]]
        lines.append("  近 30 天缺数：" + ("、".join(missing) if missing else "无"))

        lines.append("\n二、经营指标（近 7 天）")
        if not summary.get("has_data"):
            lines.append("  没有任何已校验的平台数据，无法给出经营结论（请先导入官方报表）")
        else:
            lines.append(f"  统计区间：{summary['start_date']} ~ {summary['end_date']}")
            for key, item in summary["platforms"].items():
                metrics = item["metrics"]
                lines.append(
                    f"  {item['label']}：曝光 {self._num(metrics['impressions'])}、详情访问 {self._num(metrics['detail_views'])}、"
                    f"支付 {self._num(metrics['paid_orders'])}、核销 {self._num(metrics['verified_orders'])}、"
                    f"GMV ¥{self._num(metrics['gmv'])}"
                )
                lines.append(
                    f"    访问率 {self._rate(metrics['view_rate'])}、支付转化率 {self._rate(metrics['pay_rate'])}、"
                    f"核销率 {self._rate(metrics['verify_rate'])}、退款率 {self._rate(metrics['refund_rate'])}"
                )
            missing_platforms = [label for key, label in PLATFORMS.items() if key not in summary["platforms"]]
            if missing_platforms:
                lines.append("  未同步：" + "、".join(missing_platforms) + "（不能分析）")
        lines.append(f"  较前一周期：" + self._changes(summary))

        lines.append("\n三、任务执行（近 7 天创建）")
        if completion["total"] == 0:
            lines.append("  近 7 天没有生成任务（先导入数据后执行 store_cli.py plan）")
        else:
            labels = "、".join(f"{REPORT_STATUS_LABELS.get(k, k)} {v}" for k, v in sorted(completion["count_by_status"].items()))
            lines.append(f"  合计 {completion['total']} 项：{labels}")
            lines.append(f"  验收完成率：{self._rate(completion['completion_rate'])}；未关闭：{completion['open_count']} 项")
        if open_tasks:
            lines.append("  最优先待办：")
            for task in open_tasks[:3]:
                approval = "（需审批）" if task["approval_required"] else ""
                lines.append(f"    - [{task['priority']}] {task['title']}{approval} · 截止 {task['due_at'][:16].replace('T', ' ')}")

        lines.append("\n四、数据质量问题")
        issues = quality["summary"]["issues"]
        lines.extend([f"  - {issue}" for issue in issues] or ["  未发现异常"])

        lines.append("\n五、下一步建议")
        suggestions: list[str] = []
        if not quality["summary"]["has_data"]:
            suggestions.append("先把平台后台导出的报表放进 imports/inbox 并执行 platform_sync.py import-dir")
        if quality["summary"]["missing_day_count"]:
            suggestions.append("补齐缺失日期数据后重新执行 store_cli.py plan，避免漏派任务")
        if quality["summary"]["stale_platforms"]:
            suggestions.append("重新导出最新平台报表并导入，避免按过期数据做决策")
        if quality["summary"]["anomaly_count"]:
            suggestions.append("先人工确认跳变指标属于真实波动还是导入错误，再决定是否调整套餐")
        if open_tasks:
            urgent = [task for task in open_tasks if task["priority"] in ("urgent", "high")]
            if urgent:
                suggestions.append(f"优先处理 {len(urgent)} 项紧急/高优先级任务，并按要求提交证据")
        if not suggestions:
            suggestions.append("数据与任务均正常，按今日待办执行即可")
        lines.extend([f"  {index}. {item}" for index, item in enumerate(suggestions, 1)])

        return {
            "store_id": self.store_id,
            "date": day.isoformat(),
            "report": "\n".join(lines),
            "sources": self.metric_sources(summary) + self.rule_sources(),
            "summary": {
                "has_data": summary.get("has_data", False),
                "missing_day_count": quality["summary"]["missing_day_count"],
                "anomaly_count": quality["summary"]["anomaly_count"],
                "open_task_count": len(open_tasks),
                "completion_rate": completion["completion_rate"],
            },
        }

    # ---------------- 依据（结构化） ----------------

    @staticmethod
    def _num(value: Any) -> str:
        if value is None:
            return "缺数"
        if isinstance(value, float):
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{value:,}"

    @staticmethod
    def _rate(value: Any) -> str:
        return "缺数" if value is None else f"{value}%"

    def _changes(self, summary: dict[str, Any]) -> str:
        if not summary.get("has_data"):
            return "数据不足，不能判断升降趋势"
        parts: list[str] = []
        for key, item in summary["platforms"].items():
            for metric, label in (("impressions", "曝光"), ("detail_views", "访问"), ("paid_orders", "支付"), ("verified_orders", "核销")):
                value = (item.get("change_percent") or {}).get(metric)
                if value is not None:
                    parts.append(f"{item['label']}{label} {value:+.1f}%")
        return "，".join(parts) if parts else "前一周期数据不足，暂不能判断升降"

    def metric_sources(self, summary: dict[str, Any]) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for key, item in (summary.get("platforms") or {}).items():
            for name in item.get("source_names") or []:
                sources.append({
                    "type": "metric_export",
                    "platform": key,
                    "label": f"{item['label']}官方导出《{name}》",
                    "date_range": {"start": summary.get("start_date"), "end": summary.get("end_date")},
                })
        if not sources:
            sources.append({"type": "metric_store", "label": "统一平台指标库（无数据）", "date_range": {}})
        return sources

    def batch_sources(self, limit: int = 3) -> list[dict[str, Any]]:
        quality = self.data_quality(days=30)
        return [
            {
                "type": "metric_batch",
                "batch_id": item["id"],
                "dataset": item["dataset"],
                "label": f"导入批次 #{item['id']}（{item['dataset_label']}·{item['source_name']}）",
                "checksum_prefix": item["checksum_prefix"],
                "date_range": {"start": item["min_date"], "end": item["max_date"]},
            }
            for item in quality["batches"][: max(1, limit)]
        ]

    def rule_sources(self) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = [
            {"type": "rule", "rule_key": "daily_actions", "version": "1.0", "label": "当日动作规则 v1.0"},
        ]
        if self.config_path:
            sources.append({
                "type": "store_config",
                "label": f"门店配置 {self.config_path.name}",
                "path": str(self.config_path),
            })
        return sources
