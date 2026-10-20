# -*- coding: utf-8 -*-
"""标准库备用入口的契约与 MFA 降级测试。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_server import AgentHandler, validate_startup
from auth import AuthStore, SecurityContext, permissions_for, totp_code


class AgentServerContractTests(unittest.TestCase):
    def test_mfa_login_returns_explicit_not_implemented(self):
        old = os.environ.get("STORE_AGENT_MFA_ENFORCE")
        os.environ["STORE_AGENT_MFA_ENFORCE"] = "1"
        try:
            with tempfile.TemporaryDirectory() as temp:
                auth = AuthStore(Path(temp) / "ops.db", tenant_id="LPL-001")
                user = auth.create_user(
                    "mfa01", "ShiZiZuo-2026-store", "MFA User", "manager",
                    "LPL-001", "WH-LION-001",
                )
                enrollment = auth.begin_mfa_enrollment(user["user_id"])
                auth.confirm_mfa_enrollment(user["user_id"], totp_code(enrollment["secret"]))

                handler = object.__new__(AgentHandler)
                handler.auth = auth
                handler.tools = SimpleNamespace(store_id="WH-LION-001")
                handler.client_address = ("127.0.0.1", 12345)
                handler._read_json = lambda: {
                    "username": "mfa01", "password": "ShiZiZuo-2026-store",
                }
                responses = []
                handler._json = lambda status, payload, extra=None: responses.append(
                    (status, payload, extra)
                )

                AgentHandler._login(handler)

                self.assertEqual(responses[0][0], 501)
                self.assertIn("FastAPI", responses[0][1]["error"])
        finally:
            if old is None:
                os.environ.pop("STORE_AGENT_MFA_ENFORCE", None)
            else:
                os.environ["STORE_AGENT_MFA_ENFORCE"] = old

    def test_mfa_startup_is_rejected_with_fastapi_hint(self):
        old = os.environ.get("STORE_AGENT_MFA_ENFORCE")
        os.environ["STORE_AGENT_MFA_ENFORCE"] = "1"
        try:
            with self.assertRaisesRegex(RuntimeError, "FastAPI"):
                validate_startup()
        finally:
            if old is None:
                os.environ.pop("STORE_AGENT_MFA_ENFORCE", None)
            else:
                os.environ["STORE_AGENT_MFA_ENFORCE"] = old

    def test_bootstrap_declares_core_only_capabilities(self):
        handler = object.__new__(AgentHandler)
        context = SecurityContext(
            user_id="manager01", tenant_id="LPL-001", store_id="WH-LION-001",
            role="manager", display_name="店长", session_id="session-1",
            expires_at="2099-01-01T00:00:00", permissions=permissions_for("manager"),
        )
        handler._require = lambda *args, **kwargs: context
        handler.tools = SimpleNamespace(
            tasks_for=lambda user, limit=50: [],
            data_status=lambda: {"store_id": "WH-LION-001", "platforms": {}},
        )
        handler.agent = SimpleNamespace(
            snapshot={"store": {"store_id": "WH-LION-001"}},
            llm_responder=None,
            tool_agent=None,
        )
        handler.auth_mode = "session"
        handler.path = "/api/bootstrap"
        responses = []
        handler._json = lambda status, payload, extra=None: responses.append(
            (status, payload, extra)
        )

        handler.do_GET()

        self.assertEqual(responses[0][0], 200)
        capabilities = responses[0][1]["capabilities"]
        self.assertFalse(capabilities["full"])
        self.assertFalse(capabilities["mfa"])
        self.assertFalse(capabilities["admin"])
        self.assertFalse(capabilities["callbacks"])
        self.assertFalse(capabilities["evidence_upload"])
        self.assertFalse(capabilities["outcomes"])