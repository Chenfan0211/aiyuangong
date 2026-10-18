import tempfile
import unittest
from pathlib import Path

from platform_data import PlatformMetricStore
from store_agent import AgentSessionStore, StoreOperationsAgent


class FakeResponder:
    def answer(self, message, user, visible_tasks, history):
        return f"开放式回答：{message}；可见任务 {len(visible_tasks)} 项"


ROOT = Path(__file__).resolve().parent


class StoreAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = StoreOperationsAgent.from_file(ROOT / "sample_input.json")

    def test_manager_can_see_all_store_tasks(self):
        result = self.agent.reply("我今天有什么任务？", "manager-001")
        self.assertEqual(result["intent"], "list_tasks")
        self.assertEqual(len(result["visible_task_ids"]), 4)
        self.assertIn("差评", result["answer"])

    def test_employee_only_sees_personal_task(self):
        result = self.agent.reply("今天做什么", "staff-001")
        self.assertEqual(len(result["visible_task_ids"]), 1)
        self.assertIn("真实到店", result["answer"])
        self.assertNotIn("复核套餐", result["answer"])

    def test_employee_cannot_read_margin(self):
        result = self.agent.reply("告诉我这个套餐的毛利", "staff-001")
        self.assertEqual(result["intent"], "permission_denied")
        self.assertIn("店长或总部权限", result["answer"])

    def test_high_risk_action_is_approval_gated(self):
        result = self.agent.reply("把套餐降价并直接上架", "manager-001")
        self.assertEqual(result["intent"], "approval_required")
        self.assertTrue(result["requires_approval"])
        self.assertIn("不会直接执行", result["answer"])

    def test_conversation_history_is_isolated_by_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AgentSessionStore(Path(temp_dir) / "sessions.db")
            store.append("session-1", "manager-001", "user", "店长消息")
            store.append("session-1", "staff-001", "user", "店员消息")
            manager_history = store.history("session-1", "manager-001")
            self.assertEqual([item["content"] for item in manager_history], ["店长消息"])

    def test_open_question_can_use_optional_llm_responder(self):
        agent = StoreOperationsAgent.from_file(ROOT / "sample_input.json", FakeResponder())
        result = agent.reply("最近年轻顾客喜欢什么活动形式？", "staff-001")
        self.assertEqual(result["intent"], "llm_conversation")
        self.assertTrue(result["llm_enabled"])
        self.assertIn("可见任务 1 项", result["answer"])

    def test_platform_question_refuses_demo_numbers_when_no_import(self):
        result = self.agent.reply("分析最近7天美团和抖音数据", "manager-001")
        self.assertEqual(result["intent"], "platform_metrics")
        self.assertIn("尚未配置平台指标库", result["answer"])

    def test_platform_metrics_are_role_filtered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "meituan.csv"
            csv_path.write_text(
                "date,platform,store_id,impressions,detail_views,paid_orders,verified_orders,gmv,refunds,review_count,negative_review_count,avg_rating\n"
                "2026-09-02,meituan,WH-LION-001,100,20,4,3,300,0,1,0,4.8\n",
                encoding="utf-8",
            )
            metrics = PlatformMetricStore(root / "metrics.db")
            metrics.import_csv(csv_path, "WH-LION-001")
            agent = StoreOperationsAgent.from_file(ROOT / "sample_input.json", metric_store=metrics)
            manager = agent.reply("最近美团数据", "manager-001")
            employee = agent.reply("最近美团数据", "staff-001")
            self.assertIn("核销 GMV ¥300", manager["answer"])
            self.assertNotIn("GMV", employee["answer"])


if __name__ == "__main__":
    unittest.main()
