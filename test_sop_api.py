# -*- coding: utf-8 -*-
"""第 7 期：SOP 接口（定义/版本/运行/恢复）与验收链路。"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
PASSWORD = "ShiZiZuo-2026-store"
NODES = [
    {"node_id": "inspect", "node_type": "human", "title": "提交巡检结果",
     "evidence_required": ["巡检照片"]},
    {"node_id": "approve", "node_type": "approval", "title": "店长审批"},
    {"node_id": "done", "node_type": "end"},
]
EDGES = [
    {"from_node": "inspect", "to_node": "approve"},
    {"from_node": "approve", "to_node": "done"},
]
SOP = {"sop_key": "daily-inspection", "name": "日巡店", "nodes": NODES, "edges": EDGES,
       "start_node": "inspect", "activate": True}


class SopApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_db = root / "ops.db"
        auth = AuthStore(self.store_db, tenant_id=TENANT)
        for username, display, role in (("owner01", "店主老王", "owner"),
                                        ("manager01", "武汉狮子座店店长", "manager"),
                                        ("staff01", "员工小李", "employee")):
            auth.create_user(username, PASSWORD, display, role, TENANT, STORE)
        self.client = TestClient(create_app(config_path=ROOT / "store_config.json",
                                            metrics_db=root / "metrics.db", store_db=self.store_db,
                                            chat_db=root / "chat.db", auth_mode="session"))
        self.tokens = {}
        for name in ("owner01", "manager01", "staff01"):
            status, data = self.call("POST", "/api/auth/login", {"username": name, "password": PASSWORD})
            self.assertEqual(status, 200, data)
            self.tokens[name] = data["token"]
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def call(self, method, path, payload=None, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client.request(method, path, json=payload, headers=headers)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def create_sop(self, username="manager01"):
        return self.call("POST", "/api/sops", SOP, token=self.tokens[username])

    def test_permissions(self):
        status, _ = self.call("POST", "/api/sops", SOP)
        self.assertEqual(status, 401)
        status, _ = self.call("POST", "/api/sops", SOP, token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/api/sop-runs/run-x", token=self.tokens["staff01"])
        self.assertEqual(status, 403)

    def test_full_chain_via_api(self):
        status, sop = self.create_sop()
        self.assertEqual(status, 200, sop)
        self.assertEqual((sop["version"], sop["status"]), (1, "active"))

        status, run = self.call("POST", f"/api/sops/{sop['sop_id']}/runs",
                                {"context": {"date": "2026-10-22"}}, token=self.tokens["manager01"])
        self.assertEqual(status, 200, run)
        self.assertEqual(run["status"], "waiting_human")

        run_id = run["run_id"]
        status, waiting = self.call("POST", f"/api/sop-runs/{run_id}/resume",
                                    {"payload": {"checked_items": 6}}, token=self.tokens["manager01"])
        self.assertEqual(status, 200, waiting)
        self.assertEqual(waiting["status"], "waiting_approval")

        status, detail = self.call("GET", f"/api/sop-runs/{run_id}", token=self.tokens["owner01"])
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["run"]["context"]["checked_items"], 6)      # 快照不丢
        self.assertEqual(detail["sop_version"], 1)

        status, done = self.call("POST", f"/api/sop-runs/{run_id}/resume",
                                 {"decision": "approved"}, token=self.tokens["manager01"])
        self.assertEqual(status, 200, done)
        self.assertEqual(done["status"], "completed")

    def test_duplicate_key_and_new_version(self):
        status, sop = self.create_sop()
        status, body = self.create_sop()
        self.assertEqual(status, 409, body)
        status, v2 = self.call("POST", f"/api/sops/{sop['sop_id']}/versions",
                               {"name": "日巡店", "nodes": NODES, "edges": EDGES,
                                "start_node": "inspect", "activate": True},
                               token=self.tokens["manager01"])
        self.assertEqual(status, 200, v2)
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["status"], "active")

    def test_invalid_graph_is_rejected(self):
        bad = dict(SOP, sop_key="bad-sop", nodes=[{"node_id": "a", "node_type": "human"}], edges=[])
        status, body = self.call("POST", "/api/sops", bad, token=self.tokens["manager01"])
        self.assertEqual(status, 400, body)

    def test_resume_on_terminal_run_is_conflict(self):
        """终态不可恢复：resume 在 completed 上必须是 409。"""
        _, sop = self.create_sop()
        _, run = self.call("POST", f"/api/sops/{sop['sop_id']}/runs", {}, token=self.tokens["manager01"])
        run_id = run["run_id"]
        self.call("POST", f"/api/sop-runs/{run_id}/resume", {"payload": {}}, token=self.tokens["manager01"])
        status, done = self.call("POST", f"/api/sop-runs/{run_id}/resume",
                                 {"decision": "approved"}, token=self.tokens["manager01"])
        self.assertEqual(status, 200, done)
        self.assertEqual(done["status"], "completed")
        status, body = self.call("POST", f"/api/sop-runs/{run_id}/resume",
                                 {"decision": "approved"}, token=self.tokens["manager01"])
        self.assertEqual(status, 409, body)


if __name__ == "__main__":
    unittest.main()