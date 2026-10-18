# -*- coding: utf-8 -*-
"""批次 13：大模型工具调用（工具集权限过滤、调度容错、编排循环、留痕与审计）。

模型侧没有真实凭据：用**本地 OpenAI 兼容桩服务**验证真实 HTTP 往返；
编排逻辑用脚本化响应覆盖（多轮、参数非法、次数上限）。
"""
import json
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_tools import StoreTools
from auth import AuthStore, permissions_for
from governance import ToolRunLog
from llm_agent import (
    TOOL_SPECS,
    OpenAIToolResponder,
    ToolCallingAgent,
    ToolCallingError,
)
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import build_snapshot, load_config
from store_agent import StoreOperationsAgent
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
          "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"

MANAGER = {"user_id": "u-mgr", "name": "武汉狮子座店店长", "role": "manager", "role_label": "店长"}
EMPLOYEE = {"user_id": "u-staff", "name": "员工小李", "role": "employee", "role_label": "店员"}
OWNER = {"user_id": "u-owner", "name": "店主老王", "role": "owner", "role_label": "店主"}


class ScriptedResponder:
    """按脚本依次返回响应；记录每次请求，便于断言"发了什么"。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": [dict(t) for t in tools]})
        if not self.responses:
            raise ToolCallingError("脚本用尽")
        return self.responses.pop(0)


def tool_call_response(name, arguments="{}", call_id="call_1"):
    return {"choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": call_id, "type": "function",
                                                     "function": {"name": name, "arguments": arguments}}]}}]}


def content_response(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class LlmStubServer:
    """OpenAI 兼容的本地桩：按队列返回响应，并记录收到的请求体。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                stub.requests.append({"path": self.path,
                                      "body": json.loads(self.rfile.read(length).decode("utf-8") or "{}"),
                                      "authorization": self.headers.get("Authorization")})
                payload = json.dumps(stub.responses.pop(0) if stub.responses else content_response("（桩无响应）")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = dict(load_config(ROOT / "store_config.json"), tenant_id=TENANT)
        self.metrics = PlatformMetricStore(self.root / "m.db", tenant_id=TENANT)
        self.tasks = TaskStore(self.root / "s.db", tenant_id=TENANT)
        self.auth = AuthStore(self.root / "s.db", tenant_id=TENANT)
        self.tool_log = ToolRunLog(self.root / "s.db", tenant_id=TENANT)
        self.tools = StoreTools(self.config, self.metrics, self.tasks, config_path=ROOT / "store_config.json")
        today, yesterday = date.today(), date.today() - timedelta(days=1)

        for platform, base in (("meituan", 100), ("douyin", 80)):
            path = self.root / f"{platform}.csv"
            path.write_bytes((HEADER + f"{yesterday.isoformat()},{platform},{STORE},{base},{base // 5},4,3,300,0,1,0,4.8\n"
                              + f"{today.isoformat()},{platform},{STORE},{base + 10},{base // 5},4,3,320,0,1,0,4.8\n").encode("utf-8"))
            self.metrics.import_csv(path, STORE)
        pkg = self.root / "p.csv"
        pkg.write_bytes((PKG + f"{today.isoformat()},meituan,{STORE},MT-1632214010,AI全周小包2h,168,5,4,1200\n").encode("utf-8"))
        self.metrics.import_packages_csv(pkg, STORE)
        plan = generate_daily_plan(build_snapshot(self.config, self.metrics, today, task_store=self.tasks))
        self.tasks.save_plan(plan, STORE)
        self.task_id = plan["tasks"][0]["task_id"]

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, responder, **kwargs):
        return ToolCallingAgent(self.tools, responder, tool_log=self.tool_log, audit=self.auth,
                                tenant_id=TENANT, **kwargs)


class ToolSetTests(Base):
    def test_tool_specs_are_filtered_by_role(self):
        manager = {spec.name for spec in self.agent(ScriptedResponder([])).exposed_specs(MANAGER)}
        employee = {spec.name for spec in self.agent(ScriptedResponder([])).exposed_specs(EMPLOYEE)}
        owner = {spec.name for spec in self.agent(ScriptedResponder([])).exposed_specs(OWNER)}
        all_names = {spec.name for spec in TOOL_SPECS}
        self.assertEqual(manager, all_names)
        self.assertEqual(owner, all_names)
        self.assertTrue({"get_data_quality", "get_data_gaps", "build_daily_report"}.isdisjoint(employee))
        self.assertIn("get_metric_summary", employee)
        self.assertIn("get_tasks", employee)

    def test_unknown_and_unauthorized_tools_are_rejected(self):
        agent = self.agent(ScriptedResponder([]))
        result, error = agent.dispatch("delete_everything", {}, MANAGER)
        self.assertIsNone(result)
        self.assertIn("不存在或当前身份无权", error)
        result, error = agent.dispatch("build_daily_report", {}, EMPLOYEE)
        self.assertIsNone(result)
        self.assertIn("无权", error)

    def test_tool_errors_are_returned_not_raised(self):
        agent = self.agent(ScriptedResponder([]))
        result, error = agent.dispatch("get_task_detail", {"task_id": "not-exist"}, MANAGER)
        self.assertIsNone(result)
        self.assertIn("执行失败", error)

    def test_employee_metric_summary_has_no_financial_fields(self):
        agent = self.agent(ScriptedResponder([]))
        manager_result, _ = agent.dispatch("get_metric_summary", {"days": 7}, MANAGER)
        employee_result, _ = agent.dispatch("get_metric_summary", {"days": 7}, EMPLOYEE)
        self.assertIn("gmv", manager_result["platforms"]["meituan"]["metrics"])
        self.assertNotIn("gmv", employee_result["platforms"]["meituan"]["metrics"])
        self.assertFalse(employee_result["financial_included"])

    def test_arguments_are_clamped(self):
        agent = self.agent(ScriptedResponder([]))
        result, error = agent.dispatch("get_metric_summary", {"days": 9999}, MANAGER)
        self.assertIsNone(error)
        self.assertEqual(result["window"]["days"], 90)


class LoopTests(Base):
    def test_full_loop_against_openai_compatible_stub(self):
        stub = LlmStubServer([
            tool_call_response("get_metric_summary", '{"days": 7}'),
            content_response("近 7 天曝光 210、支付 8 单，整体平稳。"),
        ])
        try:
            responder = OpenAIToolResponder("test-key", stub.base_url, "stub-model")
            agent = self.agent(responder)
            outcome = agent.answer("最近生意怎么样？", MANAGER, session_id="s-llm")
            self.assertEqual(outcome["mode"], "tool_calling")
            self.assertEqual(outcome["iterations"], 2)
            self.assertIn("整体平稳", outcome["answer"])
            self.assertEqual([call["name"] for call in outcome["calls"]], ["get_metric_summary"])
            self.assertTrue(outcome["calls"][0]["ok"])

            first, second = stub.requests
            self.assertEqual(first["path"], "/v1/chat/completions")
            self.assertEqual(first["authorization"], "Bearer test-key")
            self.assertIn("tools", first["body"])
            exposed = {tool["function"]["name"] for tool in first["body"]["tools"]}
            self.assertIn("build_daily_report", exposed)          # 店长拿到全部工具
            self.assertEqual(first["body"]["messages"][-1]["content"], "最近生意怎么样？")
            # 第二次请求必须把工具结果回灌给模型
            tool_messages = [m for m in second["body"]["messages"] if m.get("role") == "tool"]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn('"impressions"', tool_messages[0]["content"])
            self.assertIn("gmv", tool_messages[0]["content"])     # 店长可见财务口径

            runs = self.tool_log.list(STORE)
            self.assertEqual([r["tool_name"] for r in runs], ["llm:get_metric_summary"])
            self.assertEqual(runs[0]["mode"], "read")
            self.assertEqual(runs[0]["input_redacted"], {"days": 7})
            events = [row["event_type"] for row in self.auth.list_audit(store_id=STORE, limit=10)]
            self.assertIn("llm_tool_call", events)
        finally:
            stub.stop()

    def test_employee_loop_hides_financial_and_excludes_report_tools(self):
        stub = LlmStubServer([
            tool_call_response("get_metric_summary", '{"days": 7}'),
            content_response("曝光 210。"),
        ])
        try:
            agent = self.agent(OpenAIToolResponder("k", stub.base_url, "stub-model"))
            agent.answer("最近怎么样", EMPLOYEE)
            tools_sent = {tool["function"]["name"] for tool in stub.requests[0]["body"]["tools"]}
            self.assertNotIn("build_daily_report", tools_sent)
            tool_message = [m for m in stub.requests[1]["body"]["messages"] if m.get("role") == "tool"][0]
            self.assertNotIn("gmv", tool_message["content"])
        finally:
            stub.stop()

    def test_iteration_and_tool_call_limits(self):
        responses = [tool_call_response("get_data_status", "{}", call_id=f"call_{i}") for i in range(10)]
        responder = ScriptedResponder(responses)
        agent = self.agent(responder, max_iterations=3)
        outcome = agent.answer("一直调用工具", MANAGER)
        self.assertTrue(outcome["maxed_out"])
        self.assertEqual(outcome["iterations"], 3)
        self.assertIn("上限", outcome["answer"])
        self.assertLessEqual(len(outcome["calls"]), agent.max_tool_calls)

    def test_invalid_arguments_are_reported_to_model(self):
        responder = ScriptedResponder([
            tool_call_response("get_metric_summary", "{不是JSON"),
            content_response("参数有问题，我换个说法：需要统计近 7 天吗？"),
        ])
        outcome = self.agent(responder).answer("查一下", MANAGER)
        self.assertEqual(outcome["calls"][0]["ok"], False)
        self.assertIn("参数无法解析", responder.requests[1]["messages"][-1]["content"])
        self.assertIn("换个说法", outcome["answer"])

    def test_tool_failure_is_fed_back_and_model_continues(self):
        responder = ScriptedResponder([
            tool_call_response("get_task_detail", '{"task_id": "not-exist"}'),
            content_response("没有找到这个任务。"),
        ])
        outcome = self.agent(responder).answer("任务 X 怎么样了", MANAGER)
        self.assertFalse(outcome["calls"][0]["ok"])
        self.assertEqual(outcome["iterations"], 2)
        self.assertIn("没有找到", outcome["answer"])
        runs = self.tool_log.list(STORE)
        self.assertEqual(runs[0]["status"], "failed")

    def test_responder_rejects_malformed_completion(self):
        """桩返回缺 choices 的响应时，客户端必须报错而不是当成正常回答。"""
        stub = LlmStubServer([{"error": "boom"}])
        try:
            responder = OpenAIToolResponder("k", stub.base_url, "stub-model")
            with self.assertRaises(ToolCallingError) as error:
                responder.complete([{"role": "user", "content": "hi"}], [])
            self.assertIn("choices", str(error.exception))
        finally:
            stub.stop()


class IntegrationTests(Base):
    def test_store_agent_uses_tool_agent_for_open_questions(self):
        responder = ScriptedResponder([content_response("我从工具里没查到相关数据，建议先导入报表。")])
        tool_agent = self.agent(responder)
        agent = StoreOperationsAgent.from_tools(self.config, self.tools, None, tool_agent)
        result = agent.reply("帮我看看这周有没有值得关注的异常", "manager-001")
        self.assertEqual(result["intent"], "llm_tools")
        self.assertIn("建议先导入报表", result["answer"])
        self.assertTrue(any(src.get("type") in ("tool_call", "rule", "store_config") for src in result["sources"]))

    def test_rule_intents_still_win_over_llm(self):
        responder = ScriptedResponder([content_response("不应被调用")])
        agent = StoreOperationsAgent.from_tools(self.config, self.tools, None, self.agent(responder))
        result = agent.reply("我今天有什么任务？", "manager-001")
        self.assertEqual(result["intent"], "list_tasks")
        self.assertEqual(responder.requests, [])

    def test_tool_agent_failure_degrades_gracefully(self):
        class Exploding:
            def answer(self, *args, **kwargs):
                raise RuntimeError("模型挂了")

        agent = StoreOperationsAgent.from_tools(self.config, self.tools, None, Exploding())
        result = agent.reply("随便聊聊", "manager-001")
        self.assertEqual(result["intent"], "llm_unavailable")
        self.assertIn("大模型暂时不可用", result["answer"])


if __name__ == "__main__":
    unittest.main()
