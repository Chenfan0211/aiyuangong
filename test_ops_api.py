import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from ops_store import OpsStore


class OpsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "store_config.json"
        self.config.write_text((Path(__file__).resolve().parent / "store_config.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.auth = AuthStore(self.root / "ops.db", tenant_id="LPL-001")
        self.auth.create_user("owner01", "ShiZiZuo-2026-store", "店主", "owner", "LPL-001", "WH-LION-001")
        self.ops = OpsStore(self.root / "ops.db", tenant_id="LPL-001", store_id="WH-LION-001")
        self.failed = self.ops.record_run("daily_sync", "import", status="failed", error="boom")
        self.app = create_app(config_path=self.config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                              evidence_dir=self.root / "evidence")
        self.client = TestClient(self.app)
        token = self.client.post("/api/auth/login", json={"username": "owner01", "password": "ShiZiZuo-2026-store"}).json()["token"]
        self.headers = {"Authorization": "Bearer " + token}
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_list_get_and_retry(self):
        listed = self.client.get("/api/admin/ops-runs", headers=self.headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertTrue(any(row["id"] == self.failed["id"] for row in listed.json()["runs"]))
        detail = self.client.get(f"/api/admin/ops-runs/{self.failed['id']}", headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        retry = self.client.post(f"/api/admin/ops-runs/{self.failed['id']}/retry", headers=self.headers)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["accepted"])
        self.assertEqual(retry.json()["queued_run"]["step_name"], "retry-request")


if __name__ == "__main__":
    unittest.main()
