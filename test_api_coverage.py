# -*- coding: utf-8 -*-
"""FastAPI 路由契约与遗漏接口的直接覆盖。"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore

PASSWORD = "ShiZiZuo-2026-store"
EXPECTED_API_ROUTES = {
    ("post", "/api/auth/login"),
    ("post", "/api/auth/mfa/verify"),
    ("post", "/api/auth/password"),
    ("post", "/api/auth/mfa/setup"),
    ("post", "/api/auth/mfa/enable"),
    ("post", "/api/auth/logout"),
    ("get", "/api/auth/me"),
    ("get", "/api/webhooks/approvals/{provider}"),
    ("post", "/api/webhooks/approvals/{provider}"),
    ("post", "/api/inbound/messages/{provider}"),
    ("get", "/api/admin/config"),
    ("get", "/api/admin/channel-bindings"),
    ("post", "/api/admin/channel-bindings"),
    ("patch", "/api/admin/channel-bindings/{binding_id}"),
    ("patch", "/api/admin/config"),
    ("get", "/api/admin/members"),
    ("post", "/api/admin/members"),
    ("patch", "/api/admin/members/{user_id}"),
    ("post", "/api/admin/members/{user_id}/status"),
    ("post", "/api/admin/members/{user_id}/reset-password"),
    ("post", "/api/admin/members/{user_id}/reset-mfa"),
    ("get", "/api/admin/ops-runs"),
    ("get", "/api/admin/retries"),
    ("get", "/api/admin/ops-runs/{run_id}"),
    ("post", "/api/admin/ops-runs/{run_id}/retry"),
    ("get", "/api/bootstrap"),
    ("get", "/api/data-status"),
    ("get", "/api/metrics/summary"),
    ("get", "/api/data-quality"),
    ("get", "/api/analysis/compare"),
    ("get", "/api/reports/daily"),
    ("get", "/api/tasks/today"),
    ("get", "/api/tasks/{task_id}"),
    ("post", "/api/tasks/{task_id}/evidence"),
    ("post", "/api/tasks/{task_id}/evidence-upload"),
    ("get", "/api/evidence/{evidence_id}"),
    ("post", "/api/tasks/{task_id}/feedback"),
    ("get", "/api/tasks/{task_id}/outcomes"),
    ("get", "/api/outcomes/summary"),
    ("post", "/api/tasks/{task_id}/status"),
    ("get", "/api/dashboard"),
    ("get", "/api/reports/weekly"),
    ("get", "/api/reports/monthly"),
    ("get", "/api/approvals"),
    ("post", "/api/approvals"),
    ("post", "/api/approvals/{approval_id}/decide"),
    ("get", "/api/audit"),
    ("get", "/api/tool-runs"),
    ("post", "/api/chat"),
    ("get", "/api/history"),
}

class ApiCoverageTests(unittest.TestCase):
    def setUp(self):
        old = os.environ.pop("STORE_AGENT_MFA_ENFORCE", None)
        self.addCleanup(self._restore_env, "STORE_AGENT_MFA_ENFORCE", old)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = self.root / "store_config.json"
        config.write_text(
            (Path(__file__).resolve().parent / "store_config.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        auth = AuthStore(self.root / "ops.db", tenant_id="LPL-001")
        for username, role in (("owner01", "owner"), ("staff01", "employee")):
            auth.create_user(
                username, PASSWORD, username, role, "LPL-001", "WH-LION-001",
            )
        self.app = create_app(
            config_path=config,
            metrics_db=self.root / "metrics.db",
            store_db=self.root / "ops.db",
            chat_db=self.root / "chat.db",
            evidence_dir=self.root / "evidence",
        )
        self.client = TestClient(self.app)
        self.owner = self._login("owner01")
        self.staff = self._login("staff01")
        self.client.cookies.clear()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def _restore_env(name, old):
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old

    def _login(self, username):
        response = self.client.post(
            "/api/auth/login", json={"username": username, "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    @staticmethod
    def _headers(token):
        return {"Authorization": "Bearer " + token}

    def test_openapi_route_manifest_is_locked(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        actual = {
            (method, path)
            for path, operations in response.json()["paths"].items()
            if path.startswith("/api/")
            for method in operations
            if method in {"get", "post", "put", "patch", "delete"}
        }
        self.assertEqual(EXPECTED_API_ROUTES, actual)
        self.assertEqual(50, len(actual))

    def test_agent_page_supports_opt_in_demo_prefill(self):
        with patch.dict(os.environ, {
            "STORE_AGENT_DEMO_PREFILL_USER": "e2e_owner",
            "STORE_AGENT_DEMO_PREFILL_PASSWORD": "Owner-Secure-2026",
        }, clear=False):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('value="e2e_owner"', response.text)
        self.assertIn('value="Owner-Secure-2026"', response.text)

    def test_chat_advice_is_structured_and_backward_compatible(self):
        response = self.client.post(
            "/api/chat",
            headers=self._headers(self.owner),
            json={"session_id": "advice-check", "message": "给我一个提升曝光的方案"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("answer", body)
        self.assertEqual(body["intent"], "advice")
        self.assertIsInstance(body["advice"], dict)
        self.assertTrue(body["advice"]["actions"])
        self.assertTrue(body["advice"]["missing_data"])

    def test_bootstrap_declares_full_fastapi_capabilities(self):
        response = self.client.get("/api/bootstrap", headers=self._headers(self.owner))
        self.assertEqual(response.status_code, 200, response.text)
        capabilities = response.json()["capabilities"]
        for key in ("full", "mfa", "admin", "callbacks", "evidence_upload", "outcomes", "inbound_messages", "channel_bindings"):
            self.assertIs(capabilities[key], True, key)

    def test_data_status_requires_auth_and_returns_for_metrics_reader(self):
        self.assertEqual(self.client.get("/api/data-status").status_code, 401)
        response = self.client.get("/api/data-status", headers=self._headers(self.owner))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["store_id"], "WH-LION-001")

    def test_history_requires_auth_and_returns_owned_session(self):
        self.assertEqual(
            self.client.get("/api/history?session_id=smoke-session").status_code, 401,
        )
        response = self.client.get(
            "/api/history?session_id=smoke-session", headers=self._headers(self.owner),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"messages": []})

    def test_admin_retries_requires_owner_and_returns_queue(self):
        denied = self.client.get("/api/admin/retries", headers=self._headers(self.staff))
        self.assertEqual(denied.status_code, 403, denied.text)
        allowed = self.client.get("/api/admin/retries", headers=self._headers(self.owner))
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json(), {"retries": []})


if __name__ == "__main__":
    unittest.main()