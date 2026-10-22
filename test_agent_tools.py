# -*- coding: utf-8 -*-
"""批次 6：意图规则表、槽位解析与只读工具集的测试。"""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from agent_tools import PermissionDenied, StoreTools
from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import build_snapshot
from store_agent import INTENT_RULES, StoreOperationsAgent
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE_ID = "WH-LION-001"
STORE_HEADER = ("date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,"
                "gmv,refunds,review_count,negative_review_count,avg_rating\n")
PKG_HEADER = "date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv\n"
REV_HEADER = "review_id,platform,store_id,rating,created_at,status,category,replied_at\n"

MANAGER = {"user_id": "manager-001", "name": "武汉狮子座店店长", "role": "manager", "role_label": "店长"}
STAFF = {"user_id": "staff-001", "name": "员工小李", "role": "employee", "role_label": "店员"}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metrics = PlatformMetricStore(self.root / "metrics.db")
        self.tasks = TaskStore(self.root / "ops.db")
        self.config = json.loads((ROOT / "store_config.json").read_text(encoding="utf-8"))
        self.tools = StoreTools(self.config, self.metrics, self.tasks, config_path=ROOT / "store_config.json")
        self.today = date.today()
        self.yesterday = self.today - timedelta(days=1)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, header, body):
        path = self.root / name
        path.write_bytes((header + body).encode("utf-8"))
        return path

    def seed(self, with_negative=True):
        self.metrics.import_csv(self.write("m.csv", STORE_HEADER,
            f"{self.yesterday},meituan,{STORE_ID},100,20,5,4,300,0,1,0,4.8\n"
            f"{self.today},meituan,{STORE_ID},110,22,5,4,320,0,1,0,4.8\n"), STORE_ID)
        self.metrics.import_csv(self.write("d.csv", STORE_HEADER,
            f"{self.yesterday},douyin,{STORE_ID},80,15,4,3,200,0,1,0,4.7\n"
            f"{self.today},douyin,{STORE_ID},85,16,4,3,210,0,1,0,4.7\n"), STORE_ID)
        self.metrics.import_packages_csv(self.write("p.csv", PKG_HEADER,
            f"{self.today},meituan,{STORE_ID},MT-1632214010,AI全周小包2h,168,5,4,1200\n"), STORE_ID)
        body = f"MT-1001,meituan,{STORE_ID},5,{self.today} 10:00:00,replied,环境安静,{self.today} 11:00:00\n"
        if with_negative:
            body = (f"MT-1024,meituan,{STORE_ID},2,{self.today} 09:15:00,open,服务体验,\n" + body)
        self.metrics.import_reviews_csv(self.write("r.csv", REV_HEADER, body), STORE_ID)
        snapshot = build_snapshot(self.config, self.metrics, self.today, task_store=self.tasks)
        return self.tasks.save_plan(generate_daily_plan(snapshot), STORE_ID)


class IntentTableTests(unittest.TestCase):
    def test_intent_matrix(self):
        cases = (
            ("数据齐不齐？", "data_quality"),
            ("数据质量怎么样", "data_quality"),
            ("哪天缺数", "data_gap"),
            ("任务完成情况", "task_progress"),
            ("这些任务为什么生成？", "explain_tasks"),
            ("任务完成需要什么证据？", "task_help"),
            ("这些任务具体要做什么？", "task_help"),
            ("给我一个提升曝光的方案", "advice"),
            ("差评应该怎么处理？", "reputation_help"),
            ("套餐转化怎么优化", "package_help"),
            ("分析最近7天美团和抖音数据", "platform_metrics"),
            ("美团曝光多少", "platform_metrics"),
            ("我今天有什么任务？", "list_tasks"),
            ("今天做什么", "list_tasks"),
            ("生成今日日报", "daily_report"),
            ("最近年轻顾客喜欢什么活动形式？", None),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(StoreOperationsAgent.match_intent(text), expected)

    def test_rules_are_declared_as_data(self):
        intents = [rule.intent for rule in INTENT_RULES]
        self.assertEqual(len(intents), len(set(intents)), "意图不应重复声明")
        self.assertIn("daily_report", intents)
        self.assertIn("data_quality", intents)
        # 越具体的意图必须排在 list_tasks 之前，否则会被"任务/今天"抢走
        self.assertLess(intents.index("explain_tasks"), intents.index("list_tasks"))
        self.assertLess(intents.index("task_help"), intents.index("list_tasks"))
        self.assertLess(intents.index("advice"), intents.index("platform_metrics"))
        self.assertLess(intents.index("data_quality"), intents.index("platform_metrics"))

    def test_slot_parsing(self):
        cases = (
            ("近30天美团曝光", {"days": 30, "platform": "meituan", "metric": "impressions"}),
            ("最近7天抖音核销", {"days": 7, "platform": "douyin", "metric": "verified_orders"}),
            ("今天GMV", {"days": 1, "platform": None, "metric": "gmv"}),
            ("数据齐不齐", {"days": 7, "platform": None, "metric": None}),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(StoreOperationsAgent.parse_slots(text), expected)


class StoreToolsTests(Base):
    def test_role_visibility(self):
        self.seed()
        manager_tasks = self.tools.tasks_for(MANAGER)
        staff_tasks = self.tools.tasks_for(STAFF)
        self.assertEqual(len(manager_tasks), 3)
        self.assertEqual([t["task_id"] for t in staff_tasks], [t["task_id"] for t in manager_tasks
                                                               if t["assignee_id"] == STAFF["name"]])

    def test_task_detail_permission(self):
        self.seed()
        negative = next(t for t in self.tools.tasks_for(MANAGER) if "negative" in t["task_id"])
        with self.assertRaises(PermissionDenied):
            self.tools.task_detail(negative["task_id"], STAFF)
        detail = self.tools.task_detail(negative["task_id"], MANAGER)
        self.assertIn("events", detail)

    def test_status_permissions(self):
        self.seed()
        invite = next(t for t in self.tools.tasks_for(MANAGER) if t["task_id"].endswith("review-invite"))
        negative = next(t for t in self.tools.tasks_for(MANAGER) if "negative" in t["task_id"])
        self.assertTrue(self.tools.can_change_status(STAFF, invite, "accepted"))
        self.assertFalse(self.tools.can_change_status(STAFF, invite, "verified"))
        self.assertTrue(self.tools.can_change_status(MANAGER, invite, "verified"))
        self.assertFalse(self.tools.can_change_status(STAFF, negative, "accepted"))
        self.assertFalse(self.tools.can_submit_evidence(STAFF, negative))

    def test_metric_summary_platform_filter(self):
        self.seed()
        both = self.tools.metric_summary(days=7)
        self.assertEqual(set(both["platforms"]), {"meituan", "douyin"})
        only_meituan = self.tools.metric_summary(days=7, platform="meituan")
        self.assertEqual(set(only_meituan["platforms"]), {"meituan"})
        with self.assertRaises(ValueError):
            self.tools.metric_summary(platform="meituan-supplier")

    def test_data_quality_uses_config_thresholds(self):
        self.seed()
        quality = self.tools.data_quality(days=30)
        self.assertEqual(quality["parameters"]["fresh_days"], self.config["rules"]["fresh_days"])
        self.assertEqual(quality["parameters"]["jump_alert_percent"], self.config["rules"]["jump_alert_percent"])
        self.assertIn("meituan", quality["coverage"])

    def test_task_completion_counts(self):
        self.seed()
        invite = next(t for t in self.tools.tasks_for(MANAGER) if t["task_id"].endswith("review-invite"))
        self.tasks.change_status(invite["task_id"], "accepted", STAFF["name"])
        self.tasks.add_evidence(invite["task_id"], "脱敏订单号", "X-1", STAFF["name"])
        self.tasks.change_status(invite["task_id"], "done", STAFF["name"])
        self.tasks.change_status(invite["task_id"], "verified", MANAGER["name"])
        stats = self.tools.task_completion(days=7)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["count_by_status"]["verified"], 1)
        self.assertEqual(stats["completion_rate"], round(1 / 3 * 100, 1))
        with self.assertRaises(ValueError):
            self.tools.task_completion(days=0)

    def test_daily_report_sections_and_sources(self):
        self.seed()
        report = self.tools.daily_report()
        self.assertEqual(report["date"], self.today.isoformat())
        for section in ("一、数据状态", "二、经营指标", "三、任务执行", "四、数据质量问题", "五、下一步建议"):
            self.assertIn(section, report["report"])
        self.assertTrue(any(item.get("type") == "store_config" for item in report["sources"]))
        self.assertTrue(any(item.get("type") == "rule" for item in report["sources"]))

    def test_daily_report_without_data_is_honest(self):
        report = self.tools.daily_report()
        self.assertIn("没有任何已校验的平台数据", report["report"])
        self.assertFalse(report["summary"]["has_data"])


class AgentToolsModeTests(Base):
    def agent(self):
        return StoreOperationsAgent.from_tools(self.config, self.tools)

    def test_reply_uses_task_store_and_structured_sources(self):
        self.seed()
        agent = self.agent()
        result = agent.reply("我今天有什么任务？", "manager-001")
        self.assertEqual(result["intent"], "list_tasks")
        self.assertEqual(result["mode"], "tools")
        self.assertEqual(len(result["visible_task_ids"]), 3)
        self.assertTrue(all(isinstance(item, dict) for item in result["sources"]))

    def test_employee_cannot_read_store_wide_quality(self):
        self.seed()
        result = self.agent().reply("数据齐不齐？", "staff-001")
        self.assertEqual(result["intent"], "data_quality")
        self.assertIn("仅店长及以上", result["answer"])

    def test_manager_gets_daily_report(self):
        self.seed()
        result = self.agent().reply("生成今日日报", "manager-001")
        self.assertEqual(result["intent"], "daily_report")
        self.assertIn("经营日报", result["answer"])

    def test_metric_question_hides_gmv_from_employee(self):
        self.seed()
        agent = self.agent()
        self.assertIn("GMV", agent.reply("最近7天美团数据", "manager-001")["answer"])
        self.assertNotIn("GMV", agent.reply("最近7天美团数据", "staff-001")["answer"])

    def test_task_help_returns_concrete_task_fields(self):
        self.seed()
        result = self.agent().reply("这些任务具体要做什么？", "manager-001")
        self.assertEqual(result["intent"], "task_help")
        self.assertIn("生成依据", result["answer"])
        self.assertIn("需要提交的证据", result["answer"])
        self.assertIn("执行建议", result["answer"])

    def test_advice_returns_structured_actions(self):
        self.seed()
        result = self.agent().reply("给我一个提升曝光的方案", "manager-001")
        self.assertEqual(result["intent"], "advice")
        self.assertIsInstance(result["advice"], dict)
        self.assertTrue(result["advice"]["actions"])
        self.assertIn("优先动作", result["answer"])
        self.assertIn("watch", result["advice"])

    def test_advice_without_data_returns_next_steps(self):
        result = self.agent().reply("给我一个提升曝光的方案", "manager-001")
        self.assertEqual(result["intent"], "advice")
        self.assertTrue(result["advice"]["missing_data"])
        self.assertIn("先补齐", result["answer"])

    def test_unknown_question_degrades_without_llm(self):
        """规则表命中不了、又没有配置大模型时，应退化为能力说明而不是编造答案。"""
        self.seed()
        result = self.agent().reply("帮我写一段小红书探店文案", "manager-001")
        self.assertEqual(result["intent"], "capability_help")
        self.assertFalse(result["llm_enabled"])
        # 第 11 期：兜底不再是一段与问题无关的固定清单，而是"我没确定 + 我能答什么 + 怎么才能答"
        self.assertIn("没有完全确定", result["answer"])
        self.assertIn("我现在能直接回答的", result["answer"])

    def test_fallback_guides_by_topic(self):
        """兜底要按用户问到的主题给可操作指引（第 11 期）。"""
        self.seed()
        for question, keyword in (("帮我看看库存够不够", "库存与物料"),
                                  ("哪个时段人最多", "时段与客流"),
                                  ("同商圈竞品怎么样", "竞品")):
            with self.subTest(question=question):
                result = self.agent().reply(question, "manager-001")
                self.assertEqual(result["intent"], "capability_help")
                self.assertIn(keyword, result["answer"])

    def test_weak_keyword_scope_is_documented(self):
        """"今天/今日"属弱关键词，会命中任务清单；这是规则表的已知覆盖面，不是缺陷。"""
        self.assertEqual(StoreOperationsAgent.match_intent("今天天气怎么样"), "list_tasks")


if __name__ == "__main__":
    unittest.main()
