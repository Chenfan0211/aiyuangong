# -*- coding: utf-8 -*-
"""第 5 期：能力注册表、作用域校验与受控调用（越权拒绝且留痕）。"""
import json
import tempfile
import unittest
from pathlib import Path

from agent_tools import StoreTools
from capabilities import CapabilityDenied, CapabilityError, build_registry
from capability_scope import CapabilityScope, CapabilityScopeError
from governance import ToolRunLog
from llm_agent import TOOL_SPECS, ToolCallingAgent
from platform_data import PlatformMetricStore
from snapshot_builder import load_config
from task_store import TaskStore

ROOT = Path(__file__).resolve().parent
STORE = "WH-LION-001"
TENANT = "LPL-001"
MANAGER = {"user_id": "manager-001", "role": "manager", "tenant_id": TENANT, "store_id": STORE}
STAFF = {"user_id": "staff-001", "role": "employee", "tenant_id": TENANT, "store_id": STORE}


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = load_config(ROOT / "store_config.json")
        store_db = root / "ops.db"
        self.tools = StoreTools(self.config,
                                PlatformMetricStore(root / "metrics.db", tenant_id=TENANT),
                                TaskStore(store_db, tenant_id=TENANT),
                                config_path=ROOT / "store_config.json")
        self.tool_log = ToolRunLog(store_db, tenant_id=TENANT)
        self.registry = build_registry(tenant_id=TENANT, store_id=STORE, tool_log=self.tool_log)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_tool_specs_are_registered_with_ids(self):
        capabilities = self.registry.all()
        self.assertEqual(len(capabilities), len(TOOL_SPECS))
        self.assertEqual([item.name for item in capabilities],
                         sorted(spec.name for spec in TOOL_SPECS))
        self.assertTrue(all(item.capability_id == f"tool:{item.name}" for item in capabilities))
        self.assertTrue(all(item.scope.roles for item in capabilities))

    def test_derived_tool_specs_are_byte_identical_to_legacy(self):
        legacy = [spec.as_openai_tool() for spec in TOOL_SPECS]
        derived = [spec.as_openai_tool() for spec in self.registry.as_tool_specs()]
        self.assertEqual(json.dumps(legacy, ensure_ascii=False), json.dumps(derived, ensure_ascii=False))

    def test_available_for_matches_legacy_exposure(self):
        agent = ToolCallingAgent(self.tools, None)
        for user in (MANAGER, STAFF):
            legacy = {spec.name for spec in agent.exposed_specs(user)}
            derived = {item.name for item in self.registry.available_for(user)}
            self.assertEqual(derived, legacy, user["role"])

    def test_scope_requires_subset(self):
        allowed = CapabilityScope.of("T1", "S1", ["manager", "owner"])
        allowed.require(CapabilityScope.of("T1", "S1", ["manager"]))
        with self.assertRaises(CapabilityScopeError):
            allowed.require(CapabilityScope.of("T1", "S1", ["employee"]))
        with self.assertRaises(CapabilityScopeError):
            allowed.require(CapabilityScope.of("T2", "S1", ["manager"]))

    def test_denied_invocation_is_rejected_and_logged(self):
        agent = ToolCallingAgent(self.tools, None)
        with self.assertRaises(CapabilityDenied):
            self.registry.invoke("tool:build_daily_report", {}, STAFF, agent._invoke)
        runs = self.tool_log.list(STORE, limit=10)
        blocked = [run for run in runs if run.get("status") == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["tool_name"], "build_daily_report")
        self.assertIn("无权", str(blocked[0].get("error")))

    def test_allowed_invocation_returns_result_and_logs_success(self):
        agent = ToolCallingAgent(self.tools, None)
        result = self.registry.invoke("tool:get_data_status", {}, MANAGER, agent._invoke)
        self.assertIsInstance(result, dict)
        runs = self.tool_log.list(STORE, limit=10)
        self.assertEqual(runs[0]["status"], "success")
        self.assertEqual(runs[0]["tool_name"], "get_data_status")

    def test_unknown_capability_is_denied_and_logged(self):
        agent = ToolCallingAgent(self.tools, None)
        with self.assertRaises(CapabilityDenied):
            self.registry.invoke("tool:no_such_tool", {}, MANAGER, agent._invoke)
        with self.assertRaises(CapabilityError):
            self.registry.get("tool:no_such_tool")

    def test_duplicate_registration_is_rejected(self):
        capability = self.registry.all()[0]
        with self.assertRaises(CapabilityError):
            self.registry.register(capability)


if __name__ == "__main__":
    unittest.main()