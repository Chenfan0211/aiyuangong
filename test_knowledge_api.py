# -*- coding: utf-8 -*-
"""第 6 期：知识库接口（列库 / 导入 / 检索）与对话引用回填。"""
import json
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
CONTENT = """# 门店运营手册

## 库存管理
库存低于安全库存时，需要当日内提交补货草稿，并附盘点截图。

## 差评处理
差评必须在 4 小时内回复，涉及赔付的要走审批。
"""
DOC = {"base_key": "ops-manual", "base_name": "门店运营手册", "title": "运营手册", "content": CONTENT}


class KnowledgeApiTests(unittest.TestCase):
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

    def test_permissions_and_base_listing(self):
        status, _ = self.call("GET", "/api/knowledge/bases")
        self.assertEqual(status, 401)
        status, body = self.call("GET", "/api/knowledge/bases", token=self.tokens["staff01"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["count"], 0)
        status, _ = self.call("POST", "/api/knowledge/documents", DOC, token=self.tokens["staff01"])
        self.assertEqual(status, 403)
        status, created = self.call("POST", "/api/knowledge/documents", DOC, token=self.tokens["manager01"])
        self.assertEqual(status, 200, created)
        self.assertFalse(created["duplicate"])
        status, body = self.call("GET", "/api/knowledge/bases", token=self.tokens["staff01"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["bases"][0]["document_count"], 1)

    def test_duplicate_import_is_idempotent(self):
        self.call("POST", "/api/knowledge/documents", DOC, token=self.tokens["manager01"])
        status, again = self.call("POST", "/api/knowledge/documents", DOC, token=self.tokens["manager01"])
        self.assertEqual(status, 200, again)
        self.assertTrue(again["duplicate"])
        status, body = self.call("GET", "/api/knowledge/bases", token=self.tokens["manager01"])
        self.assertEqual(body["bases"][0]["document_count"], 1)

    def test_search_hit_and_no_match(self):
        self.call("POST", "/api/knowledge/documents", DOC, token=self.tokens["manager01"])
        status, hit = self.call("POST", "/api/knowledge/search",
                                {"query": "库存安全库存补货"}, token=self.tokens["staff01"])
        self.assertEqual(status, 200, hit)
        self.assertTrue(hit["has_match"])
        citation = hit["citations"][0]
        self.assertEqual(citation["heading_path"], ["门店运营手册", "库存管理"])
        self.assertTrue(citation["chunk_id"].startswith("kch-"))
        status, miss = self.call("POST", "/api/knowledge/search",
                                 {"query": "如何烘焙咖啡豆"}, token=self.tokens["staff01"])
        self.assertFalse(miss["has_match"])
        self.assertEqual(miss["reason"], "知识库没有相关内容")
        self.assertEqual(miss["citations"], [])

    def test_chat_response_carries_citations(self):
        self.call("POST", "/api/knowledge/documents", DOC, token=self.tokens["manager01"])
        status, body = self.call("POST", "/api/chat",
                                 {"session_id": "s-knowledge", "message": "库存不足怎么办"},
                                 token=self.tokens["manager01"])
        self.assertEqual(status, 200, body)
        self.assertIn("citations", body)
        self.assertIsInstance(body["citations"], list)

    def test_stream_done_event_carries_citations(self):
        self.call("POST", "/api/knowledge/documents", DOC, token=self.tokens["manager01"])
        headers = {"Authorization": f"Bearer {self.tokens['manager01']}"}
        with self.client.stream("POST", "/api/chat/stream",
                                json={"session_id": "s-stream", "message": "库存不足怎么办"},
                                headers=headers) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(chunk for chunk in response.iter_text())
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        done = [item for item in events if item.get("type") == "done"]
        self.assertTrue(done, events)
        self.assertIn("citations", done[0])
        self.assertIsInstance(done[0]["citations"], list)


if __name__ == "__main__":
    unittest.main()