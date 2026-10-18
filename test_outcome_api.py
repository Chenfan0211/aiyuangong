import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from task_store import TaskStore


class OutcomeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth = AuthStore(self.root / "ops.db", tenant_id="LPL-001")
        self.auth.create_user("manager01", "ShiZiZuo-2026-store", "店长", "manager", "LPL-001", "WH-LION-001")
        self.tasks = TaskStore(self.root / "ops.db", tenant_id="LPL-001")
        self.tasks.save_plan({"date": "2026-10-18", "tasks": [{
            "task_id": "T-1", "category": "reputation", "title": "任务", "assignee": "店长",
            "priority": "high", "due_at": "2026-10-18T18:00:00+08:00", "target": {},
            "evidence_required": [], "reason": "r", "requires_approval": False}]}, "WH-LION-001")
        self.app = create_app(config_path=Path(__file__).resolve().parent / "store_config.json",
                              metrics_db=self.root / "metrics.db", store_db=self.root / "ops.db",
                              chat_db=self.root / "chat.db", evidence_dir=self.root / "evidence")
        self.client = TestClient(self.app)
        token = self.client.post("/api/auth/login", json={"username": "manager01", "password": "ShiZiZuo-2026-store"}).json()["token"]
        self.headers = {"Authorization": "Bearer " + token}
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_feedback_and_summary(self):
        feedback = self.client.post("/api/tasks/T-1/feedback", headers=self.headers,
                                    json={"decision": "accepted", "reason": ""})
        self.assertEqual(feedback.status_code, 200, feedback.text)
        outcomes = self.client.get("/api/tasks/T-1/outcomes", headers=self.headers)
        self.assertEqual(outcomes.status_code, 200)
        self.assertEqual(outcomes.json()["outcomes"], [])
        summary = self.client.get("/api/outcomes/summary", headers=self.headers)
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["summary"]["feedback"]["total"], 1)

    def test_reject_requires_reason(self):
        response = self.client.post("/api/tasks/T-1/feedback", headers=self.headers,
                                    json={"decision": "rejected", "reason": ""})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
