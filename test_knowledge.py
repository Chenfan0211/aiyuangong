# -*- coding: utf-8 -*-
"""第 6 期：知识库结构感知检索、可回查引用与"无命中就明说"。"""
import tempfile
import unittest
from pathlib import Path

from knowledge import NO_MATCH_REASON, KnowledgeError, KnowledgeStore, split_sections, trim_snippet

STORE = "WH-LION-001"
TENANT = "LPL-001"
CONTENT = """# 门店运营手册

## 库存管理
库存低于安全库存时，需要当日内提交补货草稿，并附盘点截图。
第 3 页

## 差评处理
差评必须在 4 小时内回复，涉及赔付的要走审批。

### 话术
回复差评时不要暴露顾客手机号。
"""


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp.name) / "ops.db", tenant_id=TENANT)
        self.base = self.store.create_base(STORE, "ops-manual", "门店运营手册", "内部制度")

    def tearDown(self):
        self.temp.cleanup()

    def test_base_creation_is_idempotent(self):
        again = self.store.create_base(STORE, "ops-manual", "门店运营手册")
        self.assertEqual(again["base_id"], self.base["base_id"])
        self.assertEqual(len(self.store.list_bases(STORE)), 1)
        self.assertEqual(self.store.list_bases(STORE)[0]["document_count"], 0)

    def test_ingest_is_idempotent_by_content_hash(self):
        first = self.store.ingest_document(STORE, self.base["base_id"], "运营手册", CONTENT)
        self.assertFalse(first["duplicate"])
        self.assertGreaterEqual(first["chunk_count"], 2)
        second = self.store.ingest_document(STORE, self.base["base_id"], "运营手册（重复）", CONTENT)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["document_id"], first["document_id"])
        self.assertEqual(len(self.store.list_documents(self.base["base_id"])), 1)

    def test_sections_keep_heading_paths(self):
        sections = split_sections(CONTENT)
        paths = [section["heading_path"] for section in sections]
        self.assertIn(["门店运营手册", "库存管理"], paths)
        self.assertIn(["门店运营手册", "差评处理", "话术"], paths)

    def test_search_hits_and_citations_are_traceable(self):
        self.store.ingest_document(STORE, self.base["base_id"], "运营手册", CONTENT)
        result = self.store.search(STORE, "库存安全库存补货")
        self.assertTrue(result["has_match"])
        citation = result["citations"][0]
        self.assertEqual(citation["index"], 1)
        self.assertEqual(citation["heading_path"], ["门店运营手册", "库存管理"])
        self.assertTrue(citation["chunk_id"].startswith("kch-"))
        self.assertTrue(citation["document_id"].startswith("kdoc-"))
        self.assertEqual(citation["document_title"], "运营手册")

    def test_no_match_says_so_without_fabricating(self):
        self.store.ingest_document(STORE, self.base["base_id"], "运营手册", CONTENT)
        result = self.store.search(STORE, "如何烘焙咖啡豆")
        self.assertFalse(result["has_match"])
        self.assertEqual(result["reason"], NO_MATCH_REASON)
        self.assertEqual(result["citations"], [])

    def test_snippet_trims_long_text_and_strips_footer(self):
        self.store.ingest_document(STORE, self.base["base_id"], "运营手册", CONTENT)
        result = self.store.search(STORE, "库存安全库存")
        snippet = result["citations"][0]["snippet"]
        self.assertNotIn("第 3 页", snippet)
        long_text = "制度" * 400
        self.assertTrue(trim_snippet(long_text).endswith("…"))
        self.assertLessEqual(len(trim_snippet(long_text)), 401)

    def test_citations_can_be_recorded_for_traceback(self):
        self.store.ingest_document(STORE, self.base["base_id"], "运营手册", CONTENT)
        result = self.store.search(STORE, "差评回复话术")
        recorded = self.store.record_citations(STORE, "差评回复话术", result["citations"])
        self.assertEqual(recorded, len(result["citations"]))
        self.assertEqual(self.store._scalar(
            "select count(*) as n from knowledge_citations where tenant_id=? and store_id=?",
            (TENANT, STORE)), recorded)

    def test_empty_content_and_unknown_base_are_rejected(self):
        with self.assertRaises(KnowledgeError):
            self.store.ingest_document(STORE, self.base["base_id"], "空文档", "   ")
        with self.assertRaises(KnowledgeError):
            self.store.ingest_document(STORE, "kb-none", "文档", CONTENT)

    def test_chunk_embeddings_are_persisted(self):
        document = self.store.ingest_document(STORE, self.base["base_id"], "运营手册", CONTENT)
        rows = self.store._candidates(STORE, self.base["base_id"])
        self.assertTrue(rows)
        for row in rows:
            self.assertNotEqual(row["embedding_json"], "{}")
        self.assertGreater(document["chunk_count"], 0)

    def test_vector_scoring_can_be_toggled(self):
        self.store.ingest_document(STORE, self.base["base_id"], "运营手册", CONTENT)
        with_vector = self.store.search(STORE, "库存安全库存", use_vector=True)
        without_vector = self.store.search(STORE, "库存安全库存", use_vector=False)
        self.assertTrue(with_vector["has_match"])
        self.assertTrue(without_vector["has_match"])
        self.assertTrue(with_vector["vector_enabled"])
        self.assertEqual(with_vector["vector_weight"], 0.3)
        self.assertFalse(without_vector["vector_enabled"])
        self.assertEqual(without_vector["vector_weight"], 0.0)
        # 同一查询：向量参与后最终得分发生变化（说明确实参与打分）
        self.assertNotEqual(with_vector["citations"][0]["score"],
                            without_vector["citations"][0]["score"])

    def test_vector_weight_can_be_disabled_by_zero_weight(self):
        self.store.ingest_document(STORE, self.base["base_id"], "运营手册", CONTENT)
        zero = self.store.search(STORE, "库存安全库存", use_vector=True, vector_weight=0.0)
        plain = self.store.search(STORE, "库存安全库存", use_vector=False)
        self.assertEqual(zero["citations"][0]["score"], plain["citations"][0]["score"])

    def test_no_match_reports_vector_flag(self):
        result = self.store.search(STORE, "如何烘焙咖啡豆")
        self.assertFalse(result["has_match"])
        self.assertIn("vector_enabled", result)


if __name__ == "__main__":
    unittest.main()