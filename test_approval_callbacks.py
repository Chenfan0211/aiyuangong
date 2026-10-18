# -*- coding: utf-8 -*-
"""审批回调适配层的契约测试：mock / 飞书 / 企微统一协议。"""
import base64
import hashlib
import json
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from approval_callbacks import (
    ApprovalCallbackAdapter,
    ApprovalCallbackError,
    create_approval_callback_adapter,
)
from governance import ApprovalStore
from security import sign_webhook
from task_store import TaskStore

STORE = "WH-LION-001"
STORE_B = "WH-LION-002"
TENANT = "LPL-001"
SIGNING_KEY = "local-callback-signing-key"  # noqa: secret - 测试夹具，不是第三方密钥
NOW = 1_800_000_000
FEISHU_TOKEN = "fixture-feishu-verification-token"
FEISHU_ENCRYPT_KEY = "fixture-feishu-encrypt-key"
WECOM_TOKEN = "fixture-wecom-callback-token"
WECOM_AES_KEY = base64.b64encode(b"W" * 32).decode("ascii").rstrip("=")
WECOM_CORP_ID = "ww-fixture-corp"
RESULT_KEYS = {
    "ok", "provider", "approval_id", "decision", "decided_by", "reason",
    "idempotency_key", "duplicate", "result", "timestamp",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ops.db"
        self.approvals = ApprovalStore(self.db, tenant_id=TENANT)
        self.tasks = TaskStore(self.db, tenant_id=TENANT)

    def tearDown(self):
        self.temp.cleanup()

    def approval(self, store_id=STORE, task_id=None):
        return self.approvals.request(
            store_id, "pricing:change", {"price": 168},
            requested_by="线上运营", task_id=task_id,
        )

    def adapter(self, provider="mock", **kwargs):
        options = dict(
            secret=SIGNING_KEY,
            db_target=self.db,
            approval_store=self.approvals,
            task_store=self.tasks,
            tenant_id=TENANT,
            store_id=STORE,
            now=lambda: NOW,
        )
        options.update(kwargs)
        return create_approval_callback_adapter(provider, **options)

    def envelope(self, provider, approval_id, decision="approved", *, key="cb-idem-1",
                 nonce="cb-nonce-1", timestamp=NOW, reason="", actor="manager01"):
        if provider == "mock":
            body = {
                "approval_id": approval_id,
                "decision": decision,
                "operator_id": actor,
                "reason": reason,
            }
            names = ("X-Callback-Timestamp", "X-Callback-Signature",
                     "X-Callback-Nonce", "X-Callback-Idempotency-Key")
        elif provider == "feishu":
            body = {
                "header": {"event_type": "card.action.trigger", "event_id": key},
                "event": {
                    "operator": {"open_id": actor},
                    "action": {"value": {
                        "approval_id": approval_id,
                        "decision": decision,
                        "reason": reason,
                    }},
                },
            }
            names = ("X-Lark-Request-Timestamp", "X-Lark-Signature",
                     "X-Lark-Request-Nonce", "X-Idempotency-Key")
        elif provider == "wecom":
            body = {
                "approval_id": approval_id,
                "action": decision,
                "user_id": actor,
                "comment": reason,
            }
            names = ("X-WeCom-Timestamp", "X-WeCom-Signature",
                     "X-WeCom-Nonce", "X-Idempotency-Key")
        else:
            raise AssertionError(provider)

        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True).encode("utf-8")
        signed = sign_webhook(SIGNING_KEY, raw, timestamp=timestamp)
        headers = {
            names[0]: str(signed["timestamp"]),
            names[1]: signed["signature"],
            names[2]: nonce,
            names[3]: key,
        }
        return raw, headers


    @staticmethod
    def _pad(raw, block_size):
        padding = block_size - (len(raw) % block_size)
        return raw + bytes([padding]) * padding

    def feishu_encrypted_envelope(self, approval_id, decision="approved", *, key="cb-idem-1",
                                  nonce="cb-nonce-1", timestamp=NOW, reason="",
                                  actor="ou_manager", token=FEISHU_TOKEN,
                                  encrypt_key=FEISHU_ENCRYPT_KEY, encrypt_value=None,
                                  signature_key=None):
        plaintext = {
            "token": token,
            "header": {"event_type": "card.action.trigger", "event_id": key},
            "event": {
                "operator": {"open_id": actor},
                "action": {"value": {
                    "approval_id": approval_id,
                    "decision": decision,
                    "reason": reason,
                }},
            },
        }
        if encrypt_value is None:
            encoded = json.dumps(plaintext, ensure_ascii=False, separators=(",", ":"),
                                 sort_keys=True).encode("utf-8")
            iv = b"0123456789abcdef"
            cipher = Cipher(algorithms.AES(hashlib.sha256(encrypt_key.encode("utf-8")).digest()),
                            modes.CBC(iv)).encryptor()
            ciphertext = cipher.update(self._pad(encoded, 16)) + cipher.finalize()
            encrypt_value = base64.b64encode(iv + ciphertext).decode("ascii")
        raw = json.dumps({"encrypt": encrypt_value}, ensure_ascii=False,
                         separators=(",", ":"), sort_keys=True).encode("utf-8")
        material = f"{timestamp}{nonce}{signature_key or encrypt_key}".encode("utf-8") + raw
        signature = hashlib.sha256(material).hexdigest()
        headers = {
            "X-Lark-Request-Timestamp": str(timestamp),
            "X-Lark-Signature": signature,
            "X-Lark-Request-Nonce": nonce,
            "X-Idempotency-Key": key,
        }
        return raw, headers

    def wecom_encrypt_value(self, plaintext, *, encoding_aes_key=WECOM_AES_KEY,
                            receive_id=WECOM_CORP_ID):
        key = base64.b64decode(encoding_aes_key + "=")
        payload = plaintext.encode("utf-8")
        message = b"R" * 16 + struct.pack(">I", len(payload)) + payload + receive_id.encode("utf-8")
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
        encrypted = cipher.update(self._pad(message, 32)) + cipher.finalize()
        return base64.b64encode(encrypted).decode("ascii")

    def wecom_envelope(self, approval_id, decision="approved", *, key="cb-idem-1",
                       nonce="cb-nonce-1", timestamp=NOW, reason="", actor="wecom-manager",
                       token=WECOM_TOKEN, encoding_aes_key=WECOM_AES_KEY,
                       receive_id=WECOM_CORP_ID, encrypted=None):
        plaintext = (
            "<xml>"
            f"<ApprovalId>{approval_id}</ApprovalId>"
            f"<Decision>{decision}</Decision>"
            f"<UserId>{actor}</UserId>"
            f"<Reason>{reason}</Reason>"
            f"<EventKey>{key}</EventKey>"
            "</xml>"
        )
        encrypted = encrypted or self.wecom_encrypt_value(
            plaintext, encoding_aes_key=encoding_aes_key, receive_id=receive_id)
        raw = f"<xml><ToUserName>{receive_id}</ToUserName><Encrypt>{encrypted}</Encrypt></xml>".encode("utf-8")
        signature = hashlib.sha1(
            "".join(sorted((token, str(timestamp), nonce, encrypted))).encode("utf-8")
        ).hexdigest()
        headers = {
            "msg_signature": signature,
            "timestamp": str(timestamp),
            "nonce": nonce,
            "X-Idempotency-Key": key,
        }
        return raw, headers


class AdapterContractTests(Base):
    def test_mock_approval_returns_fixed_structure_and_decides(self):
        approval = self.approval()
        raw, headers = self.envelope("mock", approval["approval_id"], actor="manager01",
                                     reason="同意试点")
        result = self.adapter("mock").handle(raw, headers)

        self.assertEqual(set(result), RESULT_KEYS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "mock")
        self.assertEqual(result["approval_id"], approval["approval_id"])
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(result["decided_by"], "manager01")
        self.assertEqual(result["reason"], "同意试点")
        self.assertEqual(result["idempotency_key"], "cb-idem-1")
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["result"]["approval"]["status"], "approved")
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "approved")

    def test_feishu_nested_payload_and_alias_action(self):
        approval = self.approval()
        raw, headers = self.envelope("feishu", approval["approval_id"], decision="approve",
                                     actor="ou_manager")
        result = self.adapter("feishu").handle(raw, headers)
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(result["decided_by"], "ou_manager")
        self.assertEqual(self.approvals.get(approval["approval_id"])["decided_by"], "ou_manager")

    def test_wecom_payload_and_reject_alias(self):
        approval = self.approval()
        raw, headers = self.envelope("wecom", approval["approval_id"], decision="reject",
                                     reason="毛利不达标", actor="wecom-manager")
        result = self.adapter("wecom").handle(raw, headers)
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["reason"], "毛利不达标")
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "rejected")

    def test_feishu_encrypted_real_protocol_decrypts_and_decides(self):
        approval = self.approval()
        raw, headers = self.feishu_encrypted_envelope(
            approval["approval_id"], reason="同意试点", actor="ou_manager")
        result = self.adapter(
            "feishu", verification_token=FEISHU_TOKEN, encrypt_key=FEISHU_ENCRYPT_KEY,
        ).handle(raw, headers)
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(result["decided_by"], "ou_manager")
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "approved")

    def test_wecom_real_xml_protocol_decrypts_and_decides(self):
        approval = self.approval()
        raw, headers = self.wecom_envelope(
            approval["approval_id"], decision="reject", reason="毛利不达标",
            actor="wecom-manager")
        result = self.adapter(
            "wecom", token=WECOM_TOKEN, encoding_aes_key=WECOM_AES_KEY,
            receive_id=WECOM_CORP_ID,
        ).handle(raw, headers)
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["reason"], "毛利不达标")
        self.assertEqual(result["decided_by"], "wecom-manager")
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "rejected")

    def test_wecom_url_verification_returns_decrypted_challenge(self):
        encrypted = self.wecom_encrypt_value("echo-ok")
        timestamp, nonce = NOW, "wecom-url-nonce"
        signature = hashlib.sha1(
            "".join(sorted((WECOM_TOKEN, str(timestamp), nonce, encrypted))).encode("utf-8")
        ).hexdigest()
        params = {
            "echostr": encrypted,
            "msg_signature": signature,
            "timestamp": str(timestamp),
            "nonce": nonce,
        }
        result = self.adapter(
            "wecom", token=WECOM_TOKEN, encoding_aes_key=WECOM_AES_KEY,
            receive_id=WECOM_CORP_ID,
        ).handle(b"", params)
        self.assertEqual(result["provider"], "wecom")
        self.assertEqual(result["challenge"], "echo-ok")
        self.assertFalse(result["duplicate"])

    def test_factory_accepts_direct_adapter_class(self):
        adapter = ApprovalCallbackAdapter(
            "mock", secret=SIGNING_KEY, db_target=self.db, approval_store=self.approvals,
            tenant_id=TENANT, store_id=STORE, now=lambda: NOW,
        )
        self.assertEqual(adapter.provider, "mock")


class VerificationTests(Base):
    def test_bad_signature_is_rejected_without_consuming_nonce(self):
        approval = self.approval()
        raw, headers = self.envelope("mock", approval["approval_id"])
        bad = dict(headers)
        bad["X-Callback-Signature"] = "0" * 64
        with self.assertRaises(ApprovalCallbackError) as ctx:
            self.adapter("mock").handle(raw, bad)
        self.assertEqual(ctx.exception.code, "verification_failed")
        # 坏签名不得占用 nonce，合法重试仍然可以成功。
        result = self.adapter("mock").handle(raw, headers)
        self.assertTrue(result["ok"])

    def test_expired_timestamp_is_rejected(self):
        approval = self.approval()
        raw, headers = self.envelope("mock", approval["approval_id"], timestamp=NOW - 400)
        with self.assertRaises(ApprovalCallbackError) as ctx:
            self.adapter("mock").handle(raw, headers)
        self.assertEqual(ctx.exception.code, "verification_failed")

    def test_required_callback_metadata_is_checked(self):
        approval = self.approval()
        for header, field in (
            ("X-Callback-Signature", "signature"),
            ("X-Callback-Timestamp", "timestamp"),
            ("X-Callback-Nonce", "nonce"),
            ("X-Callback-Idempotency-Key", "idempotency_key"),
        ):
            with self.subTest(field=field):
                raw, headers = self.envelope("mock", approval["approval_id"],
                                             key=f"key-{field}", nonce=f"nonce-{field}")
                headers.pop(header)
                with self.assertRaises(ApprovalCallbackError) as ctx:
                    self.adapter("mock").handle(raw, headers)
                self.assertEqual(ctx.exception.code, f"missing_{field}")

    def test_nonce_replay_is_rejected(self):
        approval = self.approval()
        raw, headers = self.envelope("mock", approval["approval_id"])
        adapter = self.adapter("mock")
        self.assertTrue(adapter.handle(raw, headers)["ok"])
        replay_raw, replay_headers = self.envelope(
            "mock", approval["approval_id"], key="cb-idem-2", nonce="cb-nonce-1")
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(replay_raw, replay_headers)
        self.assertEqual(ctx.exception.code, "replay")

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(ApprovalCallbackError) as ctx:
            self.adapter("mock").handle(b"{not-json", {})
        self.assertEqual(ctx.exception.code, "invalid_payload")


    def test_feishu_real_bad_signature_does_not_consume_nonce(self):
        approval = self.approval()
        raw, headers = self.feishu_encrypted_envelope(
            approval["approval_id"], nonce="feishu-bad-signature-nonce")
        bad = dict(headers)
        bad["X-Lark-Signature"] = "0" * 64
        adapter = self.adapter(
            "feishu", verification_token=FEISHU_TOKEN, encrypt_key=FEISHU_ENCRYPT_KEY,
        )
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw, bad)
        self.assertEqual(ctx.exception.code, "verification_failed")
        self.assertTrue(adapter.handle(raw, headers)["ok"])

    def test_feishu_real_decrypt_failure_is_rejected(self):
        approval = self.approval()
        raw, headers = self.feishu_encrypted_envelope(
            approval["approval_id"],
            encrypt_value=base64.b64encode(b"not-aes-ciphertext").decode("ascii"),
        )
        adapter = self.adapter(
            "feishu", verification_token=FEISHU_TOKEN, encrypt_key=FEISHU_ENCRYPT_KEY,
        )
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw, headers)
        self.assertEqual(ctx.exception.code, "decryption_failed")

    def test_feishu_real_verification_token_mismatch_is_rejected(self):
        approval = self.approval()
        raw, headers = self.feishu_encrypted_envelope(
            approval["approval_id"], token="wrong-verification-token",  # noqa: secret
        )
        adapter = self.adapter(
            "feishu", verification_token=FEISHU_TOKEN, encrypt_key=FEISHU_ENCRYPT_KEY,
        )
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw, headers)
        self.assertEqual(ctx.exception.code, "verification_failed")

    def test_wecom_real_bad_signature_does_not_consume_nonce(self):
        approval = self.approval()
        raw, headers = self.wecom_envelope(
            approval["approval_id"], nonce="wecom-bad-signature-nonce")
        bad = dict(headers)
        bad["msg_signature"] = "0" * 40
        adapter = self.adapter(
            "wecom", token=WECOM_TOKEN, encoding_aes_key=WECOM_AES_KEY,
            receive_id=WECOM_CORP_ID,
        )
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw, bad)
        self.assertEqual(ctx.exception.code, "verification_failed")
        self.assertTrue(adapter.handle(raw, headers)["ok"])

    def test_wecom_real_decrypt_failure_is_rejected(self):
        approval = self.approval()
        raw, headers = self.wecom_envelope(
            approval["approval_id"],
            encrypted=base64.b64encode(b"not-aes-ciphertext").decode("ascii"),
        )
        adapter = self.adapter(
            "wecom", token=WECOM_TOKEN, encoding_aes_key=WECOM_AES_KEY,
            receive_id=WECOM_CORP_ID,
        )
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw, headers)
        self.assertEqual(ctx.exception.code, "decryption_failed")

    def test_wecom_real_receiveid_mismatch_is_rejected(self):
        approval = self.approval()
        raw, headers = self.wecom_envelope(
            approval["approval_id"], receive_id="another-corp")
        adapter = self.adapter(
            "wecom", token=WECOM_TOKEN, encoding_aes_key=WECOM_AES_KEY,
            receive_id=WECOM_CORP_ID,
        )
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw, headers)
        self.assertEqual(ctx.exception.code, "receive_id_mismatch")

class IdempotencyTests(Base):
    def test_duplicate_idempotency_key_with_new_nonce_does_not_execute_twice(self):
        approval = self.approval()
        calls = []

        def apply(approval_id, decision, actor, reason):
            calls.append((approval_id, decision, actor, reason))
            return {"approval": self.approvals.decide(approval_id, decision, actor, reason),
                    "task": None, "task_note": None}

        adapter = self.adapter("mock", decision_applier=apply)
        raw, headers = self.envelope("mock", approval["approval_id"])
        first = adapter.handle(raw, headers)
        raw2, headers2 = self.envelope("mock", approval["approval_id"],
                                       key="cb-idem-1", nonce="cb-nonce-2")
        second = adapter.handle(raw2, headers2)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["result"]["approval"]["status"], "approved")

    def test_same_idempotency_key_with_different_decision_is_conflict(self):
        approval = self.approval()
        adapter = self.adapter("mock")
        raw, headers = self.envelope("mock", approval["approval_id"])
        adapter.handle(raw, headers)
        raw2, headers2 = self.envelope("mock", approval["approval_id"], decision="rejected",
                                       reason="不同意", key="cb-idem-1", nonce="cb-nonce-2")
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw2, headers2)
        self.assertEqual(ctx.exception.code, "idempotency_conflict")

    def test_receipt_survives_new_adapter_instance(self):
        approval = self.approval()
        first = self.adapter("mock")
        raw, headers = self.envelope("mock", approval["approval_id"])
        first.handle(raw, headers)
        second = self.adapter("mock")
        raw2, headers2 = self.envelope("mock", approval["approval_id"],
                                       key="cb-idem-1", nonce="cb-nonce-2")
        result = second.handle(raw2, headers2)
        self.assertTrue(result["duplicate"])
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "approved")


    def test_feishu_real_duplicate_event_with_new_nonce_does_not_execute_twice(self):
        approval = self.approval()
        calls = []

        def apply(approval_id, decision, actor, reason):
            calls.append((approval_id, decision, actor, reason))
            return {"approval": self.approvals.decide(approval_id, decision, actor, reason),
                    "task": None, "task_note": None}

        adapter = self.adapter(
            "feishu", verification_token=FEISHU_TOKEN, encrypt_key=FEISHU_ENCRYPT_KEY,
            decision_applier=apply,
        )
        raw, headers = self.feishu_encrypted_envelope(approval["approval_id"])
        encrypted = json.loads(raw.decode("utf-8"))["encrypt"]
        first = adapter.handle(raw, headers)
        raw2, headers2 = self.feishu_encrypted_envelope(
            approval["approval_id"], key="cb-idem-1", nonce="cb-nonce-2",
            encrypt_value=encrypted)
        second = adapter.handle(raw2, headers2)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(calls), 1)

    def test_wecom_real_duplicate_event_with_new_nonce_does_not_execute_twice(self):
        approval = self.approval()
        calls = []

        def apply(approval_id, decision, actor, reason):
            calls.append((approval_id, decision, actor, reason))
            return {"approval": self.approvals.decide(approval_id, decision, actor, reason),
                    "task": None, "task_note": None}

        adapter = self.adapter(
            "wecom", token=WECOM_TOKEN, encoding_aes_key=WECOM_AES_KEY,
            receive_id=WECOM_CORP_ID, decision_applier=apply,
        )
        raw, headers = self.wecom_envelope(approval["approval_id"])
        encrypted = ET.fromstring(raw).findtext("Encrypt")
        first = adapter.handle(raw, headers)
        raw2, headers2 = self.wecom_envelope(
            approval["approval_id"], key="cb-idem-1", nonce="cb-nonce-2",
            encrypted=encrypted)
        second = adapter.handle(raw2, headers2)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(calls), 1)

class DecisionSafetyTests(Base):
    def test_invalid_approval_id_is_rejected(self):
        raw, headers = self.envelope("mock", "apr-does-not-exist")
        with self.assertRaises(ApprovalCallbackError) as ctx:
            self.adapter("mock").handle(raw, headers)
        self.assertEqual(ctx.exception.code, "invalid_approval_id")

    def test_illegal_action_is_rejected(self):
        approval = self.approval()
        raw, headers = self.envelope("mock", approval["approval_id"], decision="execute")
        with self.assertRaises(ApprovalCallbackError) as ctx:
            self.adapter("mock").handle(raw, headers)
        self.assertEqual(ctx.exception.code, "invalid_action")
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "pending")

    def test_rejection_requires_reason_and_does_not_decide(self):
        approval = self.approval()
        raw, headers = self.envelope("mock", approval["approval_id"], decision="rejected")
        with self.assertRaises(ApprovalCallbackError) as ctx:
            self.adapter("mock").handle(raw, headers)
        self.assertEqual(ctx.exception.code, "reason_required")
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "pending")

    def test_cross_store_approval_is_rejected(self):
        approval = self.approval(store_id=STORE_B)
        raw, headers = self.envelope("mock", approval["approval_id"])
        with self.assertRaises(ApprovalCallbackError) as ctx:
            self.adapter("mock").handle(raw, headers)
        self.assertEqual(ctx.exception.code, "scope_mismatch")
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "pending")

    def test_already_decided_same_action_returns_duplicate_without_execution(self):
        approval = self.approval()
        self.approvals.decide(approval["approval_id"], "approved", "manager01", "已线下批准")
        calls = []
        adapter = self.adapter("mock", decision_applier=lambda *args: calls.append(args))
        raw, headers = self.envelope("mock", approval["approval_id"], key="cb-idem-new")
        result = adapter.handle(raw, headers)
        self.assertTrue(result["duplicate"])
        self.assertEqual(len(calls), 0)
        self.assertEqual(result["result"]["approval"]["status"], "approved")

    def test_already_decided_other_action_is_rejected(self):
        approval = self.approval()
        self.approvals.decide(approval["approval_id"], "approved", "manager01", "已线下批准")
        raw, headers = self.envelope("mock", approval["approval_id"], decision="rejected",
                                     reason="不同意", key="cb-idem-new")
        with self.assertRaises(ApprovalCallbackError) as ctx:
            self.adapter("mock").handle(raw, headers)
        self.assertEqual(ctx.exception.code, "already_decided")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ApprovalCallbackError) as ctx:
            self.adapter("other")
        self.assertEqual(ctx.exception.code, "unknown_provider")


    def test_feishu_real_illegal_action_is_rejected(self):
        approval = self.approval()
        raw, headers = self.feishu_encrypted_envelope(
            approval["approval_id"], decision="execute")
        adapter = self.adapter(
            "feishu", verification_token=FEISHU_TOKEN, encrypt_key=FEISHU_ENCRYPT_KEY,
        )
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw, headers)
        self.assertEqual(ctx.exception.code, "invalid_action")
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "pending")

    def test_wecom_real_invalid_approval_id_is_rejected(self):
        raw, headers = self.wecom_envelope("apr-does-not-exist")
        adapter = self.adapter(
            "wecom", token=WECOM_TOKEN, encoding_aes_key=WECOM_AES_KEY,
            receive_id=WECOM_CORP_ID,
        )
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw, headers)
        self.assertEqual(ctx.exception.code, "invalid_approval_id")

    def test_feishu_real_cross_store_approval_is_rejected(self):
        approval = self.approval(store_id=STORE_B)
        raw, headers = self.feishu_encrypted_envelope(approval["approval_id"])
        adapter = self.adapter(
            "feishu", verification_token=FEISHU_TOKEN, encrypt_key=FEISHU_ENCRYPT_KEY,
        )
        with self.assertRaises(ApprovalCallbackError) as ctx:
            adapter.handle(raw, headers)
        self.assertEqual(ctx.exception.code, "scope_mismatch")
        self.assertEqual(self.approvals.get(approval["approval_id"])["status"], "pending")

class TaskIntegrationTests(Base):
    def test_approved_callback_moves_linked_task_to_ready(self):
        task_id = "task-callback-1"
        self.tasks.save_plan({
            "date": "2026-10-19",
            "tasks": [{
                "task_id": task_id,
                "category": "package",
                "title": "套餐调整",
                "assignee": "manager01",
                "priority": "P1",
                "reason": "测试审批回调",
                "target": {"package_id": "MT-1"},
                "evidence_required": ["脱敏订单号"],
                "requires_approval": True,
                "due_at": "2026-10-20T00:00:00",
            }],
        }, STORE)
        approval = self.approval(task_id=task_id)
        raw, headers = self.envelope("mock", approval["approval_id"], actor="manager01")
        result = self.adapter("mock").handle(raw, headers)
        self.assertEqual(result["result"]["task"]["status"], "ready")
        self.assertEqual(self.tasks.get_task(task_id)["status"], "ready")


if __name__ == "__main__":
    unittest.main()