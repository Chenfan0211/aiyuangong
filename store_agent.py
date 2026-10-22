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
import sys
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

# 高风险动作：只有"既提到风险对象、又表达了执行诉求、且不是在提问"才拦。
# 旧写法是"提及即拦"，导致"同商圈竞品有没有降价"被当成要改价（见第 11 期实证）。
RISK_OBJECTS = ("价", "价格", "赔付", "赔偿", "补偿", "赔", "警告", "处罚", "罚款", "处分",
                "奖惩", "发布", "上架", "回复")
# 注："回复"要单独列出：只写"公开回复"会让"帮我把这条差评回复掉"漏过闸门，
# 而"差评怎么回复"因为带疑问标记仍然放行——这正是本期实证要求的那一对。
RISK_ACTIONS = ("帮我", "给我", "替我", "把", "将", "下调", "上调", "改成", "改为", "调整",
                "执行", "下发", "发布", "上架", "回复掉", "处理掉", "马上", "立刻", "立即",
                "给他", "给她", "给顾客")
# 另一件事：判断**一条建议的内容**是否涉及需要审批的高风险动作
# （用在 advice 的 approval_required 上，与"用户是否在要求执行"不是同一判定）。
HIGH_RISK_ACTION_TERMS = ("改价", "降价", "赔付", "赔偿", "警告", "处罚", "罚款", "奖惩",
                          "发布", "上架")
QUESTION_MARKERS = ("吗", "呢", "?", "？", "怎么", "如何", "为什么", "能不能", "可不可以",
                    "是否", "有没有", "该不该", "多少", "哪些", "哪个", "什么", "啥")


def is_high_risk_request(text: str) -> bool:
    """三个条件同时成立才算"要求执行高风险动作"：风险对象 ∩ 动作表达 ∩ 非提问。

    这是**尽力而为的规则闸门**：中文里动作与对象会重叠（"上架"既是动作也是对象）、
    表达方式也无穷（"给他赔 50 元"）。它只是第一道防线，第二道是模型侧的 system 提示
    （禁止输出可执行的高风险指令），第三道是写操作永远只出草稿。
    误拦的代价是"多问一句"，漏拦的代价由后两道兜住。
    """
    return (any(term in text for term in RISK_OBJECTS)
            and any(term in text for term in RISK_ACTIONS)
            and not any(mark in text for mark in QUESTION_MARKERS))


# 兜底引导：未命中任何能力时，按用户问到的主题给出"怎么才能答"的可操作指引，
# 而不是回一段与问题无关的固定能力清单。
TOPIC_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("库存", "物料", "补货", "断货", "物料预警"),
     "库存与物料：把盘点结果放进收件箱（import-inventory 或 import-dir）导入后，"
     "我可以报出未处理的库存预警与要不要补货。"),
    (("时段", "翻台", "客流", "午市", "晚市", "高峰期"),
     "时段与客流：导入「时段分布」「开台与客流」两类报表后，我能报出各时段的订单占比与翻台率。"),
    (("竞品", "对手", "同行", "同商圈", "别人家", "隔壁"),
     "竞品：采样台账（竞品门店 + 采样明细）导入后，我可以报已确认的匹配和尚未匹配的竞品套餐。"),
    (("手册", "规范", "制度", "知识库", "文档"),
     "门店手册与制度类问答属于知识库能力，当前还不能按文档回答。"),
    (("排班", "考勤", "工资", "薪资"),
     "排班、考勤与工资不在当前能力范围内。"),
)
SENSITIVE_TERMS = ("毛利", "利润", "工资", "薪资", "老板报表", "全店员工", "奖惩记录")



@dataclass(frozen=True)
class IntentRule:
    """一条意图规则：命中任一关键词即视为该意图。规则表按顺序匹配，越具体越靠前。"""

    intent: str
    keywords: tuple[str, ...]
    note: str = ""


INTENT_RULES: tuple[IntentRule, ...] = (
    IntentRule("store_diagnosis", (
        "为什么订单", "为什么曝光", "为什么访问", "为什么下降", "为什么上涨", "为什么掉",
        "下降原因", "上涨原因", "原因分析", "经营诊断", "归因",
        "订单少", "订单下滑", "订单下降", "销量下降", "业绩下降", "生意变差", "生意差",
        "客流下降", "客流少", "涨不上去", "越来越差",
    ), "经营变化归因"),
    IntentRule("task_help", (
        "任务详情", "这个任务", "这些任务具体", "任务具体", "任务目标", "任务要求",
        "需要提交什么证据", "提交什么证据", "任务完成需要什么证据", "怎么完成这个任务", "这个任务怎么做",
    ), "任务具体信息与执行方案"),
    IntentRule("period_comparison", (
        "对比昨天", "和昨天比", "比昨天", "对比前天", "前几天", "周期对比", "环比",
        "和前7天", "较前", "变化趋势",
    ), "周期对比"),

    IntentRule("daily_report", ("日报", "经营报告", "今日报告", "今天总结"), "生成店长日报"),
    IntentRule("data_gap", ("哪天缺数", "缺哪几天", "哪天没数据", "哪些天没数据", "缺数日期"), "缺日明细"),
    IntentRule("data_quality", ("数据齐不齐", "数据质量", "数据全不全", "缺不缺数", "数据完不完整"), "数据质量体检"),
    IntentRule("task_progress", ("任务完成情况", "完成情况", "完成率", "完成了多少"), "任务执行统计"),
    IntentRule("explain_tasks", (
        "任务为什么", "为什么生成", "生成依据", "任务依据", "派单依据", "任务的原因",
    ), "解释任务生成依据"),
    IntentRule("execution_help", ("怎么做", "流程", "步骤", "证据", "完成了", "做完了"), "执行 SOP"),
    IntentRule("reputation_help", ("差评", "评价", "口碑"), "评价与差评处理"),
    IntentRule("package_help", ("套餐", "转化", "团购"), "套餐诊断"),
    IntentRule("advice", (
        "建议", "方案", "优化", "提升", "改善", "怎么办", "怎么处理", "下一步",
        "行动计划", "如何提升", "怎么提高", "如何提高",
    ), "经营行动建议"),
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

TASK_CATEGORY_LABELS = {
    "reputation": "口碑与评价",
    "package": "套餐与转化",
    "traffic": "流量与曝光",
    "fulfillment": "履约与核销",
    "review": "评价处理",
}
TASK_TARGET_LABELS = {
    "metric": "指标",
    "operator": "条件",
    "threshold": "阈值",
    "value": "目标值",
    "window": "窗口",
    "platform": "平台",
    "package_id": "套餐",
    "category": "类别",
    "name": "名称",
    "target": "目标",
}
TASK_CATEGORY_STEPS = {
    "reputation": (
        "先核对订单、评价原文和履约事实，确认问题属于服务、履约还是顾客预期差异",
        "形成不诱导好评的回复或补救草稿；涉及赔付、公开回复时提交审批",
        "上传脱敏订单号、沟通记录或截图，完成后等待负责人验收",
    ),
    "package": (
        "确认套餐样本量、价格和漏斗掉点，先判断是曝光、访问还是支付环节问题",
        "只做一个变量实验，明确上线时间、观察窗口和停止条件",
        "价格、库存、门店范围和上架动作必须走审批，记录实验前后指标",
    ),
    "traffic": (
        "核对活动、投放、平台曝光入口和内容发布时间是否发生变化",
        "检查标题、主图、团购锚点和营业状态是否影响曝光",
        "记录调整前后曝光量、访问量和活动状态，按窗口复盘",
    ),
    "fulfillment": (
        "核查预约排期、库存、核销提醒和到店体验问题",
        "逐单区分正常延期、顾客取消和履约异常",
        "上传脱敏处理记录，并跟踪核销率和退款变化",
    ),
}
DEFAULT_TASK_STEPS = (
    "确认任务目标、截止时间和负责人，不自行扩大执行范围",
    "按门店 SOP 执行，并保留可核验的脱敏记录",
    "提交任务要求的证据，等待负责人验收",
)


def load_env_file(path: Optional[Path] = None) -> int:
    """把仓库根目录的 `.env` 填进环境变量（**只填空，不覆盖已有值**）。

    `.env` 已在 .gitignore / .dockerignore 中排除；直接 `python app_fastapi.py` 启动时
    不会自动读取它，部署侧读的是 `.env.production`。这里补上，让"Key 放 .env 即生效"成立。
    返回实际注入的变量个数。
    """
    # 测试环境绝不加载本地 .env：否则开发者本机的真实 Key 会被测试捡到，
    # 造成"测试依赖外部模型 / 产生真实费用 / 结果不确定"。显式置
    # STORE_AGENT_DISABLE_DOTENV=1 也能关掉（供其它测试框架使用）。
    if os.environ.get("STORE_AGENT_DISABLE_DOTENV") or "unittest" in sys.modules:
        return 0
    target = Path(path) if path else Path(__file__).resolve().parent / ".env"
    if not target.is_file():
        return 0
    filled = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value and not os.environ.get(name):
            os.environ[name] = value
            filled += 1
    return filled


class OpenAICompatibleResponder:
    """可选的大模型兜底；只接收服务端裁剪后的最小上下文。"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 20):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> Optional["OpenAICompatibleResponder"]:
        load_env_file()
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
            approval = "，需审批后执行" if task.get("requires_approval") or task.get("approval_required") else ""
            status = f"，当前状态 {task.get('status')}" if task.get("status") else ""
            due = str(task.get("due_at") or "")
            due_text = due[11:16] if len(due) >= 16 else (due or "未设置")
            reason = str(task.get("reason") or "未提供").strip()
            evidence = StoreOperationsAgent._evidence_text(task)
            lines.append(
                f"{index}. {task.get('title')}（{task.get('priority')}，截止 {due_text}{approval}{status}）\n"
                f"   依据：{reason}\n   证据：{evidence}"
            )
        return "\n".join(lines)

    @staticmethod
    def _target_text(task: dict[str, Any]) -> str:
        target = task.get("target") or {}
        if isinstance(target, dict):
            parts = []
            for key, value in target.items():
                if value is None or value == "":
                    continue
                label = TASK_TARGET_LABELS.get(str(key), str(key))
                if isinstance(value, (dict, list)):
                    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                else:
                    rendered = str(value)
                parts.append(f"{label}：{rendered}")
            return "；".join(parts) or "未配置"
        if isinstance(target, list):
            return "；".join(str(item) for item in target) or "未配置"
        return str(target) if target else "未配置"

    @staticmethod
    def _evidence_text(task: dict[str, Any]) -> str:
        evidence = task.get("evidence_required") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        return "、".join(str(item) for item in evidence if str(item).strip()) or "无额外证据要求"

    @classmethod
    def _task_step_lines(cls, task: dict[str, Any]) -> list[str]:
        category = str(task.get("category") or "")
        return list(TASK_CATEGORY_STEPS.get(category, DEFAULT_TASK_STEPS))

    @classmethod
    def _task_detail_text(cls, task: dict[str, Any], include_track: bool = False) -> str:
        title = str(task.get("title") or "未命名任务")
        task_id = str(task.get("task_id") or "-")
        category = TASK_CATEGORY_LABELS.get(str(task.get("category") or ""), str(task.get("category") or "未分类"))
        assignee = str(task.get("assignee") or task.get("assignee_id") or "未指定")
        approval = bool(task.get("requires_approval") or task.get("approval_required"))
        lines = [
            f"任务：{title}（{task_id}）",
            f"分类：{category}｜优先级：{task.get('priority') or '-'}｜状态：{task.get('status') or '-'}｜负责人：{assignee}",
            f"截止时间：{task.get('due_at') or '未设置'}",
            f"生成依据：{task.get('reason') or '未提供'}",
            f"目标：{cls._target_text(task)}",
            f"需要提交的证据：{cls._evidence_text(task)}",
            f"审批要求：{'需要审批' if approval else '无需审批'}",
            "执行建议：",
        ]
        lines.extend(f"- {step}" for step in cls._task_step_lines(task))
        lines.append("完成条件：已按要求执行，并提交规定证据后，由负责人验收。")
        if include_track:
            evidence = task.get("evidence") or []
            events = task.get("events") or []
            if evidence:
                lines.append("已提交证据：")
                for item in evidence[-5:]:
                    label = item.get("evidence_type") or "证据"
                    ref = item.get("object_ref") or "-"
                    lines.append(f"- {label}：{ref}")
            if events:
                lines.append("最近事件：")
                for item in events[-5:]:
                    lines.append(f"- {item.get('from_status') or '创建'} → {item.get('to_status') or '-'}（{item.get('actor_id') or '系统'}）")
        return "\n".join(lines)

    @staticmethod
    def _match_task(text: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        matched = []
        for task in tasks:
            if str(task.get("task_id") or "") in text or str(task.get("title") or "") in text:
                matched.append(task)
        return matched

    @classmethod
    def _task_advice(cls, task: dict[str, Any]) -> dict[str, Any]:
        steps = cls._task_step_lines(task)
        return {
            "headline": f"任务《{task.get('title') or '未命名任务'}》执行方案",
            "actions": [
                {"priority": index, "title": step, "detail": "按任务目标执行，不扩大范围",
                 "owner_role": str(task.get("assignee") or task.get("assignee_id") or "负责人"),
                 "evidence": cls._evidence_text(task),
                 "metric": cls._target_text(task),
                 "approval_required": bool(task.get("requires_approval") or task.get("approval_required"))}
                for index, step in enumerate(steps, 1)
            ],
            "watch": ["任务状态", "提交证据", "截止时间"],
            "confidence": "high",
            "missing_data": [] if (task.get("evidence_required") or []) else ["任务证据要求未配置"],
        }

    @staticmethod
    def _advice_metric_label(cause: dict[str, Any]) -> str:
        for evidence in cause.get("evidence") or []:
            metric = evidence.get("metric") if isinstance(evidence, dict) else None
            if metric:
                return {
                    "impressions": "曝光量", "detail_views": "详情访问", "paid_orders": "支付订单",
                    "verified_orders": "核销订单", "view_rate": "访问转化率", "pay_rate": "支付转化率",
                    "verify_rate": "核销率", "refunds": "退款单数", "refund_rate": "退款率",
                    "negative_rate": "差评率", "avg_rating": "评分",
                }.get(str(metric), str(metric))
        return "核心经营指标"

    @staticmethod
    def _advice_owner_role(code: str) -> str:
        return {
            "data_quality": "店长/运营",
            "traffic": "线上运营",
            "visit_conversion": "线上运营",
            "pay_conversion": "店长",
            "fulfillment": "店长/店员",
            "refund": "店长",
            "reputation": "店长/店员",
        }.get(code, "店长")

    @staticmethod
    def _advice_evidence(code: str) -> str:
        return {
            "data_quality": "补数记录、导出批次和日期覆盖截图",
            "traffic": "活动/投放状态记录、曝光入口截图",
            "visit_conversion": "主图、标题、价格带调整前后记录",
            "pay_conversion": "套餐价格/库存变更记录和下单链路截图",
            "fulfillment": "预约、核销和退款处理记录",
            "refund": "退款原因分类表",
            "reputation": "差评回复、回访和整改任务记录",
        }.get(code, "执行记录和调整前后指标")

    def _advice_from_diagnosis(self, diagnosis: dict[str, Any], comparison: dict[str, Any]) -> dict[str, Any]:
        causes = diagnosis.get("causes") or []
        actions: list[dict[str, Any]] = []
        missing_data: list[str] = []
        for cause in causes:
            code = str(cause.get("code") or "")
            if code in {"insufficient_evidence", "data_quality"}:
                missing_data.append(str(cause.get("label") or "需要补齐数据"))
            for action in cause.get("actions") or []:
                if len(actions) >= 3:
                    break
                text = str(action)
                actions.append({
                    "priority": len(actions) + 1,
                    "title": text,
                    "detail": str(cause.get("label") or ""),
                    "owner_role": self._advice_owner_role(code),
                    "evidence": self._advice_evidence(code),
                    "metric": self._advice_metric_label(cause),
                    "approval_required": any(term in text for term in HIGH_RISK_ACTION_TERMS),
                })
            if len(actions) >= 3:
                break
        if missing_data and not actions:
            actions = [{"priority": 1, "title": "先补齐缺失日期的平台报表并重新导入",
                        "detail": "数据不完整时不能做确定经营归因", "owner_role": "店长/运营",
                        "evidence": "补数记录和导出批次", "metric": "数据覆盖率",
                        "approval_required": False}]
        if not actions:
            actions = [{"priority": 1, "title": "保持当前经营动作，继续观察下一周期",
                        "detail": "当前数据未发现明显异常", "owner_role": "店长",
                        "evidence": "本周期经营记录", "metric": "核心经营指标",
                        "approval_required": False}]
        confidence = "low" if missing_data else "medium"
        for cause in causes:
            if cause.get("confidence") == "high":
                confidence = "high"
                break
        watch = []
        for action in actions:
            if action["metric"] not in watch:
                watch.append(action["metric"])
        return {
            "headline": str(causes[0].get("label")) if causes else "当前数据未发现明显异常",
            "actions": actions,
            "watch": watch or ["曝光量", "详情访问", "支付订单", "核销率"],
            "confidence": confidence,
            "missing_data": missing_data,
        }

    @staticmethod
    def _render_advice(advice: dict[str, Any]) -> str:
        lines = [f"结论：{advice.get('headline') or '先确认数据，再决定动作'}", "优先动作："]
        for action in advice.get("actions") or []:
            lines.append(
                f"{action.get('priority')}. {action.get('title')}\n"
                f"   责任角色：{action.get('owner_role')}｜证据/输出：{action.get('evidence')}｜"
                f"观察指标：{action.get('metric')}｜需审批：{'是' if action.get('approval_required') else '否'}"
            )
        if advice.get("missing_data"):
            lines.append("缺失数据：" + "；".join(advice["missing_data"]))
        lines.append("观察指标：" + "、".join(advice.get("watch") or []))
        lines.append("风险提示：建议基于已导入数据，不替代门店现场核实；涉及改价、赔付、发布或上架必须先审批。")
        return "\n".join(lines)

    def _answer_task_help(self, user: dict[str, Any], text: str) -> tuple[str, list[Any], Optional[dict[str, Any]]]:
        visible = self.visible_tasks(user["user_id"], user)
        if not visible:
            return "当前没有分配给你的任务，因此没有可展开的任务详情。", self._default_sources(), None
        matched = self._match_task(text, visible)
        selected = matched[:3] if matched else visible[:3]
        if matched and len(matched) == 1 and self.tools is not None:
            detail = self.tools.task_detail(matched[0]["task_id"], user)
            view = self._task_view(detail)
            view["events"] = detail.get("events") or []
            view["evidence"] = detail.get("evidence") or []
            selected = [view]
        blocks = [self._task_detail_text(task, include_track=bool(matched and len(matched) == 1))
                  for task in selected]
        suffix = ""
        if len(visible) > len(selected):
            suffix = f"\n\n还有 {len(visible) - len(selected)} 项任务未展开，请继续指定任务名称。"
        return "任务具体信息如下：\n\n" + "\n\n".join(blocks) + suffix, self._default_sources(), self._task_advice(selected[0])

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

    def _fallback_guidance(self, user: dict[str, Any], text: str) -> str:
        """未命中任何能力时的兜底：按用户问到的**主题**给出可操作指引。

        旧的兜底是一段固定的能力清单，与用户实际问的东西无关；这里改为
        先看问的是哪一类（库存 / 时段 / 竞品 / 手册 / 排班），再说明要怎样才能答。
        """
        hints = [hint for keywords, hint in TOPIC_HINTS
                 if any(keyword in text for keyword in keywords)]
        lines = [f"{user['name']}，我没有完全确定你在问哪一类信息。"]
        if hints:
            lines.append("")
            lines.extend(f"- {hint}" for hint in hints)
        lines.append("")
        lines.append(
            "我现在能直接回答的：今日任务、某项任务为什么生成、任务完成率与逾期、"
            "数据齐不齐与缺哪几天、经营周期对比与归因、差评处理流程、套餐转化，以及生成店长日报。"
        )
        lines.append("配置大模型后，可以直接用日常说法提问（含开台/客流、时段、库存与竞品）。")
        return "\n".join(lines)

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

    COMPARISON_METRICS = (
        ("impressions", "曝光量"), ("detail_views", "详情访问"), ("paid_orders", "支付订单"),
        ("verified_orders", "核销订单"), ("gmv", "成交金额"), ("refunds", "退款单数"),
    )
    COMPARISON_RATES = (
        ("view_rate", "访问率"), ("pay_rate", "支付转化率"), ("verify_rate", "核销率"),
        ("negative_rate", "差评率"), ("refund_rate", "退款率"),
    )
    CAUSE_CONFIDENCE = {"high": "高", "medium": "中", "low": "低"}

    @staticmethod
    def _fmt_change(value: Any) -> str:
        if value is None:
            return "变化不可比"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        return f"{number:+.1f}%"

    @staticmethod
    def _fmt_abs(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{value:,}"

    def _render_comparison(self, result: dict[str, Any]) -> str:
        lines = ["周期对比结果（默认按每平台最新完整数据日）"]
        for key in ("day", "week"):
            window = (result.get("windows") or {}).get(key)
            if not window:
                continue
            current, previous = window.get("current"), window.get("previous")
            if current and previous:
                lines.append(f"\n【{window.get('label') or key}】{current['start']} ~ {current['end']} "
                             f"对比 {previous['start']} ~ {previous['end']}")
            metrics, previous_metrics = window.get("metrics"), window.get("previous_metrics")
            if metrics and previous_metrics:
                for metric, label in self.COMPARISON_METRICS:
                    if metric not in metrics or metric not in previous_metrics:
                        continue
                    absolute = (window.get("absolute_change") or {}).get(metric)
                    suffix = f"，变化 {self._fmt_abs(absolute)}" if absolute is not None else ""
                    lines.append(f"- {label}：{self._fmt_abs(metrics[metric])} vs "
                                 f"{self._fmt_abs(previous_metrics[metric])}"
                                 f"（{self._fmt_change((window.get('change_percent') or {}).get(metric))}{suffix}）")
                for metric, label in self.COMPARISON_RATES:
                    if metric not in metrics or metric not in previous_metrics:
                        continue
                    absolute = (window.get("absolute_change") or {}).get(metric)
                    suffix = f"，变化 {self._fmt_abs(absolute)} 个百分点" if absolute is not None else ""
                    lines.append(f"- {label}：{self._fmt_abs(metrics[metric])}% vs "
                                 f"{self._fmt_abs(previous_metrics[metric])}%{suffix}")
            else:
                lines.append("- 门店汇总缺少两个窗口都覆盖完整的平台数据，不能给出确定升降结论。")
            platforms = window.get("platforms") or {}
            for item in platforms.values():
                if item.get("metrics") and item.get("previous_metrics"):
                    changes = item.get("change_percent") or {}
                    lines.append(
                        f"- {item.get('label')}：曝光 {self._fmt_abs(item['metrics'].get('impressions'))} "
                        f"({self._fmt_change(changes.get('impressions'))})，支付 "
                        f"{self._fmt_abs(item['metrics'].get('paid_orders'))} "
                        f"({self._fmt_change(changes.get('paid_orders'))})"
                    )
                else:
                    reason = "；".join(str(entry.get("label", entry.get("code", ""))) for entry in item.get("blocked") or [])
                    lines.append(f"- {item.get('label')}：{reason or '没有可对比数据'}")
            blocked = window.get("blocked") or []
            if blocked:
                lines.append("数据完整性提示：" + "；".join(
                    str(item.get("label", item.get("code", ""))) for item in blocked
                ))
        lines.append("\n依据：以上数字来自已导入的平台指标库和实际对比日期，不包含演示数字。")
        return "\n".join(lines)

    def _render_diagnosis(self, diagnosis: dict[str, Any]) -> str:
        lines = ["周期对比与经营归因（以下为可能原因，不是确定因果）"]
        causes = diagnosis.get("causes") or []
        for cause in causes:
            confidence = self.CAUSE_CONFIDENCE.get(str(cause.get("confidence")), str(cause.get("confidence") or ""))
            lines.append(f"\n- {cause.get('label')}（置信度：{confidence or '低'}）")
            for evidence in cause.get("evidence") or []:
                if not isinstance(evidence, dict):
                    lines.append(f"  证据：{evidence}")
                    continue
                metric = evidence.get("metric")
                if metric:
                    label = self._advice_metric_label({"evidence": [evidence]})
                    pieces = []
                    if evidence.get("current") is not None or evidence.get("previous") is not None:
                        pieces.append(f"本期 {self._fmt_abs(evidence.get('current'))}，上期 {self._fmt_abs(evidence.get('previous'))}")
                    if evidence.get("change_percent") is not None:
                        pieces.append(f"变化 {self._fmt_change(evidence.get('change_percent'))}")
                    if evidence.get("absolute_change") is not None:
                        pieces.append(f"绝对变化 {self._fmt_abs(evidence.get('absolute_change'))}")
                    lines.append(f"  证据：{label}，" + "；".join(pieces))
                elif evidence.get("type") == "calendar":
                    lines.append(f"  证据：本期周末 {evidence.get('current_weekend_days')} 天，上期周末 {evidence.get('previous_weekend_days')} 天")
                elif evidence.get("type") == "blocked":
                    lines.append(f"  证据：{evidence.get('platform_label') or evidence.get('platform') or '平台'} {evidence.get('detail') or evidence.get('reason') or '数据被阻断'}")
                elif evidence.get("type") == "missing":
                    lines.append("  证据：对比窗口存在缺失数据，当前无法形成完整结论")
                elif evidence.get("detail"):
                    lines.append(f"  证据：{evidence.get('detail')}")
            actions = cause.get("actions") or []
            if actions:
                lines.append("  建议：" + "；".join(str(action) for action in actions))
        blocked = diagnosis.get("blocked") or []
        if blocked:
            lines.append("\n数据质量阻断：存在缺口，确定原因不成立，请先补齐数据。")
        for caveat in diagnosis.get("caveats") or []:
            lines.append(f"注意：{caveat}")
        lines.append(f"\n依据：{diagnosis.get('disclaimer')}")
        return "\n".join(lines)

    def _answer_period_comparison(self, user: dict[str, Any], slots: dict[str, Any]) -> tuple[str, list[Any]]:
        if self.tools is None:
            return ("周期对比需要在生产数据模式下运行；当前演示快照不能给出真实对比。",
                    self._default_sources())
        try:
            result = self.tools.period_comparison(
                user, anchor="auto", windows=("day", "week"),
                platform=slots.get("platform"), metric=slots.get("metric"),
            )
        except PermissionDenied as exc:
            return (f"{user['name']}，你没有查看经营指标的权限。", [{"type": "policy", "label": str(exc)}])
        return self._render_comparison(result), list(result.get("sources") or [])

    def _answer_advice(self, user: dict[str, Any], slots: dict[str, Any]) -> tuple[str, list[Any], Optional[dict[str, Any]]]:
        if self.tools is None:
            advice = {
                "headline": "当前是演示快照模式，不能生成真实经营建议",
                "actions": [{"priority": 1, "title": "先补齐平台数据或切换到生产数据模式",
                             "detail": "没有真实指标时不能判断曝光、转化或履约问题", "owner_role": "店长/运营",
                             "evidence": "平台导出文件和导入记录", "metric": "数据覆盖率", "approval_required": False}],
                "watch": ["数据覆盖率", "曝光量", "支付订单"], "confidence": "low",
                "missing_data": ["真实平台指标数据"],
            }
            return self._render_advice(advice), self._default_sources(), advice
        try:
            comparison = self.tools.period_comparison(
                user, anchor="auto", windows=("day", "week"),
                platform=slots.get("platform"), metric=slots.get("metric"),
            )
            diagnosis = self.tools.diagnose_store_change(user, comparison=comparison)
        except PermissionDenied as exc:
            return f"{user['name']}，你没有查看经营指标的权限。", [{"type": "policy", "label": str(exc)}], None
        advice = self._advice_from_diagnosis(diagnosis, comparison)
        sources = list(diagnosis.get("sources") or []) + self._default_sources()
        return self._render_advice(advice), sources, advice

    def _answer_store_diagnosis(self, user: dict[str, Any], slots: dict[str, Any]) -> tuple[str, list[Any], Optional[dict[str, Any]]]:
        if self.tools is None:
            return ("经营归因需要在生产数据模式下运行；当前演示快照不能给出真实原因。",
                    self._default_sources(), None)
        try:
            comparison = self.tools.period_comparison(
                user, anchor="auto", windows=("day", "week"), platform=slots.get("platform"),
            )
            diagnosis = self.tools.diagnose_store_change(user, comparison=comparison)
        except PermissionDenied as exc:
            return (f"{user['name']}，你没有查看经营指标的权限。", [{"type": "policy", "label": str(exc)}], None)
        advice = self._advice_from_diagnosis(diagnosis, comparison)
        answer = self._render_diagnosis(diagnosis) + "\n\n" + self._render_advice(advice)
        return answer, list(diagnosis.get("sources") or []), advice

    # ---------------- 主入口 ----------------

    def reply_stream(
        self,
        message: str,
        user_id: str,
        history: Optional[list[dict[str, Any]]] = None,
        user: Optional[dict[str, Any]] = None,
    ):
        """流式版 `reply()`：逐个 yield 事件，供 SSE 使用。

        - **安全闸门与无模型路径不流式**：它们本来就瞬时产出，直接复用 `reply()` 的结果，
          一次性发 `delta` + `done`（前端只有一条代码路径）。
        - **模型路径真流式**：边调工具边出字；模型失败时降级回规则/兜底并标记 `degraded`。
        """
        user = user or self.user(user_id)
        text = " ".join(message.strip().split())
        if not text:
            raise ValueError("消息不能为空")
        gated = (not self._can(user, "metrics:read:financial")
                 and any(term in text for term in SENSITIVE_TERMS)) or is_high_risk_request(text)

        def _rule_events(result: dict[str, Any], degraded: bool = False):
            yield {"type": "delta", "text": result["answer"]}
            # mode 描述的是"这次答案是谁产出的"，不是"是否配了模型"：
            # 安全闸门与规则层产出的答案必须报 rules，否则前端会误标成模型回答。
            yield {"type": "done", "text": result["answer"], "intent": result["intent"],
                   "sources": result["sources"],
                   "mode": "rules",
                   "llm_enabled": result["llm_enabled"], "degraded": degraded,
                   "requires_approval": result["requires_approval"],
                   "advice": result.get("advice")}

        if gated or self.tool_agent is None:
            yield from _rule_events(self.reply(message, user_id, history=history, user=user))
            return

        yield {"type": "status", "text": "正在理解你的问题…"}
        final: Optional[dict[str, Any]] = None
        streamed = False
        for event in self.tool_agent.answer_stream(text, user, history or []):
            if event["type"] == "delta":
                streamed = True
                yield event
            elif event["type"] == "status":
                yield event
            elif event["type"] == "error":
                # 已经吐出过内容就不能假装没发生：如实标记降级并收尾。
                if streamed:
                    yield {"type": "done", "text": "", "intent": "llm_unavailable",
                           "sources": event.get("sources") or [], "mode": "tools",
                           "llm_enabled": True, "degraded": True,
                           "requires_approval": False, "advice": None}
                else:
                    fallback = self.reply(message, user_id, history=history, user=user)
                    yield from _rule_events(fallback, degraded=True)
                return
            else:
                final = event

        if final is None:
            yield {"type": "done", "text": "", "intent": "llm_unavailable",
                   "sources": [], "mode": "tools", "llm_enabled": True, "degraded": True,
                   "requires_approval": False, "advice": None}
            return
        calls = final.get("calls") or []
        answer = str(final.get("text") or "")
        if calls:
            used = "、".join(sorted({call["name"] for call in calls}))
            answer += f"\n\n（依据：调用了 {used}；共 {final.get('iterations')} 轮）"
            yield {"type": "delta", "text": f"\n\n（依据：调用了 {used}；共 {final.get('iterations')} 轮）"}
        yield {"type": "done", "text": answer, "intent": "llm_tools",
               "sources": list(final.get("sources") or []) + self._default_sources(),
               "mode": "tools", "llm_enabled": True, "degraded": False,
               "requires_approval": False, "advice": None}

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
        advice: Optional[dict[str, Any]] = None
        # 这三个在三个分支里都会用到（权限拦截 / 高风险诉求 / 正常作答），
        # 必须在这里初始化：否则高风险分支走到函数末尾会 UnboundLocalError。
        handled = False
        keyword_intent = ""
        intent = ""

        if not self._can(user, "metrics:read:financial") and any(term in text for term in SENSITIVE_TERMS):
            intent = "permission_denied"
            answer = (
                f"{user['name']}，这项内容属于店长或总部权限，我不能向店员账号展示。"
                "你可以问自己的任务、操作步骤、差评接待流程和如何提交完成证据。"
            )
            sources = ["角色权限策略 v1"]
        elif is_high_risk_request(text):
            intent = "approval_required"
            requires_approval = True
            answer = (
                "我可以整理数据依据并生成操作草稿，但不会直接执行。"
                "改价、赔付、公开回复、警告、奖惩和发布必须由有权限的管理者确认，确认后还要记录审批人与执行结果。"
            )
            sources = ["高风险动作审批策略 v1"]
        else:
            days = int(slots["days"])
            # 第 11 期：有模型就模型优先。旧顺序把关键词层排在模型之前，而关键词是
            # "包含匹配"，导致配了 Key 也轮不到模型理解自由提问（见本期"根因与实证"）。
            if self.tool_agent is not None:
                try:
                    outcome = self.tool_agent.answer(text, user, history or [])
                    answer = outcome["answer"]
                    if outcome.get("calls"):
                        used = "、".join(sorted({call["name"] for call in outcome["calls"]}))
                        answer += f"\n\n（依据：调用了 {used}；共 {outcome['iterations']} 轮）"
                    sources = list(outcome.get("sources") or []) + self._default_sources()
                    intent = "llm_tools"
                    handled = True
                except Exception:                    # noqa: BLE001 - 模型不可用必须降级，不能抛给用户
                    intent = "llm_unavailable"
            elif self.llm_responder is not None:
                try:
                    answer = self.llm_responder.answer(text, user, visible, history or [])
                    sources = [{"type": "llm", "label": "大模型基于当前用户可见上下文生成"}]
                    intent = "llm_conversation"
                    handled = True
                except Exception:                    # noqa: BLE001
                    intent = "llm_unavailable"
            keyword_intent = "" if handled else (self.match_intent(text) or "")
            if handled:
                pass
            elif keyword_intent == "task_help":
                answer, sources, advice = self._answer_task_help(user, text)
            elif keyword_intent == "advice":
                answer, sources, advice = self._answer_advice(user, slots)
            elif keyword_intent == "platform_metrics":
                answer, sources = self._answer_platform_metrics(user, slots)
            elif keyword_intent == "period_comparison":
                answer, sources = self._answer_period_comparison(user, slots)
            elif keyword_intent == "store_diagnosis":
                answer, sources, advice = self._answer_store_diagnosis(user, slots)
            elif keyword_intent == "daily_report":
                answer, sources = self._answer_daily_report(user)
            elif keyword_intent == "data_quality":
                answer, sources = self._answer_data_quality(user, days=30 if days == 7 else days)
            elif keyword_intent == "data_gap":
                answer, sources = self._answer_data_gap(user)
            elif keyword_intent == "task_progress":
                answer, sources = self._answer_task_progress(user)
            elif keyword_intent == "list_tasks":
                answer, sources = self._answer_list_tasks(user)
            elif keyword_intent == "reputation_help":
                answer, sources = self._answer_reputation_help(user)
            elif keyword_intent == "package_help":
                answer, sources = self._answer_package_help(user)
            elif keyword_intent == "execution_help":
                answer, sources = self._answer_execution_help()
            elif keyword_intent == "explain_tasks":
                answer, sources = self._answer_explain_tasks(user)
            else:
                # 关键词也没命中 → 不再回一段与问题无关的固定清单，
                # 而是识别用户问到的主题，告诉他"要怎样才能答这个问题"。
                if not handled and intent != "llm_unavailable":
                    intent = "capability_help"
                answer = self._fallback_guidance(user, text)
                sources = ["Agent 兜底引导 v1"]

        # 注意顺序：degraded 必须在 intent 被关键词覆盖之前算出来，
        # 否则"模型挂了但关键词答上了"会被当成正常作答（第 11 期修）。
        degraded = intent == "llm_unavailable"
        if not handled and keyword_intent:
            intent = keyword_intent

        return {
            "answer": answer,
            "intent": intent,
            "degraded": degraded,
            "user": user,
            "store": self.snapshot["store"],
            "sources": sources,
            "requires_approval": requires_approval,
            "advice": advice,
            "visible_task_ids": visible_ids,
            "slots": slots,
            "mode": "tools" if self.tools is not None else "local_demo",
            "llm_enabled": self.llm_responder is not None,
        }
