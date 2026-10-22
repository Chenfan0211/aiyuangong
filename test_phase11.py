# -*- coding: utf-8 -*-
"""第 11 期：竞品四维对比 + 对话问数能力（含 SSE 契约）的正式回归用例。

这些用例是把实施期间用临时脚本 + Live 验证过的结论固化下来：
根因三处、5 个问数工具、四维对比算法、竞品规则、SSE 事件契约、.env 测试隔离。
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from agent_tools import StoreTools
from app_fastapi import create_app
from auth import AuthStore
from competitor import CompetitorStore, _percentile, _price_bands
from llm_agent import TOOL_SPECS, ToolCallingAgent
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import build_snapshot, load_config
from store_agent import (
    StoreOperationsAgent,
    is_high_risk_request,
    load_env_file,
)
from store_ops_data import StoreOpsDataStore
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE_ID = "WH-LION-001"
PASSWORD = "ShiZiZuo-2026-store"
AREA_HEADER = "store_id,area_id,area_name,area_type,note\n"
CSTORE_HEADER = "store_id,competitor_id,competitor_name,area_id,note\n"
OFFER_HEADER = ("date,store_id,competitor_id,package_name,price,activity,activity_note,"
                "source_method,source_note,sampled_by\n")
USER = {"user_id": "u-test", "name": "店长张三", "role": "owner", "role_label": "店主",
        "permissions": ["agent:chat", "metrics:read", "metrics:read:financial", "quality:read",
                        "report:read", "tasks:read:store", "tasks:read:own"]}


class HighRiskGateTests(unittest.TestCase):
    """高风险闸门：从"提及即拦"改为"执行诉求"（第 11 期根因之一）。"""

    CASES = (
        ("同商圈竞品有没有降价", False), ("帮我把套餐降价 10 元", True),
        ("差评怎么回复", False), ("帮我把这条差评回复掉", True),
        ("为什么价格比竞品高", False), ("把套餐价格下调 5 元", True),
        ("竞品上架了什么新套餐", False), ("帮我把这个套餐上架", True),
        ("这个月赔付花了多少", False), ("给他赔 50 元", True),
        ("数据齐不齐", False), ("今天要做什么", False),
    )

    def test_question_vs_request(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(is_high_risk_request(text), expected)


class ProbeTests(unittest.TestCase):
    """把 8 句实证探针转正：这些是"答非所问"的原始复现句。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(ROOT / "store_config.json")
        self.metrics = PlatformMetricStore(self.root / "m.db")
        self.ops = StoreOpsDataStore(self.root / "ops.db")
        self.tasks = TaskStore(self.root / "ops.db")
        self.tools = StoreTools(self.config, self.metrics, self.tasks)
        self.agent = StoreOperationsAgent.from_tools(self.config, self.tools, None, None)
        self.agent.snapshot = build_snapshot(self.config, self.metrics, "2026-10-21",
                                             task_store=self.tasks, ops_store=self.ops)

    def tearDown(self):
        self.temp.cleanup()

    def reply(self, question):
        return self.agent.reply(question, "u-test", [], USER)

    def test_correct_intents(self):
        for question, expected in (
            ("今天要做什么", "list_tasks"),
            ("为什么曝光掉了", "store_diagnosis"),
            ("帮我把套餐降价 10 元", "approval_required"),
            ("差评怎么回复", "reputation_help"),
        ):
            with self.subTest(question=question):
                self.assertEqual(self.reply(question)["intent"], expected)

    def test_diagnosis_is_not_hijacked_by_why(self):
        """原来的 bug：句子含"为什么"就会被 explain_tasks 抢走、答成"任务为什么生成"。"""
        result = self.reply("为什么昨天订单少了")
        self.assertEqual(result["intent"], "store_diagnosis")
        self.assertNotIn("任务为什么生成", result["answer"])

    def test_competitor_question_is_not_blocked_as_high_risk(self):
        """原来的 bug：提到"降价"就被当成要改价，回"我不会直接执行改价/赔付"。"""
        result = self.reply("同商圈竞品有没有降价")
        self.assertEqual(result["intent"], "capability_help")
        self.assertFalse(result["requires_approval"])
        self.assertIn("竞品", result["answer"])

    def test_fallback_guides_by_topic(self):
        """兜底不再是无关的固定清单，而是按主题说明"要怎样才能答"。"""
        for question, keyword in (("库存还够不够", "库存与物料"),
                                  ("哪个时段生意最差", "时段与客流")):
            with self.subTest(question=question):
                result = self.reply(question)
                self.assertEqual(result["intent"], "capability_help")
                self.assertIn(keyword, result["answer"])
                self.assertIn("没有完全确定", result["answer"])


class CompetitorCompareTests(unittest.TestCase):
    """四维对比：分位/分档算法、样本不足、只有 confirmed 参与、按商圈分组。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ops = StoreOpsDataStore(self.root / "ops.db")
        self.store = CompetitorStore(self.root / "ops.db", ops_store=self.ops)
        self.config = {"store_id": STORE_ID,
                       "package_costs": [{"platform": "meituan", "package_id": "MT-1",
                                          "sale_price": 198, "variable_cost": 137}],
                       "rules": {"competitor_price_band_size": 20, "competitor_window_days": 28,
                                 "competitor_min_samples": 3, "competitor_area_shift_percent": 10}}
        self.write = lambda name, header, body: (
            (self.root / name).write_text(header + body, encoding="utf-8"),
            self.root / name)[1]
        self.ops.import_trade_area_csv(
            self.write("area.csv", AREA_HEADER, f"{STORE_ID},A01,光谷步行街,商圈,\n"), STORE_ID)
        self.store.import_competitor_store_csv(
            self.write("cs.csv", CSTORE_HEADER, f"{STORE_ID},CMP-1,A店,A01,\n"), STORE_ID)

    def tearDown(self):
        self.temp.cleanup()

    def offers(self, rows):
        body = "".join(f"{d},{STORE_ID},CMP-1,{name},{price},,,平台前台,公开页面,小李\n"
                       for d, name, price in rows)
        self.store.import_competitor_offer_csv(self.write("o.csv", OFFER_HEADER, body), STORE_ID)

    def test_percentile_edges_and_ties(self):
        self.assertEqual(_percentile([100], 0.5), 100)
        self.assertEqual(_percentile([50, 50, 50, 50], 0.75), 50)
        values = [68, 68, 68, 68, 148, 148, 148, 148, 158, 163, 168, 173]
        self.assertEqual(_percentile(values, 0.25), 68)
        self.assertEqual(_percentile(values, 0.50), 148)
        self.assertEqual(_percentile(values, 0.75), 159.25)

    def test_price_bands_are_left_closed_right_open(self):
        self.assertEqual(_price_bands([19.99, 20.0, 39.99, 40.0, 199.5], 20),
                         [{"low": 0, "high": 20, "count": 1},
                          {"low": 20, "high": 40, "count": 2},
                          {"low": 40, "high": 60, "count": 1},
                          {"low": 180, "high": 200, "count": 1}])

    def test_insufficient_samples_do_not_produce_conclusions(self):
        self.offers([("2026-10-19", "双人套餐", 168), ("2026-10-20", "双人套餐", 170)])
        result = self.store.compare(STORE_ID, self.config, today=date(2026, 10, 21))
        self.assertTrue(result["price"]["insufficient"])       # 2 < min_samples 3
        self.assertIsNone(result["price"]["p50"])
        self.assertEqual(result["price"]["bands"], [])
        self.assertTrue(result["area_change"]["insufficient"])
        self.assertFalse(result["area_change"]["shifted"])

    def test_only_confirmed_matches_enter_the_comparison(self):
        self.offers([("2026-10-17", "双人套餐", 168),
                      ("2026-10-19", "双人套餐", 168),
                      ("2026-10-21", "双人套餐", 168)])
        self.store.upsert_match(STORE_ID, platform="meituan", package_id="MT-1",
                                competitor_id="CMP-1", competitor_package_name="双人套餐",
                                status="pending", actor_id="u-1")
        result = self.store.compare(STORE_ID, self.config, today=date(2026, 10, 21))
        self.assertEqual(result["packages"]["confirmed_match_count"], 0)
        self.assertEqual(result["packages"]["matches"], [])
        self.assertEqual(len(result["unmatched_competitor_packages"]), 1)
        self.store.upsert_match(STORE_ID, platform="meituan", package_id="MT-1",
                                competitor_id="CMP-1", competitor_package_name="双人套餐",
                                status="confirmed", actor_id="u-1")
        result = self.store.compare(STORE_ID, self.config, today=date(2026, 10, 21))
        self.assertEqual(result["packages"]["confirmed_match_count"], 1)
        row = result["packages"]["matches"][0]
        self.assertEqual(row["area_id"], "A01")                # 规则要按商圈归集
        self.assertEqual(row["gap"], 30.0)                     # 198 - 168
        self.assertEqual(result["by_area"][0]["area_id"], "A01")


class ToolRegistrationTests(unittest.TestCase):
    """5 个问数工具必须注册进模型可调用清单，并遵守权限过滤。"""

    NEW = {"get_store_traffic", "get_time_slots", "get_inventory_alerts",
           "get_competitor_matches", "get_competitor_compare"}

    def test_all_tools_registered(self):
        names = {spec.name for spec in TOOL_SPECS}
        self.assertTrue(self.NEW <= names, self.NEW - names)
        self.assertEqual(len(TOOL_SPECS), 16)

    def test_tools_are_permission_filtered(self):
        self.assertTrue(self.NEW <= {spec.name for spec in TOOL_SPECS
                                     if spec.permission == "metrics:read"})
        employee = {"role": "employee", "role_label": "店员"}
        manager = {"role": "manager", "role_label": "店长"}
        tools = StoreTools(load_config(ROOT / "store_config.json"),
                           PlatformMetricStore(Path(tempfile.mkdtemp()) / "m.db"),
                           TaskStore(Path(tempfile.mkdtemp()) / "o.db"))
        agent = ToolCallingAgent(tools, responder=None)
        self.assertTrue(self.NEW <= {spec.name for spec in agent.exposed_specs(manager)})
        # 店员没有 quality:read / report:read 等，但门店侧与竞品问数沿用 metrics:read
        self.assertTrue(self.NEW <= {spec.name for spec in agent.exposed_specs(employee)})


class SseContractTests(unittest.TestCase):
    """SSE 契约：事件顺序、done 字段、无 Key 时同一路径、模型失败降级。"""

    def setUp(self):
        old = os.environ.pop("STORE_AGENT_MFA_ENFORCE", None)
        self.addCleanup(self._restore_env, "STORE_AGENT_MFA_ENFORCE", old)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = self.root / "store_config.json"
        config.write_text((ROOT / "store_config.json").read_text(encoding="utf-8"), encoding="utf-8")
        AuthStore(self.root / "ops.db", tenant_id="LPL-001").create_user(
            "owner01", PASSWORD, "店主", "owner", "LPL-001", STORE_ID)
        self.app = create_app(config_path=config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                              evidence_dir=self.root / "evidence",
                              import_inbox=self.root / "inbox")
        self.client = TestClient(self.app)
        response = self.client.post("/api/auth/login",
                                    json={"username": "owner01", "password": PASSWORD})
        self.token = response.json()["token"]
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def _restore_env(name, old):
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old

    def read_events(self, question):
        with self.client.stream("POST", "/api/chat/stream",
                                headers={"Authorization": "Bearer " + self.token},
                                json={"session_id": "sse-1", "message": question}) as response:
            self.assertEqual(response.status_code, 200, response.read())
            self.assertIn("text/event-stream", response.headers["content-type"])
            return [json.loads(line[6:]) for line in response.iter_lines()
                    if line.startswith("data:")]

    def test_rules_path_streams_same_contract(self):
        """没配 Key 时也走同一接口：delta 若干 + done，字段齐全。"""
        events = self.read_events("今天要做什么")
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[-1], "done")
        self.assertIn("delta", kinds)
        done = events[-1]
        for key in ("mode", "llm_enabled", "intent", "sources", "degraded"):
            self.assertIn(key, done)
        self.assertEqual(done["mode"], "rules")
        self.assertFalse(done["llm_enabled"])

    def test_high_risk_question_never_reaches_the_model(self):
        events = self.read_events("帮我把套餐降价 10 元")
        done = events[-1]
        self.assertEqual(done["intent"], "approval_required")
        self.assertEqual(done["mode"], "rules")      # 闸门产出必须标 rules，不能冒充模型
        self.assertTrue(done["requires_approval"])
        self.assertNotIn("status", [event["type"] for event in events])

    def test_model_path_emits_status_then_delta_then_done(self):
        """用桩模拟模型路径：必须先 status、再 delta*、最后 done。"""
        class StubAgent:
            def answer_stream(self, text, user, history):
                yield {"type": "status", "text": "正在查询：get_inventory_alerts"}
                yield {"type": "delta", "text": "库存"}
                yield {"type": "delta", "text": "充足"}
                yield {"type": "done", "text": "库存充足", "calls": [], "sources": [],
                       "iterations": 1}

        ctx = self.app.state.ctx if hasattr(self.app, "state") else None
        agent = StoreOperationsAgent.from_tools(
            load_config(ROOT / "store_config.json"),
            StoreTools(load_config(ROOT / "store_config.json"),
                       PlatformMetricStore(self.root / "m2.db"),
                       TaskStore(self.root / "o2.db")),
            None, StubAgent())
        events = list(agent.reply_stream("库存还够不够", "u-1", [], USER))
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[0], "status")
        self.assertEqual(kinds[-1], "done")
        self.assertIn("delta", kinds)
        self.assertEqual(events[-1]["mode"], "tools")
        self.assertFalse(events[-1]["degraded"])
        self.assertTrue(events[-1]["text"].startswith("库存充足"))

    def test_model_failure_degrades_with_rules_answer(self):
        class Exploding:
            def answer_stream(self, text, user, history):
                yield {"type": "error", "text": "模型暂时不可用：boom"}

        agent = StoreOperationsAgent.from_tools(
            load_config(ROOT / "store_config.json"),
            StoreTools(load_config(ROOT / "store_config.json"),
                       PlatformMetricStore(self.root / "m3.db"),
                       TaskStore(self.root / "o3.db")),
            None, Exploding())
        events = list(agent.reply_stream("今天要做什么", "u-1", [], USER))
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertTrue(done["degraded"])              # 如实标记降级
        self.assertEqual(done["mode"], "rules")
        self.assertIn("delta", [event["type"] for event in events])


class EnvIsolationTests(unittest.TestCase):
    """本地 .env 绝不能影响测试：否则会去打真实模型、产生费用且结果不确定。"""

    def test_dotenv_is_not_loaded_under_unittest(self):
        before = os.environ.get("STORE_AGENT_LLM_API_KEY")
        self.assertEqual(load_env_file(), 0)
        self.assertEqual(os.environ.get("STORE_AGENT_LLM_API_KEY"), before)


if __name__ == "__main__":
    unittest.main()