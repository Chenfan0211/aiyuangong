# -*- coding: utf-8 -*-
"""配置真相源跨入口一致性测试。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import agent_server
import platform_sync
from app_fastapi import create_app
from auth import AuthStore
from registry import RegistryStore

ROOT = Path(__file__).resolve().parent
TENANT = "LPL-001"
STORE = "WH-LION-001"
PASSWORD = "ShiZiZuo-2026-store"


class ConfigTruthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "store_config.json"
        source = json.loads((ROOT / "store_config.json").read_text(encoding="utf-8"))
        source["review_cycle"]["target_qualified_month"] = 40
        self._write_json(self.config, source)
        self.ops_db = self.root / "ops.db"
        self.metrics_db = self.root / "metrics.db"
        self.chat_db = self.root / "chat.db"
        self.evidence_dir = self.root / "evidence"
        self.auth = AuthStore(self.ops_db, tenant_id=TENANT)
        self.auth.create_user("owner01", PASSWORD, "店主", "owner", TENANT, STORE)
        RegistryStore(self.ops_db, tenant_id=TENANT).register(source)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _mutate_file_target(self, value: int) -> None:
        data = self._read_json(self.config)
        data["review_cycle"]["target_qualified_month"] = value
        self._write_json(self.config, data)

    def _patch_target_via_api(self, value: int) -> None:
        app = create_app(config_path=self.config, metrics_db=self.metrics_db,
                         store_db=self.ops_db, chat_db=self.chat_db,
                         evidence_dir=self.evidence_dir)
        client = TestClient(app)
        try:
            login = client.post("/api/auth/login", json={"username": "owner01", "password": PASSWORD})
            self.assertEqual(login.status_code, 200, login.text)
            token = login.json()["token"]
            headers = {"Authorization": "Bearer " + token}
            current = client.get("/api/admin/config", headers=headers)
            self.assertEqual(current.status_code, 200, current.text)
            response = client.patch(
                "/api/admin/config", headers=headers,
                json={"config": {"review_cycle": {"target_qualified_month": value}},
                      "revision": current.json()["revision"]},
            )
            self.assertEqual(response.status_code, 200, response.text)
        finally:
            client.close()

    def _target_in_db(self) -> int:
        config = RegistryStore(self.ops_db, tenant_id=TENANT).store_config(TENANT, STORE)
        self.assertIsNotNone(config)
        return int(config["review_cycle"]["target_qualified_month"])

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        old_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + old_path if old_path else "")
        env["PYTHONIOENCODING"] = "utf-8"
        command = [
            sys.executable, str(ROOT / "store_cli.py"),
            "--config", str(self.config),
            "--metrics-db", str(self.metrics_db),
            "--store-db", str(self.ops_db),
            *args,
        ]
        return subprocess.run(command, cwd=ROOT, env=env, text=True,
                              capture_output=True, encoding="utf-8", errors="replace")

    def test_effective_config_keeps_database_value(self):
        self._mutate_file_target(99)
        registry = RegistryStore(self.ops_db, tenant_id=TENANT)
        effective = registry.effective_config(self._read_json(self.config))
        self.assertEqual(effective["review_cycle"]["target_qualified_month"], 40)
        self.assertEqual(self._target_in_db(), 40)

    def test_config_import_requires_force_and_exports_database(self):
        self._mutate_file_target(99)
        blocked = self._run_cli("config-import", "--file", str(self.config))
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertIn("--force", blocked.stderr + blocked.stdout)
        self.assertEqual(self._target_in_db(), 40)

        forced = self._run_cli("config-import", "--file", str(self.config), "--force")
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertEqual(self._target_in_db(), 99)

        self._patch_target_via_api(77)
        self._mutate_file_target(123)
        exported = self.root / "exported.json"
        result = self._run_cli("config-export", "--out", str(exported))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._read_json(exported)["review_cycle"]["target_qualified_month"], 77)

    def test_cli_uses_database_config_after_api_patch(self):
        self._patch_target_via_api(77)
        self._mutate_file_target(99)
        result = self._run_cli("tasks")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._target_in_db(), 77)
        self.assertIn("数据库", result.stderr)

    def test_platform_sync_uses_database_config_after_api_patch(self):
        self._patch_target_via_api(77)
        self._mutate_file_target(99)
        captured: dict[str, str] = {}

        class FakeMetricStore:
            def __init__(self, db_target, tenant_id=""):
                captured["db_target"] = str(db_target)
                captured["tenant_id"] = tenant_id

            def status(self, store_id):
                captured["store_id"] = store_id
                return {"store_id": store_id}

        argv = [
            "platform_sync.py", "--config", str(self.config), "--db", str(self.metrics_db),
            "--store-db", str(self.ops_db), "status",
        ]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(platform_sync, "PlatformMetricStore", FakeMetricStore), \
                contextlib.redirect_stdout(io.StringIO()):
            platform_sync.main()

        self.assertEqual(captured["tenant_id"], TENANT)
        self.assertEqual(captured["store_id"], STORE)
        self.assertEqual(self._target_in_db(), 77)

    def test_platform_sync_first_run_imports_json_into_empty_database(self):
        empty_db = self.root / "empty-ops.db"
        captured: dict[str, str] = {}

        class FakeMetricStore:
            def __init__(self, db_target, tenant_id=""):
                captured["tenant_id"] = tenant_id

            def status(self, store_id):
                captured["store_id"] = store_id
                return {"store_id": store_id}

        argv = [
            "platform_sync.py", "--config", str(self.config), "--db", str(self.metrics_db),
            "--store-db", str(empty_db), "status",
        ]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(platform_sync, "PlatformMetricStore", FakeMetricStore), \
                contextlib.redirect_stdout(io.StringIO()):
            platform_sync.main()

        self.assertEqual(captured["tenant_id"], TENANT)
        self.assertEqual(captured["store_id"], STORE)
        self.assertEqual(
            RegistryStore(empty_db, tenant_id=TENANT).store_config(TENANT, STORE)
            ["review_cycle"]["target_qualified_month"], 40,
        )

    def test_agent_server_main_uses_database_config_after_api_patch(self):
        self._patch_target_via_api(77)
        self._mutate_file_target(99)
        captured: dict[str, dict] = {}

        class FakeServer:
            def __init__(self, address, handler):
                self.address = address

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                return None

        def fake_from_tools(config, tools, llm_responder=None, tool_agent=None):
            captured["config"] = config
            return SimpleNamespace()

        argv = [
            "agent_server.py", "--config", str(self.config), "--db", str(self.chat_db),
            "--store-db", str(self.ops_db), "--metrics-db", str(self.metrics_db),
        ]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(agent_server, "ThreadingHTTPServer", FakeServer), \
                mock.patch.object(agent_server.StoreOperationsAgent, "from_tools",
                                  side_effect=fake_from_tools), \
                contextlib.redirect_stdout(io.StringIO()):
            agent_server.main()

        self.assertEqual(captured["config"]["review_cycle"]["target_qualified_month"], 77)
        self.assertEqual(self._target_in_db(), 77)


if __name__ == "__main__":
    unittest.main()
