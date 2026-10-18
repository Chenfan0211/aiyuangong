import base64
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from platform_data import PlatformMetricStore
from task_store import TaskStore

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z0YQAAAAASUVORK5CYII="
)


class EvidenceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.metrics = PlatformMetricStore(self.root / "metrics.db")
        self.tasks = TaskStore(self.root / "ops.db")
        self.auth = AuthStore(self.root / "ops.db")
        self.config = Path(__file__).resolve().parent / "store_config.json"
        self.tasks.save_plan({"date": "2026-10-18", "tasks": [{
            "task_id": "T-EVIDENCE", "category": "reputation", "title": "上传图片证据",
            "assignee": "员工小李", "priority": "high", "due_at": "2026-10-18T18:00:00+08:00",
            "target": {}, "evidence_required": ["图片"], "reason": "测试", "requires_approval": False}]},
            "WH-LION-001")
        self.auth.create_user("staff01", "ShiZiZuo-2026-store", "员工小李", "employee",
                              "LPL-001", "WH-LION-001")
        self.app = create_app(config_path=self.config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                              evidence_dir=self.root / "evidence")
        self.client = TestClient(self.app)
        token = self.client.post("/api/auth/login", json={"username": "staff01", "password": "ShiZiZuo-2026-store"}).json()["token"]
        self.headers = {"Authorization": "Bearer " + token}
        self.client.post("/api/tasks/T-EVIDENCE/status", json={"session_id": "s", "status": "accepted"}, headers=self.headers)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_upload_download_and_bind_evidence(self):
        uploaded = self.client.post("/api/tasks/T-EVIDENCE/evidence-upload?filename=shot.png",
                                    content=PNG, headers={**self.headers, "Content-Type": "image/png"})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        evidence_id = uploaded.json()["evidence_file"]["evidence_id"]
        self.assertEqual(uploaded.json()["evidence"]["object_ref"], evidence_id)
        downloaded = self.client.get(f"/api/evidence/{evidence_id}?task_id=T-EVIDENCE", headers=self.headers)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, PNG)

    def test_rejects_non_image_upload(self):
        uploaded = self.client.post("/api/tasks/T-EVIDENCE/evidence-upload?filename=x.png",
                                    content=b"not an image", headers={**self.headers, "Content-Type": "image/png"})
        self.assertEqual(uploaded.status_code, 400)


if __name__ == "__main__":
    unittest.main()
