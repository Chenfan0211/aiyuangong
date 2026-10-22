# -*- coding: utf-8 -*-
"""第 9 期：三类记忆的抽取、去重、隔离与召回；技能蒸馏与反思提案。"""
import tempfile
import unittest
from pathlib import Path

from memory import MemoryError, MemoryStore, extract_memories
from skill_evolution import EvolutionError, SkillEvolution

STORE = "WH-LION-001"
OTHER = "WH-LION-002"
TENANT = "LPL-001"
TEXT = "我喜欢早上七点开门。我是武汉狮子座店的店长。门店有两台收银机。"


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "ops.db", tenant_id=TENANT)

    def tearDown(self):
        self.temp.cleanup()

    def test_extract_three_kinds(self):
        kinds = {item["kind"] for item in extract_memories(TEXT)}
        self.assertEqual(kinds, {"preference", "profile", "fact"})
        self.assertEqual(extract_memories("今天天气不错"), [])

    def test_absorb_dedupes_by_content(self):
        first = self.store.absorb(STORE, "u1", "我喜欢早上七点开门", role="manager")
        self.assertEqual(len(first), 1)
        again = self.store.absorb(STORE, "u1", "我喜欢早上七点开门", role="manager")
        self.assertEqual(again[0]["memory_id"], first[0]["memory_id"])
        self.assertEqual(again[0]["hits"], 2)
        self.assertEqual(len(self.store.recall(STORE, "u1")), 1)

    def test_recall_is_isolated_by_store_and_user(self):
        self.store.absorb(STORE, "u1", TEXT)
        self.store.absorb(STORE, "u2", "我喜欢晚上十点关门")
        self.store.absorb(OTHER, "u1", "门店有三台收银机")
        self.assertEqual(len(self.store.recall(STORE, "u1")), 3)
        self.assertEqual(len(self.store.recall(STORE, "u2")), 1)
        self.assertEqual(len(self.store.recall(OTHER, "u1")), 1)
        self.assertEqual(len(self.store.recall(STORE, "u1", kind="fact")), 1)

    def test_invalid_kind_is_rejected(self):
        with self.assertRaises(MemoryError):
            self.store.remember(STORE, "u1", "unknown", "x")
        with self.assertRaises(MemoryError):
            self.store.recall(STORE, "u1", kind="unknown")

    def test_forget_removes_record(self):
        record = self.store.absorb(STORE, "u1", "门店有两台收银机")[0]
        self.store.forget(record["memory_id"])
        self.assertEqual(self.store.recall(STORE, "u1"), [])


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ops.db"
        self.memory = MemoryStore(self.db, tenant_id=TENANT)
        self.evolution = SkillEvolution(self.db, tenant_id=TENANT, memory_store=self.memory)

    def tearDown(self):
        self.temp.cleanup()

    def test_distill_needs_enough_evidence(self):
        self.memory.absorb(STORE, "u1", "我喜欢早上七点开门")
        self.assertEqual(self.evolution.distill_from_memories(STORE, "u1"), [])
        self.memory.absorb(STORE, "u1", "我喜欢早上七点开门")
        proposals = self.evolution.distill_from_memories(STORE, "u1")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["proposal_type"], "sop_step")
        self.assertEqual(proposals[0]["status"], "pending")
        self.assertIn("规则版本机制", proposals[0]["payload"]["note"])

    def test_decide_only_changes_proposal_status(self):
        proposal = self.evolution.reflect(STORE, note="把日巡店提前到 09:00", evidence=["session-1"])
        self.assertEqual(proposal["status"], "pending")
        approved = self.evolution.decide(proposal["proposal_id"], "approved", "manager01", note="同意")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["decided_by"], "manager01")
        with self.assertRaises(EvolutionError):
            self.evolution.decide(proposal["proposal_id"], "rejected", "manager01")
        # 提案状态变化不影响记忆条数（未确认时行为不变的对照）
        self.assertEqual(self.memory.recall(STORE, "u1"), [])

    def test_invalid_proposal_type_and_status(self):
        with self.assertRaises(EvolutionError):
            self.evolution.propose(STORE, "unknown", "标题")
        with self.assertRaises(EvolutionError):
            self.evolution.list(STORE, status="unknown")

    def test_list_filters_by_status(self):
        first = self.evolution.reflect(STORE, note="观察一")
        self.evolution.reflect(STORE, note="观察二")
        self.evolution.decide(first["proposal_id"], "rejected", "manager01")
        self.assertEqual(len(self.evolution.list(STORE)), 2)
        self.assertEqual(len(self.evolution.list(STORE, status="pending")), 1)
        self.assertEqual(len(self.evolution.list(STORE, status="rejected")), 1)


if __name__ == "__main__":
    unittest.main()