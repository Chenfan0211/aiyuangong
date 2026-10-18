import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app_fastapi import create_app
from auth import AuthStore
from governance import ApprovalStore
from security import sign_webhook


class ApprovalWebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "store_config.json"
        self.config.write_text((Path(__file__).resolve().parent / "store_config.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.auth = AuthStore(self.root / "ops.db", tenant_id="LPL-001")
        self.auth.create_user("manager01", "ShiZiZuo-2026-store", "店长", "manager", "LPL-001", "WH-LION-001")
        self.approvals = ApprovalStore(self.root / "ops.db", tenant_id="LPL-001")
        self.approval = self.approvals.request("WH-LION-001", "pricing:change", {"title": "调价"},
                                               requested_by="员工小李", reason="测试")
        os.environ["STORE_AGENT_APPROVAL_CALLBACK_SECRET"] = "webhook-test-secret"
        self.app = create_app(config_path=self.config, metrics_db=self.root / "metrics.db",
                              store_db=self.root / "ops.db", chat_db=self.root / "chat.db",
                              evidence_dir=self.root / "evidence")
        self.client = TestClient(self.app)

    def tearDown(self):
        os.environ.pop("STORE_AGENT_APPROVAL_CALLBACK_SECRET", None)
        self.client.close()
        self.tmp.cleanup()

    def payload(self, nonce="nonce-1"):
        return {"approval_id": self.approval["approval_id"], "decision": "approved",
                "decided_by": "manager01", "reason": "同意", "nonce": nonce,
                "idempotency_key": "event-" + nonce}

    def post_signed(self, body, nonce="nonce-1"):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        signed = sign_webhook("webhook-test-secret", raw)
        return self.client.post("/api/webhooks/approvals/mock", content=raw,
                                headers={"Content-Type": "application/json",
                                         "X-Mock-Signature": signed["signature"],
                                         "X-Mock-Timestamp": str(signed["timestamp"]),
                                         "X-Mock-Nonce": nonce,
                                         "X-Mock-Idempotency-Key": body["idempotency_key"]})

    def test_signed_callback_decides_and_is_idempotent(self):
        body = self.payload()
        first = self.post_signed(body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.approvals.get(self.approval["approval_id"])["status"], "approved")
        second_body = {**body, "nonce": "nonce-1b"}
        second = self.post_signed(second_body, nonce="nonce-1b")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["result"]["duplicate"])

    def test_bad_signature_is_rejected(self):
        body = self.payload("nonce-2")
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        response = self.client.post("/api/webhooks/approvals/mock", content=raw,
                                    headers={"Content-Type": "application/json",
                                             "X-Mock-Signature": "bad", "X-Mock-Timestamp": "1700000000",
                                             "X-Mock-Nonce": "nonce-2", "X-Mock-Idempotency-Key": body["idempotency_key"]})
        self.assertGreaterEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
