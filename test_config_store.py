import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from registry import RegistryStore


class ConfigTruthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "store_config.json"
        source = json.loads((Path(__file__).resolve().parent / "store_config.json").read_text(encoding="utf-8"))
        self.config.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        self.auth = AuthStore(self.root / "ops.db", tenant_id="LPL-001")
        self.auth.create_user("owner01", "ShiZiZuo-2026-store", "店主", "owner", "LPL-001", "WH-LION-001")
        RegistryStore(self.root / "ops.db", tenant_id="LPL-001").register(source)

    def tearDown(self):
        self.tmp.cleanup()

    def test_database_config_wins_after_first_import(self):
        changed = json.loads(self.config.read_text(encoding="utf-8"))
        changed["review_cycle"]["target_qualified_month"] = 99
        self.config.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        app = create_app(config_path=self.config, metrics_db=self.root / "metrics.db",
                         store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                         evidence_dir=self.root / "evidence")
        client = TestClient(app)
        token = client.post("/api/auth/login", json={"username": "owner01", "password": "ShiZiZuo-2026-store"}).json()["token"]
        response = client.get("/api/admin/config", headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.json()["config"]["review_cycle"]["target_qualified_month"], 40)
        self.assertTrue(response.json()["revision"])
        client.close()

    def test_revision_conflict_is_rejected(self):
        app = create_app(config_path=self.config, metrics_db=self.root / "metrics.db",
                         store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                         evidence_dir=self.root / "evidence")
        client = TestClient(app)
        token = client.post("/api/auth/login", json={"username": "owner01", "password": "ShiZiZuo-2026-store"}).json()["token"]
        headers = {"Authorization": "Bearer " + token}
        current = client.get("/api/admin/config", headers=headers).json()
        first = client.patch("/api/admin/config", headers=headers,
                             json={"config": {"review_cycle": {"target_qualified_month": 41}},
                                   "revision": current["revision"]})
        self.assertEqual(first.status_code, 200)
        conflict = client.patch("/api/admin/config", headers=headers,
                                json={"config": {"review_cycle": {"target_qualified_month": 42}},
                                      "revision": current["revision"]})
        self.assertEqual(conflict.status_code, 409)
        client.close()


if __name__ == "__main__":
    unittest.main()
