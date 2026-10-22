#!/usr/bin/env python3
"""大模型工具调用（二期批次 13）：让开放式提问也能"先查真数再回答"。

定位：Agent 的**兜底路径**。规则能识别的意图（今日任务、数据齐不齐、日报…）仍走确定性逻辑；
命中不了的问题才交给这里，由模型决定调用哪个**只读工具**，拿到真实数据后组织答案。

三条安全边界（都有测试固化）：
  1. **工具集按角色过滤**：店员拿到的清单里根本没有日报/数据质量/财务口径工具；
     即使模型编造工具名，调度器也会拒绝（不是靠 prompt 自觉）。
  2. **只读**：工具集里没有任何写操作；改价、赔付、外发等仍走审批流程。
  3. **留痕**：每次工具调用写 `agent_tool_runs`（入参出参脱敏），回答带结构化引用 `sources`。

协议：OpenAI 兼容的 `POST {base_url}/chat/completions`（`tools` + `tool_calls`），
DeepSeek / OpenAI / 本地兼容服务都适用；未配置 Key 时整条路径不会被启用。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

DEFAULT_MAX_ITERATIONS = 4
DEFAULT_MAX_TOOL_CALLS = 8

SYSTEM_PROMPT = (
    "你是门店运营助手。只依据工具返回的真实数据回答，不要编造数字；数据不足就明确说缺数并给出补数步骤。"
    "你可以调用工具查询指标、数据质量、任务与日报。涉及经营变化、优化、方案、提升、建议时，"
    "必须优先调用 diagnose_store_change；涉及任务时优先调用 get_task_detail。"
    "回答必须包含：结论、原因或关键依据、优先行动建议、验证指标、风险与审批提醒。"
    "行动建议要说明责任角色、证据/输出和复盘时间；不要只罗列数据。"
    "你没有任何写权限：改价、赔付、公开回复、警告、奖惩、发布、上架都只能建议，必须由有权限的管理者审批。"
    "不得透露其他员工、其他门店、工资、密钥、顾客完整手机号等敏感信息。"
    "回答末尾用一句说明依据（用了哪些工具与统计区间）。"
)


@dataclass(frozen=True)
class ToolSpec:
    """一个只读工具的声明：名字、权限、给模型看的 schema。"""

    name: str
    permission: str
    description: str
    parameters: dict[str, Any]

    def as_openai_tool(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("get_data_status", "metrics:read",
             "查询各平台数据新鲜度（最新数据日期、是否过期、是否未接入）。",
             {"type": "object", "properties": {}, "required": []}),
    ToolSpec("get_metric_summary", "metrics:read",
             "查询近 N 天统一口径经营指标（曝光、访问、支付、核销、转化率，含环比）。",
             {"type": "object", "properties": {
                 "days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "统计天数，默认 7"},
                 "platform": {"type": "string", "enum": ["meituan", "douyin"], "description": "只看某个平台"}},
              "required": []}),
    ToolSpec("get_period_comparison", "metrics:read",
             "查询日/近7天两组周期对比，包含本期、上期、变化率、覆盖率、缺失日期与数据来源。",
             {"type": "object", "properties": {
                 "anchor": {"type": "string", "description": "auto / today / yesterday / YYYY-MM-DD，默认 auto"},
                 "windows": {"type": "array", "items": {"type": "string", "enum": ["day", "week"]},
                             "description": "默认 day 与 week"},
                 "platform": {"type": "string", "enum": ["meituan", "douyin"]},
                 "metric": {"type": "string", "description": "可选指标键，如 paid_orders"}},
              "required": []}),
    ToolSpec("diagnose_store_change", "metrics:read",
             "对周期对比结果做确定性经营归因，返回可能原因、证据、置信度和建议动作。",
             {"type": "object", "properties": {
                 "anchor": {"type": "string", "description": "auto / today / yesterday / YYYY-MM-DD，默认 auto"},
                 "windows": {"type": "array", "items": {"type": "string", "enum": ["day", "week"]}},
                 "platform": {"type": "string", "enum": ["meituan", "douyin"]},
                 "metric": {"type": "string"}},
              "required": []}),
    ToolSpec("get_data_quality", "quality:read",
             "查询数据质量：缺日、日环比跳变、导入批次与新鲜度。",
             {"type": "object", "properties": {
                 "days": {"type": "integer", "minimum": 1, "maximum": 365, "description": "体检窗口，默认 30"}},
              "required": []}),
    ToolSpec("get_data_gaps", "quality:read",
             "列出各平台缺失数据的日期（用于补数）。",
             {"type": "object", "properties": {
                 "days": {"type": "integer", "minimum": 1, "maximum": 365}}, "required": []}),
    ToolSpec("get_tasks", "tasks:read:own",
             "查询任务清单（店长可见全店，店员只能看到自己的任务）。",
             {"type": "object", "properties": {
                 "status": {"type": "string", "enum": ["open", "proposed", "ready", "accepted", "done",
                                                       "verified", "rejected", "overdue", "canceled"]}},
              "required": []}),
    ToolSpec("get_task_detail", "tasks:read:own",
             "查询某个任务的详情：状态、截止时间、证据与事件轨迹。",
             {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}),
    ToolSpec("get_task_completion", "tasks:read:own",
             "查询近 N 天任务执行情况（按状态计数与验收完成率）。",
             {"type": "object", "properties": {
                 "days": {"type": "integer", "minimum": 1, "maximum": 365, "description": "默认 7"}},
              "required": []}),
    ToolSpec("build_daily_report", "report:read",
             "生成店长日报文本（含指标、任务、数据质量与下一步建议）。",
             {"type": "object", "properties": {
                 "date": {"type": "string", "description": "today / yesterday / YYYY-MM-DD"}},
              "required": []}),

    # ---- 第 11 期：门店侧与竞品问数（此前第 1、2 期数据对模型完全不可见） ----
    ToolSpec("get_store_traffic", "metrics:read",
             "查询门店侧开台/客流/翻台率。未导入门店侧数据时如实回报缺数，不补零。",
             {"type": "object", "properties": {
                 "days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "统计天数，默认 7"}},
              "required": []}),
    ToolSpec("get_time_slots", "metrics:read",
             "查询门店各时段的客流、订单数与订单占比（用于判断哪个时段是低峰）。",
             {"type": "object", "properties": {
                 "days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "统计天数，默认 7"}},
              "required": []}),
    ToolSpec("get_inventory_alerts", "metrics:read",
             "查询未处理的库存/物料预警（按剩余量除以安全库存分级：critical 严重不足 / warning 低于安全库存）。",
             {"type": "object", "properties": {
                 "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "条数上限，默认 50"}},
              "required": []}),
    ToolSpec("get_competitor_matches", "metrics:read",
             "查询竞品匹配台账：已确认 / 待确认 / 已驳回的匹配，以及尚未匹配的竞品套餐。",
             {"type": "object", "properties": {}, "required": []}),
    ToolSpec("get_competitor_compare", "metrics:read",
             "查询竞品四维对比：价格带分档与 P25/P50/P75、套餐档位覆盖与已确认匹配的价差、"
             "活动分布、商圈价格带中位数环比。样本不足时只报样本不足，不下结论；"
             "只有人工确认过的匹配参与对比。",
             {"type": "object", "properties": {
                 "days": {"type": "integer", "minimum": 1, "maximum": 365,
                          "description": "覆盖默认窗口（默认 28 天）"}},
              "required": []}),
    ToolSpec("search_knowledge", "knowledge:read",
             "检索门店知识库（制度/SOP/手册）并返回可回查的引用：文档、章节路径与片段。"
             "没有命中时会明确说明知识库没有相关内容，不会编造引用。",
             {"type": "object", "properties": {
                 "query": {"type": "string", "description": "检索关键词或问题"},
                 "base_id": {"type": "string", "description": "只检索某个知识库"},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 10,
                           "description": "返回条数，默认 5"}},
              "required": ["query"]}),
)


class ToolCallingError(ValueError):
    """模型返回不可用（协议不符、无 choices 等）。"""


class OpenAIToolResponder:
    """OpenAI 兼容的带工具对话客户端（标准库实现，零第三方依赖）。"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 30):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> Optional["OpenAIToolResponder"]:
        from store_agent import load_env_file          # 惰性导入：避免与 store_agent 形成循环
        load_env_file()
        api_key = os.getenv("STORE_AGENT_LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            base_url=os.getenv("STORE_AGENT_LLM_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            model=os.getenv("STORE_AGENT_LLM_MODEL") or os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        )

    def stream(self, messages: list[dict[str, Any]],
               tools: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        """流式调用：逐块 yield `{"type": "content"|"tool_calls", ...}`。

        每轮都用 `stream: true`：`delta.content` 立刻转给前端，`delta.tool_calls`
        按 index 累积拼装——只有这样才能"边调工具边出字"。
        """
        payload: dict[str, Any] = {"model": self.model, "temperature": 0.2,
                                   "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        pending: dict[int, dict[str, Any]] = {}
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        yield {"type": "content", "text": delta["content"]}
                    for item in delta.get("tool_calls") or []:
                        index = int(item.get("index") or 0)
                        slot = pending.setdefault(
                            index, {"id": "", "type": "function",
                                    "function": {"name": "", "arguments": ""}})
                        if item.get("id"):
                            slot["id"] = item["id"]
                        function = item.get("function") or {}
                        if function.get("name"):
                            slot["function"]["name"] = function["name"]
                        if function.get("arguments"):
                            slot["function"]["arguments"] += function["arguments"]
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ToolCallingError(f"模型流式请求失败：{error}") from error
        if pending:
            yield {"type": "tool_calls", "calls": [pending[key] for key in sorted(pending)]}

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "temperature": 0.2, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:300]
            raise ToolCallingError(f"模型接口返回 {error.code}：{detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ToolCallingError(f"模型接口不可达：{error}") from error
        if not isinstance(body, dict) or not body.get("choices"):
            raise ToolCallingError("模型响应缺少 choices 字段")
        return body


class ToolCallingAgent:
    """把模型请求与只读工具集编排起来，并记录每一次工具调用。"""

    def __init__(
        self,
        tools: Any,
        responder: Any,
        *,
        tool_log: Optional[Any] = None,
        audit: Optional[Any] = None,
        tenant_id: str = "",
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        specs: tuple[ToolSpec, ...] = TOOL_SPECS,
    ):
        self.tools = tools
        self.responder = responder
        self.tool_log = tool_log
        self.audit = audit
        self.tenant_id = tenant_id
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.specs = specs

    # ---------------- 工具集与权限 ----------------

    def exposed_specs(self, user: dict[str, Any]) -> list[ToolSpec]:
        from auth import has_permission
        role = str(user.get("role") or "")
        return [spec for spec in self.specs if has_permission(role, spec.permission)]

    def exposed_tools(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        return [spec.as_openai_tool() for spec in self.exposed_specs(user)]

    def _spec(self, name: str) -> Optional[ToolSpec]:
        return next((spec for spec in self.specs if spec.name == name), None)

    # ---------------- 工具执行 ----------------

    def dispatch(self, name: str, arguments: dict[str, Any], user: dict[str, Any]) -> tuple[Any, Optional[str]]:
        """执行工具；返回 (结果, 错误)。未授权/未知/异常都变成错误信息回给模型，而不是抛出去。"""
        allowed = {spec.name for spec in self.exposed_specs(user)}
        if name not in allowed:
            return None, f"工具 {name} 不存在或当前身份无权调用"
        try:
            return self._invoke(name, arguments, user), None
        except Exception as exc:                                   # noqa: BLE001 - 工具错误要回给模型
            return None, f"工具 {name} 执行失败：{exc}"

    def _invoke(self, name: str, arguments: dict[str, Any], user: dict[str, Any]) -> Any:
        tools = self.tools
        include_financial = self._can(user, "metrics:read:financial")
        # ---- 第 11 期：门店侧与竞品问数 ----
        if name == "get_store_traffic":
            return tools.store_traffic(days=self._int(arguments.get("days"), 7, 1, 90))
        if name == "get_time_slots":
            return tools.time_slots(days=self._int(arguments.get("days"), 7, 1, 90))
        if name == "get_inventory_alerts":
            return tools.inventory_alerts(limit=self._int(arguments.get("limit"), 50, 1, 200))
        if name == "get_competitor_matches":
            return tools.competitor_matches()
        if name == "get_competitor_compare":
            raw_days = arguments.get("days")
            return tools.competitor_compare(
                days=self._int(raw_days, 28, 1, 365) if raw_days else None)
        if name == "search_knowledge":
            return tools.search_knowledge(user, query=str(arguments.get("query") or ""),
                                          base_id=arguments.get("base_id") or None,
                                          limit=self._int(arguments.get("limit"), 5, 1, 10))
        if name == "get_data_status":
            return tools.data_status()
        if name == "get_metric_summary":
            days = self._int(arguments.get("days"), 7, 1, 90)
            platform = arguments.get("platform") or None
            summary = tools.metric_summary(days=days, platform=platform, include_financial=include_financial)
            return {"window": {"start": summary.get("start_date"), "end": summary.get("end_date"), "days": days},
                    "has_data": summary.get("has_data"), "platforms": summary.get("platforms"),
                    "financial_included": summary.get("financial_included", True)}
        if name == "get_period_comparison":
            windows = arguments.get("windows") or ("day", "week")
            if isinstance(windows, str):
                windows = tuple(item for item in windows.split(",") if item)
            return tools.period_comparison(
                user, anchor=arguments.get("anchor") or "auto", windows=tuple(windows),
                platform=arguments.get("platform") or None, metric=arguments.get("metric") or None,
            )
        if name == "diagnose_store_change":
            windows = arguments.get("windows") or ("day", "week")
            if isinstance(windows, str):
                windows = tuple(item for item in windows.split(",") if item)
            return tools.diagnose_store_change(
                user, anchor=arguments.get("anchor") or "auto", windows=tuple(windows),
                platform=arguments.get("platform") or None, metric=arguments.get("metric") or None,
            )
        if name == "get_data_quality":
            quality = tools.data_quality(days=self._int(arguments.get("days"), 30, 1, 365))
            return {"window": quality["window"], "summary": quality["summary"],
                    "coverage": quality["coverage"], "anomalies": quality["anomalies"][:10]}
        if name == "get_data_gaps":
            quality = tools.data_quality(days=self._int(arguments.get("days"), 30, 1, 365))
            gaps = {key: item["missing_dates"] for key, item in quality["coverage"].items() if item["missing_dates"]}
            return {"days": quality["days"], "gaps": gaps, "has_gaps": bool(gaps)}
        if name == "get_tasks":
            tasks = tools.tasks_for(user, status=arguments.get("status") or None, limit=20)
            return [{"task_id": t["task_id"], "category": t["category"], "title": t["title"],
                     "status": t["status"], "priority": t["priority"], "assignee_id": t["assignee_id"],
                     "due_at": t["due_at"], "target": t.get("target") or {},
                     "reason": t.get("reason") or "", "evidence_required": t.get("evidence_required") or [],
                     "approval_required": t["approval_required"]} for t in tasks]
        if name == "get_task_detail":
            task_id = str(arguments.get("task_id") or "")
            if not task_id:
                raise ValueError("缺少 task_id")
            detail = tools.task_detail(task_id, user)
            return {"task_id": detail["task_id"], "category": detail["category"], "title": detail["title"],
                    "status": detail["status"], "priority": detail["priority"], "due_at": detail["due_at"],
                    "assignee_id": detail.get("assignee_id") or "", "target": detail.get("target") or {},
                    "reason": detail["reason"], "evidence_required": detail.get("evidence_required") or [],
                    "approval_required": detail.get("approval_required", False),
                    "evidence": [{"type": e["evidence_type"], "ref": e["object_ref"]}
                                 for e in detail.get("evidence", [])],
                    "events": [{"from": e["from_status"], "to": e["to_status"]} for e in detail.get("events", [])]}
        if name == "get_task_completion":
            return tools.task_completion(days=self._int(arguments.get("days"), 7, 1, 365))
        if name == "build_daily_report":
            report = tools.daily_report(arguments.get("date") or None)
            return {"date": report["date"], "report": report["report"], "summary": report["summary"]}
        raise ValueError(f"未实现的工具：{name}")

    def _can(self, user: dict[str, Any], permission: str) -> bool:
        from auth import has_permission
        return has_permission(str(user.get("role") or ""), permission)

    @staticmethod
    def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(maximum, number))

    # ---------------- 编排 ----------------

    def answer(
        self,
        message: str,
        user: dict[str, Any],
        history: Optional[list[dict[str, Any]]] = None,
        session_id: str = "",
    ) -> dict[str, Any]:
        specs = self.exposed_specs(user)
        tools_schema = [spec.as_openai_tool() for spec in specs]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT + f"当前身份：{user.get('name')}（{user.get('role_label')}）。"},
        ]
        for item in (history or [])[-6:]:
            role = item.get("message_role")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": str(item.get("content", ""))[:1000]})
        messages.append({"role": "user", "content": message})

        calls: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        iterations = 0
        for iterations in range(1, self.max_iterations + 1):
            completion = self.responder.complete(messages, tools_schema)
            choice = completion["choices"][0].get("message") or {}
            tool_calls = choice.get("tool_calls") or []
            if not tool_calls:
                return {"answer": str(choice.get("content") or "").strip() or "（模型没有返回内容）",
                        "calls": calls, "sources": sources, "iterations": iterations,
                        "mode": "tool_calling", "maxed_out": False}
            messages.append({"role": "assistant", "content": choice.get("content") or "",
                             "tool_calls": tool_calls})
            for call in tool_calls:
                if len(calls) >= self.max_tool_calls:
                    messages.append({"role": "user",
                                     "content": "工具调用次数已达上限，请基于已有信息作答。"})
                    break
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("参数必须是 JSON 对象")
                except (json.JSONDecodeError, ValueError) as exc:
                    arguments = {}
                    result, error = None, f"工具参数无法解析：{exc}"
                else:
                    result, error = self.dispatch(name, arguments, user)
                payload = {"ok": error is None, "data": result, "error": error}
                messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                                 "content": json.dumps(payload, ensure_ascii=False, default=str)})
                calls.append({"name": name, "arguments": arguments, "ok": error is None, "error": error})
                sources.append({"type": "tool_call",
                                "label": f"工具 {name}（{json.dumps(arguments, ensure_ascii=False)}）",
                                "tool": name, "ok": error is None})
                self._record(user, name, arguments, payload, session_id)
        return {"answer": "（工具调用次数达到上限，我先给出已知信息；如需更完整的结论请缩小问题范围。）",
                "calls": calls, "sources": sources, "iterations": iterations,
                "mode": "tool_calling", "maxed_out": True}

    def answer_stream(
        self,
        message: str,
        user: dict[str, Any],
        history: Optional[list[dict[str, Any]]] = None,
        session_id: str = "",
    ) -> Iterator[dict[str, Any]]:
        """流式版 `answer()`：逐个 yield 事件，供 SSE 使用。

        事件类型：`status`（正在调用某个工具）/ `delta`（答案增量）/ `done`（收尾）/ `error`。
        编排逻辑与 `answer()` 一致，只是把"等模型整段回完"换成"边收边发"。
        """
        specs = self.exposed_specs(user)
        tools_schema = [spec.as_openai_tool() for spec in specs]
        messages: list[dict[str, Any]] = [
            {"role": "system",
             "content": SYSTEM_PROMPT + f"当前身份：{user.get('name')}（{user.get('role_label')}）。"},
        ]
        for item in (history or [])[-6:]:
            role = item.get("message_role")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": str(item.get("content", ""))[:1000]})
        messages.append({"role": "user", "content": message})

        calls: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        iterations = 0
        try:
            for iterations in range(1, self.max_iterations + 1):
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for event in self.responder.stream(messages, tools_schema):
                    if event["type"] == "content":
                        text_parts.append(event["text"])
                        yield {"type": "delta", "text": event["text"]}
                    else:
                        tool_calls = event["calls"]
                if not tool_calls:
                    yield {"type": "done", "text": "".join(text_parts).strip() or "（模型没有返回内容）",
                           "calls": calls, "sources": sources, "iterations": iterations,
                           "mode": "tool_calling", "maxed_out": False}
                    return
                messages.append({"role": "assistant", "content": "".join(text_parts) or "",
                                 "tool_calls": tool_calls})
                for call in tool_calls:
                    if len(calls) >= self.max_tool_calls:
                        messages.append({"role": "user",
                                         "content": "工具调用次数已达上限，请基于已有信息作答。"})
                        break
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    yield {"type": "status", "text": f"正在查询：{name}"}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError("参数必须是 JSON 对象")
                    except (json.JSONDecodeError, ValueError) as exc:
                        arguments, result, error = {}, None, f"工具参数无法解析：{exc}"
                    else:
                        result, error = self.dispatch(name, arguments, user)
                    payload = {"ok": error is None, "data": result, "error": error}
                    messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                                     "content": json.dumps(payload, ensure_ascii=False, default=str)})
                    calls.append({"name": name, "arguments": arguments, "ok": error is None,
                                  "error": error})
                    sources.append({"type": "tool_call",
                                    "label": f"工具 {name}（{json.dumps(arguments, ensure_ascii=False)}）",
                                    "tool": name, "ok": error is None})
                    self._record(user, name, arguments, payload, session_id)
            yield {"type": "done",
                   "text": "（工具调用次数达到上限，我先给出已知信息；如需更完整的结论请缩小问题范围。）",
                   "calls": calls, "sources": sources, "iterations": iterations,
                   "mode": "tool_calling", "maxed_out": True}
        except Exception as exc:                    # noqa: BLE001 - 流里出错要转成事件，不能抛
            yield {"type": "error", "text": f"模型暂时不可用：{exc}",
                   "calls": calls, "sources": sources}

    def _record(self, user: dict[str, Any], name: str, arguments: dict[str, Any],
                payload: dict[str, Any], session_id: str) -> None:
        """工具调用留痕 + 审计（旁路：失败不影响回答）。"""
        if self.tool_log is not None:
            try:
                self.tool_log.record(
                    store_id=str(getattr(self.tools, "store_id", "")), tool_name=f"llm:{name}",
                    mode="read", actor_id=str(user.get("user_id") or ""), session_id=session_id or None,
                    input_payload=arguments,
                    output_payload={"ok": payload["ok"], "keys": sorted((payload["data"] or {}).keys())
                                    if isinstance(payload["data"], dict) else None,
                                    "error": payload.get("error")},
                    status="success" if payload["ok"] else "failed",
                )
            except Exception:                                      # noqa: BLE001 - 留痕失败不影响业务
                pass
        if self.audit is not None:
            try:
                self.audit.record_audit(
                    event_type="llm_tool_call", actor_type="user", actor_id=str(user.get("user_id") or ""),
                    tenant_id=self.tenant_id,
                    store_id=str(getattr(self.tools, "store_id", "")),
                    object_type="tool", object_id=name,
                    after={"arguments": arguments, "ok": payload["ok"]},
                )
            except Exception:                                      # noqa: BLE001
                pass
