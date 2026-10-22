# -*- coding: utf-8 -*-
"""第 8 期：工具模板/实例、凭据隔离、沙箱执行与三类拒绝留痕。"""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from governance import ToolRunLog
from tool_manifest import ManifestError, export_template_zip, import_template_zip, validate_manifest
from tool_registry import ToolRegistry, ToolRegistryError
from tool_sandbox import SandboxError, run_sandbox

STORE = "WH-LION-001"
OTHER = "WH-LION-002"
TENANT = "LPL-001"
MANIFEST = {"template_key": "self-check", "name": "连通性自检", "entrypoint": "echo",
            "description": "回显入参，用于连通性自检"}


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ops.db"
        self.log = ToolRunLog(self.db, tenant_id=TENANT)
        self.registry = ToolRegistry(self.db, tenant_id=TENANT, tool_log=self.log)

    def tearDown(self):
        self.temp.cleanup()

    def instance(self, manifest=None, key="inst-a", credentials=None, **kwargs):
        tpl = self.registry.register_template(STORE, manifest or MANIFEST)
        return self.registry.create_instance(STORE, tpl["template_id"], key,
                                             credentials=credentials or {"api_key": f"{key}-secret"},
                                             **kwargs)

    # ---------------- 验收 1：模板 → 两个实例 → 沙箱执行 ----------------

    def test_import_template_derive_two_instances_and_execute(self):
        data = export_template_zip(MANIFEST, secrets={"api_key": "imported-secret"})
        imported = self.registry.import_template(data, STORE, actor="manager01")
        # 导出时 secret 已清空，因此导入包里不再携带凭据（用户需重新提供，见下）
        self.assertEqual(imported["credentials"], {})
        template = imported["template"]
        first = self.registry.create_instance(STORE, template["template_id"], "inst-a",
                                              credentials={"api_key": "secret-a"})
        second = self.registry.create_instance(STORE, template["template_id"], "inst-b",
                                               credentials={"api_key": "secret-b"})
        self.assertNotEqual(first["credential"]["secret_hash"], second["credential"]["secret_hash"])
        self.assertNotIn("secret-a", json.dumps(first))            # 明文不落库
        result = self.registry.invoke(first["instance_id"], {"probe": "ok"},
                                      actor="manager01", store_id=STORE)
        self.assertEqual(result["result"]["echo"], {"probe": "ok"})
        runs = self.log.list(STORE, limit=5)
        self.assertEqual(runs[0]["status"], "success")
        self.assertEqual(runs[0]["tool_name"], "tool_instance:inst-a")

    # ---------------- 验收 2：导出 ZIP 里 secret 为空 ----------------

    def test_export_zip_clears_secrets(self):
        tpl = self.registry.register_template(STORE, MANIFEST)
        data = self.registry.export_template(tpl["template_id"], secrets={"api_key": "super-secret-value"})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            secrets = json.loads(archive.read("secrets.json"))
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(secrets.get("api_key"), "")
        self.assertNotIn(b"super-secret-value", data)
        self.assertNotIn("super-secret-value", json.dumps(manifest))
        self.assertEqual(manifest["entrypoint"], "echo")

    # ---------------- 验收 3：三类拒绝 + 留痕 ----------------

    def test_network_policy_deny_is_blocked_and_logged(self):
        instance = self.instance({**MANIFEST, "network_required": True}, key="net-a")
        with self.assertRaises(ToolRegistryError):
            self.registry.invoke(instance["instance_id"], {}, store_id=STORE)
        runs = self.log.list(STORE, limit=5)
        self.assertEqual(runs[0]["status"], "blocked")
        self.assertIn("deny", str(runs[0]["error"]))

    def test_timeout_is_blocked_and_logged(self):
        instance = self.instance({**MANIFEST, "template_key": "slow", "entrypoint": "sleep"}, key="slow-a")
        with self.assertRaises(ToolRegistryError):
            self.registry.invoke(instance["instance_id"], {"seconds": 30}, timeout=1, store_id=STORE)
        runs = self.log.list(STORE, limit=5)
        self.assertEqual(runs[0]["status"], "blocked")
        self.assertIn("超时", str(runs[0]["error"]))

    def test_cross_store_invocation_is_blocked_and_logged(self):
        instance = self.instance(key="cross-a")
        with self.assertRaises(ToolRegistryError):
            self.registry.invoke(instance["instance_id"], {}, store_id=OTHER)
        runs = self.log.list(STORE, limit=5)
        self.assertEqual(runs[0]["status"], "blocked")
        self.assertIn("门店", str(runs[0]["error"]))

    # ---------------- 验收 4：崩溃不影响主进程 ----------------

    def test_sandbox_crash_does_not_kill_host(self):
        instance = self.instance({**MANIFEST, "template_key": "boom", "entrypoint": "crash"}, key="boom-a")
        with self.assertRaises(ToolRegistryError):
            self.registry.invoke(instance["instance_id"], {}, store_id=STORE)
        self.assertEqual(self.registry.list_instances(STORE)[0]["status"], "active")
        ok = self.registry.invoke(self.instance(key="ok-a")["instance_id"], {"alive": True},
                                  store_id=STORE)
        self.assertTrue(ok["result"]["echo"]["alive"])             # 宿主还能继续干活

    # ---------------- 边界 ----------------

    def test_manifest_rejects_unknown_entrypoint_and_bad_timeout(self):
        with self.assertRaises(ManifestError):
            validate_manifest({**MANIFEST, "entrypoint": "rm -rf"})
        with self.assertRaises(ManifestError):
            validate_manifest({**MANIFEST, "timeout_seconds": 99999})
        with self.assertRaises(ManifestError):
            validate_manifest({"template_key": "x"})

    def test_sandbox_rejects_unknown_entrypoint_and_oversize_result(self):
        with self.assertRaises(SandboxError):
            run_sandbox("not-allowed", {})
        big = {"items": list(range(60000))}
        with self.assertRaises(SandboxError):
            run_sandbox("echo", big, timeout=30)

    def test_import_rejects_bad_zip(self):
        with self.assertRaises(ManifestError):
            import_template_zip(b"not-a-zip")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "no manifest")
        with self.assertRaises(ManifestError):
            import_template_zip(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()