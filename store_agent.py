#!/usr/bin/env python3
"""门店运营 Agent 的对话与权限内核。

职责边界：
  - 意图路由用**显式规则表**（INTENT_RULES），关键词是数据而不是 if/elif 分支，便于测试与加词；
  - 回答来自**只读工具集**（agent_tools.StoreTools）：指标、数据质量、任务、日报；
  - 角色可见性在工具集统一收口：店长看全店，店员只看指派给自己的任务；
  - 不保存平台密钥，不执行改价、赔付、消息、警告、奖惩或发布。

两种运行模式：
  - 生产模式：`StoreOperationsAgent(config=..., tools=..., llm_responder=...)`，任务来自任务库；
  - 演示兼容模式：`StoreOperationsAgent.from_file("sample_input.json", ...)`，任务来自手填快照，
    仅用于保留最初的演示与既有测试。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from urllib import request

from agent_tools import PermissionDenied, StoreTools
from auth import has_permission
from db import Database
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan


DEMO_USERS = {
    "manager-001": {
        "user_id": "manager-001",
        "name": "武汉狮子座店店长",
        "role": "manager",
        "role_label": "店长",
    },
    "staff-001": {
        "user_id": "staff-001",
        "name": "员工小李",
        "role": "employee",
        "role_label": "店员",
    },
}

HIGH_RISK_TERMS = ("改价", "降价", "赔付", "赔偿", "警告", "处罚", "罚款", "奖惩", "发布", "上架")
SENSITIVE_TERMS = ("毛利", "利润", "工资", "薪资", "老板报表", "全店员工", "奖惩记录")



@dataclass(frozen=True)
class IntentRule:
    """一条意图规则：命中任一关键词即视为该意图。规则表按顺序匹配，越具体越靠前。"""

    intent: str
    keywords: tuple[str, ...]
    note: str = ""


INTENT_RULES: tuple[IntentRule, ...] = (
    IntentRule("daily_report", ("日报", "经营报告", "今日报告", "今天总结"), "生成店长日报"),
    IntentRule("data_gap", ("哪天缺数", "缺哪几天", "哪天没数据", "哪些天没数据", "缺数日期"), "缺日明细"),
    IntentRule("data_quality", ("数据齐不齐", "数据质量", "数据全不全", "缺不缺数", "数据完不完整"), "数据质量体检"),
    IntentRule("task_progress", ("任务完成情况", "完成情况", "完成率", "完成了多少"), "任务执行统计"),
    IntentRule("explain_tasks", ("为什么", "原因", "依据"), "解释任务生成依据"),
    IntentRule("execution_help", ("怎么做", "流程", "步骤", "证据", "完成了", "做完了"), "执行 SOP"),
    IntentRule("reputation_help", ("差评", "评价", "口碑"), "评价与差评处理"),
    IntentRule("package_help", ("套餐", "转化", "团购"), "套餐诊断"),
    IntentRule("platform_metrics", (
        "最近数据", "经营数据", "平台数据", "数据分析", "美团", "抖音",
        "曝光", "详情访问", "访问量", "订单", "核销", "gmv", "GMV", "流量", "转化率", "数据",
    ), "平台经营指标"),
    IntentRule("list_tasks", ("今天", "今日", "任务", "做什么", "待办"), "今日任务清单"),
)

DAYS_SLOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("30", ("近30天", "最近30天", "近一个月", "最近一个月", "本月", "一个月")),
    ("7", ("近7天", "最近7天", "近一周", "最近一周", "本周", "一周", "7天")),
    ("1", ("今天", "今日", "当日", "昨天", "昨日")),
)

PLATFORM_SLOTS = (("meituan", ("美团", "点评")), ("douyin", ("抖音",)))
PLATFORM_LABELS = {"meituan": "美团", "douyin": "抖音"}

METRIC_SLOTS = (
    ("impressions", ("曝光", "展现")),
    ("detail_views", ("详情访问", "访问量", "访问")),
    ("paid_orders", ("支付订单", "支付", "订单")),
    ("verified_orders", ("核销",)),
    ("gmv", ("gmv", "GMV", "营业额", "销售额", "流水")),
    ("refunds", ("退款",)),
    ("review_count", ("评价数", "评价", "评分")),
)


class OpenAICompatibleResponder:
    """可选的大模型兜底；只接收服务端裁剪后的最小上下文。"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 20):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> Optional["OpenAICompatibleResponder"]:
        api_key = os.getenv("STORE_AGENT_LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            base_url=os.getenv("STORE_AGENT_LLM_BASE_URL")
            or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            model=os.getenv("STORE_AGENT_LLM_MODEL")
            or os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        )

    def answer(
        self,
        message: str,
        user: dict[str, str],
        visible_tasks: list[dict[str, Any]],
        history: list[dict[str, Any]],
    ) -> str:
        task_context = [
            {
                "task_id": task["task_id"],
                "title": task["title"],
                "reason": task["reason"],
                "due_at": task["due_at"],
                "requires_approval": task["requires_approval"],
            }
            for task in visible_tasks
        ]
        safe_history = [
            {"role": item.get("message_role"), "content": item.get("content", "")[:1000]}
            for item in history[-8:]
            if item.get("message_role") in ("user", "assistant")
        ]
        system = (
            "你是武汉狮子座店的门店运营 Agent。只依据提供的可见任务和门店 SOP 回答，信息不足就明确说缺数。"
            "不得透露其他员工、其他门店、工资、密钥、完整手机号等敏感信息。"
            "不得声称已执行任何操作。改价、赔付、公开回复、警告、奖惩、发布和上架必须提示需要管理者审批。"
            f"当前用户：{user['name']}，角色：{user['role_label']}。"
            f"当前可见任务：{json.dumps(task_context, ensure_ascii=False)}"
        )
        payload = json.dumps(
            {
                "model": self.model,
                "temperature": 0.2,
                "messages": [{"role": "system", "content": system}, *safe_history, {"role": "user", "content": message}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return str(body["choices"][0]["message"]["content"]).strip()


class AgentSessionStore:
    """最小会话存储；生产环境应替换为带租户策略和加密的数据库。"""

    def __init__(self, db_target, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        if self.db.dialect == "postgresql":
            with self._connection() as conn:
                self.db.apply_schema(conn)
            return
        with self._connection() as conn:
            conn.execute(
                """
                create table if not exists agent_messages (
                  tenant_id text not null default '',
                  id integer primary key autoincrement,
                  session_id text not null,
                  user_id text not null,
                  message_role text not null check (message_role in ('user','assistant')),
                  content text not null,
                  intent text,
                  created_at text not null
                )
                """
            )
            conn.execute(
                "create index if not exists idx_agent_session on agent_messages(session_id, user_id, id)"
            )
            self.db.ensure_tenant_column(conn, "agent_messages")

    def _connection(self):
        """统一走 Database：提交/回滚后必定关闭连接（详见 db.py）。"""
        return self.db.connection()

    def append(self, session_id: str, user_id: str, role: str, content: str, intent: str = "") -> None:
        with self._connection() as conn:
            conn.execute(
                "insert into agent_messages(session_id,user_id,message_role,content,intent,created_at) values(?,?,?,?,?,?)",
                (session_id, user_id, role, content, intent, datetime.now().isoformat(timespec="seconds")),
            )

    def history(self, session_id: str, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "select message_role,content,intent,created_at from agent_messages "
                "where session_id=? and user_id=? order by id desc limit ?",
                (session_id, user_id, min(max(limit, 1), 100)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]


class StoreOperationsAgent:
    def __init__(
        self,
        snapshot: Optional[dict[str, Any]] = None,
        llm_responder: Optional[OpenAICompatibleResponder] = None,
        metric_store: Optional[PlatformMetricStore] = None,
        tools: Optional[StoreTools] = None,
        config: Optional[dict[str, Any]] = None,
        tool_agent: Optional[Any] = None,
    ):
        if snapshot is None:
            if not config:
                raise ValueError("必须提供 snapshot（演示模式）或 config（生产模式）")
            snapshot = {
                "store": {
                    "tenant_id": config.get("tenant_id"),
                    "store_id": config.get("store_id"),
                    "name": config.get("name"),
                },
                "date": date.today().isoformat(),
            }
            self.plan: Optional[dict[str, Any]] = None
        else:
            self.plan = generate_daily_plan(snapshot)
        self.snapshot = snapshot
        self.llm_responder = llm_responder
        self.metric_store = metric_store
        self.tools = tools
        self.config = config or {}
        self.tool_agent = tool_agent

    @classmethod
    def from_file(
        cls,
        path: Path,
        llm_responder: Optional[OpenAICompatibleResponder] = None,
        metric_store: Optional[PlatformMetricStore] = None,
    ) -> "StoreOperationsAgent":
        """演示兼容模式：从 sample_input.json 载入手填快照。"""
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), llm_responder, metric_store)

    @classmethod
    def from_tools(
        cls,
        config: dict[str, Any],
        tools: StoreTools,
        llm_responder: Optional[OpenAICompatibleResponder] = None,
        tool_agent: Optional[Any] = None,
    ) -> "StoreOperationsAgent":
        """生产模式：任务来自任务库，指标来自平台指标库。

        `tool_agent`（批次 13）配置后，规则识别不了的开放问题会走"模型 + 只读工具"，
        由模型自己决定查什么数据；未配置时退回一次性的纯文本问答。
        """
        return cls(None, llm_responder, tools.metric_store, tools=tools, config=config,
                   tool_agent=tool_agent)

    # ---------------- 身份与可见性 ----------------

    def user(self, user_id: str) -> dict[str, str]:
        if user_id not in DEMO_USERS:
            raise ValueError("未知演示用户")
        return DEMO_USERS[user_id]

    @staticmethod
    def _can(user: dict[str, Any], permission: str) -> bool:
        return has_permission(str(user.get("role") or ""), permission)

    @staticmethod
    def _task_view(task: dict[str, Any]) -> dict[str, Any]:
        """把任务库记录投影成规则引擎/回答层使用的形状。"""
        return {
            "task_id": task["task_id"],
            "category": task["category"],
            "title": task["title"],
            "assignee": task["assignee_id"],
            "priority": task["priority"],
            "due_at": task["due_at"],
            "target": task.get("target") or {},
            "evidence_required": task.get("evidence_required") or [],
            "reason": task["reason"],
            "requires_approval": bool(task["approval_required"]),
            "status": task.get("status"),
        }

    def visible_tasks(self, user_id: str, user: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """`user` 由认证层传入；不传时回退到演示用户表（仅演示模式）。"""
        user = user or self.user(user_id)
        if self.tools is not None:
            return [self._task_view(task) for task in self.tools.tasks_for(user, limit=50)]
        if self.plan is None:
            return []
        if self._can(user, "tasks:read:store"):
            return list(self.plan["tasks"])
        return [task for task in self.plan["tasks"] if task["assignee"] == user["name"]]

    @staticmethod
    def _task_lines(tasks: list[dict[str, Any]]) -> str:
        if not tasks:
            return "目前没有分配给你的待办。你仍可询问标准操作流程或向店长反馈现场问题。"
        lines = []
        for index, task in enumerate(tasks, 1):
            approval = "，需审批后执行" if task["requires_approval"] else ""
            status = f"，当前状态 {task['status']}" if task.get("status") else ""
            lines.append(
                f"{index}. {task['title']}（{task['priority']}，截止 {task['due_at'][11:16]}{approval}{status}）"
            )
        return "\n".join(lines)

    @staticmethod
    def _metric_value(value: Any, suffix: str = "") -> str:
        if value is None:
            return "缺数"
        if isinstance(value, float):
            rendered = f"{value:,.2f}".rstrip("0").rstrip(".")
        else:
            rendered = f"{value:,}"
        return rendered + suffix

    # ---------------- 意图路由 ----------------

    @staticmethod
    def parse_slots(text: str) -> dict[str, Any]:
        """解析槽位：时间范围、平台、指标。"""
        days = 7
        for value, keywords in DAYS_SLOTS:
            if any(keyword in text for keyword in keywords):
                days = int(value)
                break
        platform = None
        for key, keywords in PLATFORM_SLOTS:
            if any(keyword in text for keyword in keywords):
                platform = key
                break
        metric = None
        for key, keywords in METRIC_SLOTS:
            if any(keyword in text for keyword in keywords):
                metric = key
                break
        return {"days": days, "platform": platform, "metric": metric}

    @staticmethod
    def match_intent(text: str) -> Optional[str]:
        for rule in INTENT_RULES:
            if any(keyword in text for keyword in rule.keywords):
                return rule.intent
        return None

    def _default_sources(self) -> list[Any]:
        if self.tools is not None:
            return self.tools.rule_sources()
        return ["武汉狮子座店演示经营快照", "当日动作规则 v1"]

    # ---------------- 回答渲染 ----------------

    def data_status(self) -> dict[str, Any]:
        store_id = self.snapshot["store"]["store_id"]
        if self.tools is not None:
            return self.tools.data_status()
        if not self.metric_store:
            return {
                "store_id": store_id,
                "has_data": False,
                "platforms": {
                    "meituan": {"label": "美团", "state": "not_connected", "max_date": None, "age_days": None},
                    "douyin": {"label": "抖音", "state": "not_connected", "max_date": None, "age_days": None},
                },
            }
        return self.metric_store.status(store_id)

    def _render_platform_summary(
        self,
        summary: dict[str, Any],
        user: dict[str, str],
        days: int,
        metric: Optional[str] = None,
    ) -> str:
        lines = [f"武汉狮子座店最近 {days} 天平台数据（{summary['start_date']} 至 {summary['end_date']}）："]
        metric_labels = {
            "impressions": "曝光", "detail_views": "详情访问", "paid_orders": "支付订单",
            "verified_orders": "核销订单", "gmv": "GMV", "refunds": "退款", "review_count": "新增评价",
        }
        for platform in ("meituan", "douyin"):
            item = summary["platforms"].get(platform)
            if not item:
                lines.append(f"\n{PLATFORM_LABELS[platform]}：未同步，不能分析。")
                continue
            metrics = item["metrics"]
            lines.append(f"\n{item['label']}（最新 {item['latest_date']}，覆盖 {item['row_count']} 天）")
            if metric and metric in metrics and (metric != "gmv" or self._can(user, "metrics:read:financial")):
                value = metrics[metric]
                rendered = f"¥{self._metric_value(value)}" if metric == "gmv" else self._metric_value(value)
                lines.append(f"{metric_labels[metric]}：{rendered}（近 {days} 天合计）")
                continue
            lines.append(
                "曝光 {impressions}；详情访问 {views}；支付订单 {paid}；核销订单 {verified}。".format(
                    impressions=self._metric_value(metrics["impressions"]),
                    views=self._metric_value(metrics["detail_views"]),
                    paid=self._metric_value(metrics["paid_orders"]),
                    verified=self._metric_value(metrics["verified_orders"]),
                )
            )
            lines.append(
                "访问率 {view_rate}；支付转化率 {pay_rate}；核销率 {verify_rate}。".format(
                    view_rate=self._metric_value(metrics["view_rate"], "%"),
                    pay_rate=self._metric_value(metrics["pay_rate"], "%"),
                    verify_rate=self._metric_value(metrics["verify_rate"], "%"),
                )
            )
            if self._can(user, "metrics:read:financial"):
                lines.append(
                    f"核销 GMV ¥{self._metric_value(metrics['gmv'])}；退款 {self._metric_value(metrics['refunds'])} 单；"
                    f"新增评价 {self._metric_value(metrics['review_count'])} 条，差评 {self._metric_value(metrics['negative_review_count'])} 条。"
                )
            comparable = [
                ("曝光", item["change_percent"].get("impressions")),
                ("详情访问", item["change_percent"].get("detail_views")),
                ("支付订单", item["change_percent"].get("paid_orders")),
                ("核销订单", item["change_percent"].get("verified_orders")),
            ]
            available = [(label, value) for label, value in comparable if value is not None]
            if available:
                lines.append("较前一周期：" + "，".join(f"{label}{value:+.1f}%" for label, value in available) + "。")
            else:
                lines.append("前一周期数据不足，暂不能判断升降趋势。")

        lines.append("\n判断原则：先看数据是否完整，再定位曝光、访问、支付、核销中的最大掉点；数据不足时不生成改价或投放结论。")
        return "\n".join(lines)

    def _answer_platform_metrics(self, user: dict[str, str], slots: dict[str, Any]) -> tuple[str, list[Any]]:
        days = int(slots["days"])
        metric = slots["metric"]
        if self.tools is not None:
            summary = self.tools.metric_summary(
                days=days, include_financial=self._can(user, "metrics:read:financial"))
            sources = self.tools.metric_sources(summary)
            if not summary["has_data"]:
                return (
                    "武汉狮子座店目前没有已校验的美团或抖音数据，我不会用演示数字代替。"
                    "请先导入官方报表；导入后我会按曝光→访问→支付→核销漏斗分析。",
                    sources,
                )
            return self._render_platform_summary(summary, user, days, metric), sources

        if not self.metric_store:
            return (
                "武汉狮子座店尚未配置平台指标库，因此我不能给出真实经营分析。"
                "请先导入美团/抖音官方报表，或完成商家授权后的只读 API 接入。",
                ["平台数据状态：未连接"],
            )
        summary = self.metric_store.period_summary(self.snapshot["store"]["store_id"], days)
        if not summary["has_data"]:
            return (
                "武汉狮子座店目前没有已校验的美团或抖音数据，我不会用演示数字代替。"
                "请先导入官方报表；导入后我会按曝光→访问→支付→核销漏斗分析。",
                ["统一平台指标库：无数据"],
            )
        return self._render_platform_summary(summary, user, days, metric), [
            f"{item['label']}官方导出：{name}"
            for item in summary["platforms"].values()
            for name in item["source_names"]
        ] or ["统一平台指标库"]

    def _answer_daily_report(self, user: dict[str, str]) -> tuple[str, list[Any]]:
        if self.tools is None:
            return ("日报需要在生产模式下生成（当前是演示模式）。", self._default_sources())
        if not self._can(user, "report:read"):
            return (
                "日报包含全店经营数据，仅店长及以上可查看。你可以问我自己的任务、操作流程或完成证据要求。",
                [{"type": "policy", "label": "角色权限策略 v1：日报仅店长及以上"}],
            )
        report = self.tools.daily_report()
        return report["report"], report["sources"]

    def _answer_data_quality(self, user: dict[str, str], days: int = 30) -> tuple[str, list[Any]]:
        if self.tools is None:
            return (
                "数据质量体检需要在生产模式下运行（当前是演示模式）。",
                self._default_sources(),
            )
        if not self._can(user, "quality:read"):
            return (
                "数据质量报告涉及全店数据，仅店长及以上可查看。你可以问我自己的任务和流程。",
                [{"type": "policy", "label": "角色权限策略 v1：数据质量仅店长及以上"}],
            )
        quality = self.tools.data_quality(days=days)
        summary = quality["summary"]
        if not summary["has_data"]:
            return (
                f"近 {days} 天没有任何已落库的平台数据，谈不上完整。"
                "请先把平台后台导出的报表放进 imports/inbox 并执行 platform_sync.py import-dir。",
                [{"type": "metric_store", "label": "统一平台指标库（无数据）"}],
            )
        lines = [f"数据完整性（近 {days} 天，窗口 {quality['window']['start_date']} ~ {quality['window']['end_date']}）"]
        for key, item in quality["coverage"].items():
            label = quality["freshness"][key]["label"]
            if item["missing_dates"]:
                lines.append(f"- {label}：缺 {len(item['missing_dates'])} 天 → {'、'.join(item['missing_dates'][:10])}")
            elif item["note"]:
                lines.append(f"- {label}：{item['note']}")
            else:
                lines.append(f"- {label}：完整（{item['actual_days']}/{item['expected_days']} 天）")
        if quality["anomalies"]:
            lines.append(f"\n另有 {len(quality['anomalies'])} 处日环比跳变（阈值 ±{quality['parameters']['jump_alert_percent']}%），")
            for item in quality["anomalies"][:5]:
                lines.append(
                    f"- {item['platform_label']} {item['metric']}：{item['previous_date']}→{item['date']} "
                    f"{item['change_percent']:+.1f}%"
                )
        lines.append("\n结论：缺日说明平台数据没导入或平台未产出，这类缺口会让当天的诊断与派单失真。")
        return "\n".join(lines), self.tools.batch_sources(limit=3) + self.tools.rule_sources()

    def _answer_data_gap(self, user: dict[str, str]) -> tuple[str, list[Any]]:
        if self.tools is None:
            return ("缺日明细需要在生产模式下查询（当前是演示模式）。", self._default_sources())
        if not self._can(user, "quality:read"):
            return ("缺日明细属于全店数据，仅店长及以上可查看。", [{"type": "policy", "label": "角色权限策略 v1"}])
        quality = self.tools.data_quality(days=30)
        gaps = {key: item["missing_dates"] for key, item in quality["coverage"].items() if item["missing_dates"]}
        if not gaps:
            return ("近 30 天没有发现缺日：各平台数据从首条日期到最新日期都是连续的。", self.tools.rule_sources())
        lines = ["近 30 天缺数明细："]
        for key, dates in gaps.items():
            label = quality["freshness"][key]["label"]
            lines.append(f"- {label}：{len(dates)} 天 → {'、'.join(dates[:15])}")
        lines.append("补数方式：从平台后台重新导出包含这些日期的报表，放进 imports/inbox 后执行 import-dir。")
        return "\n".join(lines), self.tools.batch_sources(limit=3) + self.tools.rule_sources()

    def _answer_task_progress(self, user: dict[str, str]) -> tuple[str, list[Any]]:
        if self.tools is None:
            tasks = self.visible_tasks(user["user_id"], user)
            return (f"当前可见任务 {len(tasks)} 项。\n{self._task_lines(tasks)}", self._default_sources())
        if self._can(user, "tasks:read:store"):
            stats = self.tools.task_completion(days=7)
            if stats["total"] == 0:
                return ("近 7 天还没有生成任务。先导入数据，再执行 store_cli.py plan --date today。",
                        self.tools.rule_sources())
            labels = "、".join(
                f"{label} {stats['count_by_status'].get(key, 0)}"
                for key, label in (("proposed", "待审批"), ("ready", "可执行"), ("accepted", "进行中"),
                                   ("done", "待验收"), ("verified", "已验收"), ("overdue", "已逾期"))
                if stats["count_by_status"].get(key)
            )
            return (
                f"近 7 天共生成 {stats['total']} 项任务：{labels}。"
                f"验收完成率 {self._metric_value(stats['completion_rate'], '%')}，未关闭 {stats['open_count']} 项。",
                self.tools.rule_sources(),
            )
        tasks = self.visible_tasks(user["user_id"], user)
        open_tasks = [task for task in tasks if task["status"] not in ("verified", "canceled", "rejected")]
        return (
            f"你的任务：共 {len(tasks)} 项，未关闭 {len(open_tasks)} 项。\n{self._task_lines(open_tasks)}",
            self.tools.rule_sources(),
        )

    def _answer_list_tasks(self, user: dict[str, str]) -> tuple[str, list[Any]]:
        visible = self.visible_tasks(user["user_id"], user)
        return f"{user['role_label']}当前可见任务如下：\n{self._task_lines(visible)}", self._default_sources()

    def _answer_reputation_help(self, user: dict[str, str]) -> tuple[str, list[Any]]:
        visible = [task for task in self.visible_tasks(user["user_id"], user) if task["category"] == "reputation"]
        answer = (
            f"你当前可见的评价类任务：\n{self._task_lines(visible)}\n\n"
            "处理顺序：先核对订单与事实，再分类问题，形成回复/补救草稿，上传记录后由负责人复核。"
            "不得诱导好评，也不要在聊天中粘贴顾客完整手机号。"
        )
        return answer, self._default_sources()

    def _answer_package_help(self, user: dict[str, str]) -> tuple[str, list[Any]]:
        visible = [task for task in self.visible_tasks(user["user_id"], user) if task["category"] == "package"]
        if self._can(user, "pricing:draft"):
            answer = (
                f"当前套餐类任务：\n{self._task_lines(visible)}\n\n"
                "建议先确认样本量和漏斗掉点，再做单变量实验；价格、库存、门店范围和上架都需审批。"
            )
        else:
            answer = "店员可以反馈顾客对套餐名称、时长和使用限制的真实疑问；价格、毛利和上架策略由店长处理。"
        return answer, self._default_sources()

    def _answer_execution_help(self) -> tuple[str, list[Any]]:
        answer = (
            "执行任务请按四步：①确认任务目标和截止时间；②按要求操作，不自行扩大范围；"
            "③上传脱敏截图、订单号或现场记录；④等待店长复核。"
            "仅回复“做完了”不会被记为完成，必须有任务要求的证据。"
        )
        return answer, ["门店任务执行 SOP v1"]

    def _answer_explain_tasks(self, user: dict[str, str]) -> tuple[str, list[Any]]:
        visible = self.visible_tasks(user["user_id"], user)
        if visible:
            answer = "这些任务的生成依据是：\n" + "\n".join(
                f"- {task['title']}：{task['reason']}" for task in visible
            )
        else:
            answer = "当前没有分配给你的任务，因此没有可解释的任务依据。"
        return answer, self._default_sources()

    # ---------------- 主入口 ----------------

    def reply(
        self,
        message: str,
        user_id: str,
        history: Optional[list[dict[str, Any]]] = None,
        user: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """`user` 由认证层传入（生产路径）；不传时回退到演示用户表。"""
        user = user or self.user(user_id)
        text = " ".join(message.strip().split())
        if not text:
            raise ValueError("消息不能为空")
        visible = self.visible_tasks(user_id, user)
        visible_ids = [task["task_id"] for task in visible]
        slots = self.parse_slots(text)
        sources = self._default_sources()
        requires_approval = False

        if not self._can(user, "metrics:read:financial") and any(term in text for term in SENSITIVE_TERMS):
            intent = "permission_denied"
            answer = (
                f"{user['name']}，这项内容属于店长或总部权限，我不能向店员账号展示。"
                "你可以问自己的任务、操作步骤、差评接待流程和如何提交完成证据。"
            )
            sources = ["角色权限策略 v1"]
        elif any(term in text for term in HIGH_RISK_TERMS):
            intent = "approval_required"
            requires_approval = True
            answer = (
                "我可以整理数据依据并生成操作草稿，但不会直接执行。"
                "改价、赔付、公开回复、警告、奖惩和发布必须由有权限的管理者确认，确认后还要记录审批人与执行结果。"
            )
            sources = ["高风险动作审批策略 v1"]
        else:
            intent = self.match_intent(text) or ""
            days = int(slots["days"])
            if intent == "platform_metrics":
                answer, sources = self._answer_platform_metrics(user, slots)
            elif intent == "daily_report":
                answer, sources = self._answer_daily_report(user)
            elif intent == "data_quality":
                answer, sources = self._answer_data_quality(user, days=30 if days == 7 else days)
            elif intent == "data_gap":
                answer, sources = self._answer_data_gap(user)
            elif intent == "task_progress":
                answer, sources = self._answer_task_progress(user)
            elif intent == "list_tasks":
                answer, sources = self._answer_list_tasks(user)
            elif intent == "reputation_help":
                answer, sources = self._answer_reputation_help(user)
            elif intent == "package_help":
                answer, sources = self._answer_package_help(user)
            elif intent == "execution_help":
                answer, sources = self._answer_execution_help()
            elif intent == "explain_tasks":
                answer, sources = self._answer_explain_tasks(user)
            elif self.tool_agent is not None:
                try:
                    intent = "llm_tools"
                    outcome = self.tool_agent.answer(text, user, history or [])
                    answer = outcome["answer"]
                    if outcome.get("calls"):
                        used = "、".join(sorted({call["name"] for call in outcome["calls"]}))
                        answer += f"\n\n（依据：调用了 {used}；共 {outcome['iterations']} 轮）"
                    sources = list(outcome.get("sources") or []) + self._default_sources()
                except Exception:                                   # noqa: BLE001 - 模型不可用要降级
                    intent = "llm_unavailable"
                    answer = "大模型暂时不可用。我仍可回答今日任务、任务依据、差评流程、套餐反馈和完成证据。"
                    sources = ["Agent 降级策略 v1"]
            elif self.llm_responder:
                try:
                    intent = "llm_conversation"
                    answer = self.llm_responder.answer(text, user, visible, history or [])
                    sources = [{"type": "llm", "label": "大模型基于当前用户可见上下文生成"}]
                except Exception:
                    intent = "llm_unavailable"
                    answer = "大模型暂时不可用。我仍可回答今日任务、任务依据、差评流程、套餐反馈和完成证据。"
                    sources = ["Agent 降级策略 v1"]
            else:
                intent = "capability_help"
                answer = (
                    f"你好，{user['name']}。我是武汉狮子座店的门店运营 Agent。"
                    "你可以问我：今天做什么、某项任务为什么生成、差评怎么处理、套餐问题怎么反馈、"
                    "任务完成需要什么证据、数据齐不齐、生成今日日报。"
                    "配置大模型后还可进行开放式门店运营交流。"
                )

        return {
            "answer": answer,
            "intent": intent,
            "user": user,
            "store": self.snapshot["store"],
            "sources": sources,
            "requires_approval": requires_approval,
            "visible_task_ids": visible_ids,
            "slots": slots,
            "mode": "tools" if self.tools is not None else "local_demo",
            "llm_enabled": self.llm_responder is not None,
        }
