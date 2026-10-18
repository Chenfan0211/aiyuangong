#!/usr/bin/env python3
"""审批回调适配层：统一接收 mock / 飞书 / 企微回调并调用现有审批决策链路。

设计约束：

* 厂商协议解析只在 adapter 内完成，核心审批逻辑不依赖任何厂商 SDK；
* 复用 ``security.py`` 的 HMAC-SHA256、时间窗和 nonce 去重能力；
* idempotency key 在本地回执表中持久化，重复投递不会再次推进审批或任务；
* 回调只允许 approved / rejected 两种固定决策，未知动作直接拒绝。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from db import Database
from governance import ApprovalStore, apply_decision
from security import ReplayGuard, verify_inbound
from task_store import TaskStore

SUPPORTED_PROVIDERS = ("mock", "feishu", "wecom")
DEFAULT_TOLERANCE_SECONDS = 300
MAX_IDEMPOTENCY_KEY_LENGTH = 200

CALLBACK_RECEIPT_DDL = """
create table if not exists approval_callback_receipts (
  tenant_id text not null default '',
  store_id text not null,
  provider text not null,
  idempotency_key text not null,
  approval_id text not null,
  decision text not null,
  actor_id text not null,
  reason text,
  request_fingerprint text not null,
  result_json text not null,
  created_at text not null,
  primary key (tenant_id, store_id, provider, idempotency_key)
);
create index if not exists idx_approval_callback_receipts_approval
  on approval_callback_receipts(tenant_id, store_id, approval_id);
"""

SIGNATURE_HEADERS = {
    "mock": ("X-Mock-Signature", "X-Callback-Signature", "X-Approval-Signature"),
    "feishu": ("X-Lark-Signature", "X-Feishu-Signature", "X-Callback-Signature",
               "X-Approval-Signature"),
    "wecom": ("X-WeCom-Signature", "X-WorkWechat-Signature", "X-Callback-Signature",
              "X-Approval-Signature"),
}
TIMESTAMP_HEADERS = {
    "mock": ("X-Mock-Timestamp", "X-Callback-Timestamp", "X-Approval-Timestamp"),
    "feishu": ("X-Lark-Request-Timestamp", "X-Feishu-Timestamp", "X-Callback-Timestamp",
               "X-Approval-Timestamp"),
    "wecom": ("X-WeCom-Timestamp", "X-Callback-Timestamp", "X-Approval-Timestamp"),
}
NONCE_HEADERS = {
    "mock": ("X-Mock-Nonce", "X-Callback-Nonce", "X-Approval-Nonce"),
    "feishu": ("X-Lark-Request-Nonce", "X-Feishu-Nonce", "X-Callback-Nonce",
               "X-Approval-Nonce"),
    "wecom": ("X-WeCom-Nonce", "X-Callback-Nonce", "X-Approval-Nonce"),
}
IDEMPOTENCY_HEADERS = {
    "mock": ("X-Mock-Idempotency-Key", "X-Callback-Idempotency-Key",
             "X-Approval-Idempotency-Key"),
    "feishu": ("X-Idempotency-Key", "X-Lark-Request-Id", "X-Callback-Idempotency-Key"),
    "wecom": ("X-Idempotency-Key", "X-WeCom-Message-Id", "X-Callback-Idempotency-Key"),
}

APPROVED_ALIASES = {"approved", "approve", "pass", "agree", "同意", "通过"}
REJECTED_ALIASES = {"rejected", "reject", "deny", "拒绝", "驳回", "不通过"}
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ApprovalCallbackError(ValueError):
    """审批回调拒绝错误；``code`` 是稳定的程序判断字段。"""

    def __init__(self, code: str, reason: str = "", status_code: int = 400):
        self.code = str(code)
        self.reason = str(reason or self.code)
        self.status_code = int(status_code)
        super().__init__(self.reason)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "reason": self.reason}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _get_ci(mapping: Any, *keys: str) -> Any:
    """不区分大小写地读取字段，兼容飞书/企微常见的 camelCase。"""
    values = _as_dict(mapping)
    lowered = {str(key).lower(): value for key, value in values.items()}
    for key in keys:
        value = lowered.get(str(key).lower())
        if value is not None and value != "":
            return value
    return None


def _header(headers: Any, names: tuple[str, ...]) -> Any:
    values = {str(key).lower(): value for key, value in _as_dict(headers).items()}
    for name in names:
        value = values.get(name.lower())
        if value is not None and value != "":
            return value
    return None


def _body_bytes(body: Any) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, Mapping):
        # 便于本地 mock/测试；真实 HTTP 请求必须直接传原始 body bytes 才能验签。
        return json.dumps(dict(body), ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True).encode("utf-8")
    raise ApprovalCallbackError("invalid_payload", "回调体必须是 bytes、str 或 JSON 对象")


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ApprovalCallbackError("invalid_payload", f"回调体不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ApprovalCallbackError("invalid_payload", "回调体必须是 JSON 对象")
    return value


def _required(value: Any, field: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ApprovalCallbackError(f"missing_{field}", f"缺少回调字段：{field}")
    return value


def _normalise_decision(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in APPROVED_ALIASES:
        return "approved"
    if normalized in REJECTED_ALIASES:
        return "rejected"
    raise ApprovalCallbackError("invalid_action", f"不支持的审批动作：{value}")


def _valid_idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        raise ApprovalCallbackError("missing_idempotency_key", "缺少 idempotency key")
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH or CONTROL_CHARS.search(key):
        raise ApprovalCallbackError("invalid_idempotency_key", "idempotency key 非法")
    return key


def _fingerprint(provider: str, approval_id: str, decision: str, actor: str, reason: str) -> str:
    material = "|".join((provider, approval_id, decision, actor, reason))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _base64_decode(value: Any, field: str) -> bytes:
    try:
        return base64.b64decode(str(value), validate=True)
    except (TypeError, ValueError) as exc:
        raise ApprovalCallbackError("decryption_failed", f"{field} 不是合法 Base64") from exc


def _decode_aes_cbc(ciphertext: bytes, key: bytes, iv: bytes, block_size: int = 128) -> bytes:
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(block_size).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except Exception as exc:                                   # noqa: BLE001 - 统一映射为协议解密失败
        raise ApprovalCallbackError("decryption_failed", "回调内容解密失败") from exc


def _xml_root(raw: bytes) -> ET.Element:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ApprovalCallbackError("invalid_payload", f"回调体不是合法 XML：{exc}") from exc
    if str(root.tag).lower() != "xml":
        raise ApprovalCallbackError("invalid_payload", "回调 XML 根节点必须是 xml")
    return root


def _xml_encrypted(root: ET.Element) -> str:
    for child in root:
        if str(child.tag).lower() == "encrypt" and str(child.text or "").strip():
            return str(child.text).strip()
    raise ApprovalCallbackError("missing_encrypt", "企微 XML 缺少 Encrypt 字段")


def _xml_object(raw: bytes) -> dict[str, Any]:
    root = _xml_root(raw)
    values = {str(child.tag): str(child.text or "").strip() for child in root}
    event_key = next(
        (value for key, value in values.items() if key.lower() == "eventkey"), ""
    )
    if event_key.startswith("{") and event_key.endswith("}"):
        try:
            nested = json.loads(event_key)
        except json.JSONDecodeError as exc:
            raise ApprovalCallbackError("invalid_payload", "EventKey 不是合法 JSON") from exc
        if isinstance(nested, dict):
            values.update(nested)
    return values


def _within_tolerance(timestamp: int, now: int, tolerance: int) -> bool:
    return abs(now - timestamp) <= tolerance


class ApprovalCallbackAdapter:
    """统一的审批回调入口；provider 只影响字段解析和头名别名。"""

    def __init__(
        self,
        provider: str,
        *,
        secret: str,
        db_target: Any,
        approval_store: ApprovalStore,
        task_store: Optional[TaskStore] = None,
        tenant_id: str = "",
        store_id: str = "",
        replay_guard: Optional[ReplayGuard] = None,
        decision_applier: Optional[Callable[[str, str, str, str], dict[str, Any]]] = None,
        now: Optional[Callable[[], int]] = None,
        tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    ):
        self.provider = str(provider or "").strip().lower()
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ApprovalCallbackError("unknown_provider", f"不支持的审批回调提供方：{provider}")
        if not secret:
            raise ApprovalCallbackError("missing_secret", "审批回调签名密钥不能为空", status_code=500)
        inferred_tenant = getattr(getattr(approval_store, "db", None), "tenant_id", "") or ""
        self.tenant_id = str(tenant_id or inferred_tenant)
        self.store_id = str(store_id or "").strip()
        if not self.store_id:
            raise ApprovalCallbackError("missing_store_id", "审批回调必须绑定 store_id", status_code=500)

        self.secret = str(secret)
        self.approval_store = approval_store
        self.task_store = task_store
        self.db = Database(db_target, tenant_id=self.tenant_id)
        self.guard = replay_guard or ReplayGuard(db_target, tenant_id=self.tenant_id)
        self._decision_applier = decision_applier
        self._now = now or (lambda: int(datetime.now().timestamp()))
        self.tolerance = int(tolerance)
        self._init_receipts()

    def _init_receipts(self) -> None:
        with self.db.connection() as conn:
            conn.executescript(CALLBACK_RECEIPT_DDL)

    # ---------------- 协议解析 ----------------

    def _extract(self, payload: dict[str, Any], headers: Any) -> dict[str, Any]:
        value = payload
        operator: Any = {}
        if self.provider == "feishu":
            event = _as_dict(payload.get("event"))
            action = _as_dict(event.get("action"))
            value = _as_dict(action.get("value")) or event or payload
            operator = _as_dict(event.get("operator")) or _as_dict(payload.get("operator"))
        elif self.provider == "wecom":
            value = payload

        approval_id = (_get_ci(value, "approval_id", "approvalId", "id")
                       or _get_ci(payload, "approval_id", "approvalId", "id"))
        decision = (_get_ci(value, "decision", "action", "result", "approval_action",
                            "approvalAction", "approval_result", "approvalResult")
                    or _get_ci(payload, "decision", "action", "result", "approval_action",
                               "approvalAction", "approval_result", "approvalResult"))
        actor = (_get_ci(value, "decided_by", "decidedBy", "operator_id", "operatorId",
                         "user_id", "userId", "open_id", "openId", "from_user_name",
                         "fromUserName", "from_user_id", "fromUserId", "from_user",
                         "fromUser")
                 or _get_ci(operator, "open_id", "openId", "user_id", "userId", "id")
                 or _get_ci(payload, "decided_by", "decidedBy", "operator_id", "operatorId",
                            "user_id", "userId", "open_id", "openId", "from_user_name",
                            "fromUserName", "from_user_id", "fromUserId", "from_user",
                            "fromUser"))
        reason = (_get_ci(value, "reason", "comment", "note", "message", "remark")
                  or _get_ci(payload, "reason", "comment", "note", "message", "remark") or "")
        signature = (_header(headers, SIGNATURE_HEADERS[self.provider])
                     or _get_ci(value, "signature", "sign")
                     or _get_ci(payload, "signature", "sign"))
        timestamp = (_header(headers, TIMESTAMP_HEADERS[self.provider])
                     or _get_ci(value, "timestamp", "ts", "time")
                     or _get_ci(payload, "timestamp", "ts", "time"))
        nonce = (_header(headers, NONCE_HEADERS[self.provider])
                 or _get_ci(value, "nonce")
                 or _get_ci(payload, "nonce"))
        idempotency_key = (_header(headers, IDEMPOTENCY_HEADERS[self.provider])
                           or _get_ci(value, "idempotency_key", "idempotencyKey",
                                      "event_id", "eventId", "event_key", "eventKey",
                                      "msg_id", "msgId", "message_id", "messageId")
                           or _get_ci(payload, "idempotency_key", "idempotencyKey",
                                      "event_id", "eventId", "event_key", "eventKey",
                                      "msg_id", "msgId", "message_id", "messageId")
                           or _get_ci(_as_dict(payload.get("header")), "event_id", "eventId"))

        return {
            "approval_id": _required(approval_id, "approval_id"),
            "decision": _required(decision, "decision"),
            "actor": _required(actor, "actor"),
            "reason": str(reason or "").strip(),
            "signature": _required(signature, "signature"),
            "timestamp": _required(timestamp, "timestamp"),
            "nonce": _required(nonce, "nonce"),
            "idempotency_key": _valid_idempotency_key(idempotency_key),
        }

    def parse(self, body: Any, headers: Any = None) -> dict[str, Any]:
        """解析厂商 payload 并返回规范化字段；不做签名验证，供调试和内部使用。"""
        return self._extract(_json_object(_body_bytes(body)), headers or {})

    # ---------------- 主流程 ----------------

    def _prepare_inbound(self, raw: bytes, headers: Any) -> dict[str, Any]:
        parsed = self._extract(_json_object(raw), headers)
        verification = verify_inbound(
            self.secret,
            raw,
            parsed["signature"],
            parsed["timestamp"],
            str(parsed["nonce"]),
            self.guard,
            now=int(self._now()),
            tolerance=self.tolerance,
            source=self.provider,
        )
        if not verification.get("ok"):
            reason = str(verification.get("reason") or "回调验签失败")
            code = "replay" if "nonce" in reason else "verification_failed"
            raise ApprovalCallbackError(code, reason)
        return {"parsed": parsed, "timestamp": int(verification["timestamp"])}

    @staticmethod
    def _challenge_response(prepared: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": str(prepared.get("provider") or ""),
            "challenge": str(prepared.get("challenge") or ""),
            "echostr": str(prepared.get("challenge") or ""),
            "duplicate": False,
            "timestamp": int(prepared.get("timestamp") or 0),
        }

    def handle(self, body: Any, headers: Any = None) -> dict[str, Any]:
        raw = _body_bytes(body)
        prepared = self._prepare_inbound(raw, headers or {})
        if "challenge" in prepared:
            return self._challenge_response({**prepared, "provider": self.provider})
        parsed = prepared["parsed"]
        timestamp = int(prepared["timestamp"])

        decision = _normalise_decision(parsed["decision"])
        actor = str(parsed["actor"]).strip()
        reason = str(parsed["reason"]).strip()
        if decision == "rejected" and not reason:
            raise ApprovalCallbackError("reason_required", "驳回审批必须填写原因")

        try:
            approval = self.approval_store.get(str(parsed["approval_id"]))
        except ValueError as exc:
            raise ApprovalCallbackError("invalid_approval_id", str(exc)) from exc

        self._assert_scope(approval)
        fingerprint = _fingerprint(
            self.provider, str(parsed["approval_id"]), decision, actor, reason,
        )
        receipt = self._receipt(str(parsed["idempotency_key"]))
        if receipt:
            self._assert_same_request(receipt, fingerprint)
            return self._response(
                parsed["approval_id"], decision, actor, reason, parsed["idempotency_key"],
                json.loads(receipt["result_json"]), duplicate=True, timestamp=timestamp,
            )

        if approval.get("status") != "pending":
            if approval.get("status") == decision:
                result = self._existing_result(approval)
                self._save_receipt(
                    parsed["approval_id"], decision, actor, reason,
                    parsed["idempotency_key"], fingerprint, result,
                )
                return self._response(
                    parsed["approval_id"], decision, actor, reason, parsed["idempotency_key"],
                    result, duplicate=True, timestamp=timestamp,
                )
            raise ApprovalCallbackError(
                "already_decided",
                f"审批单已是 {approval.get('status')}，不能改为 {decision}",
            )

        result = self._execute(str(parsed["approval_id"]), decision, actor, reason)
        duplicate = False
        try:
            self._save_receipt(
                parsed["approval_id"], decision, actor, reason,
                parsed["idempotency_key"], fingerprint, result,
            )
        except ApprovalCallbackError:
            raise
        except Exception:                                   # noqa: BLE001 - 竞态下按回执判定
            receipt = self._receipt(str(parsed["idempotency_key"]))
            if not receipt:
                raise
            self._assert_same_request(receipt, fingerprint)
            result = json.loads(receipt["result_json"])
            duplicate = True
        return self._response(
            parsed["approval_id"], decision, actor, reason, parsed["idempotency_key"],
            result, duplicate=duplicate, timestamp=timestamp,
        )

    def _assert_scope(self, approval: dict[str, Any]) -> None:
        if str(approval.get("store_id") or "") != self.store_id:
            raise ApprovalCallbackError("scope_mismatch", "审批单不属于当前门店")
        if self.tenant_id and str(approval.get("tenant_id") or "") != self.tenant_id:
            raise ApprovalCallbackError("scope_mismatch", "审批单不属于当前租户")

    def _execute(self, approval_id: str, decision: str, actor: str, reason: str) -> dict[str, Any]:
        try:
            if self._decision_applier is not None:
                result = self._decision_applier(approval_id, decision, actor, reason)
            elif self.task_store is not None:
                result = apply_decision(
                    self.approval_store, self.task_store, approval_id, decision, actor, reason,
                )
            else:
                result = {
                    "approval": self.approval_store.decide(approval_id, decision, actor, reason),
                    "task": None,
                    "task_note": None,
                }
        except ApprovalCallbackError:
            raise
        except ValueError as exc:
            message = str(exc)
            if "不存在" in message:
                raise ApprovalCallbackError("invalid_approval_id", message) from exc
            if "已是" in message:
                raise ApprovalCallbackError("already_decided", message) from exc
            if "驳回" in message:
                raise ApprovalCallbackError("reason_required", message) from exc
            raise ApprovalCallbackError("execution_failed", message) from exc
        if not isinstance(result, dict):
            raise ApprovalCallbackError("execution_failed", "审批决策返回结构非法", status_code=500)
        return result

    def _existing_result(self, approval: dict[str, Any]) -> dict[str, Any]:
        task = None
        task_id = approval.get("task_id")
        if self.task_store is not None and task_id:
            try:
                task = self.task_store.get_task(str(task_id))
            except Exception:                               # noqa: BLE001 - 任务读取失败不影响幂等返回
                task = None
        return {"approval": approval, "task": task, "task_note": None}

    # ---------------- 幂等回执 ----------------

    def _receipt(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        with self.db.connection() as conn:
            return conn.execute(
                """select * from approval_callback_receipts
                   where tenant_id=? and store_id=? and provider=? and idempotency_key=?""",
                (self.tenant_id, self.store_id, self.provider, idempotency_key),
            ).fetchone()

    @staticmethod
    def _assert_same_request(receipt: dict[str, Any], fingerprint: str) -> None:
        if str(receipt.get("request_fingerprint") or "") != fingerprint:
            raise ApprovalCallbackError("idempotency_conflict", "同一 idempotency key 对应了不同请求")

    def _save_receipt(
        self,
        approval_id: Any,
        decision: str,
        actor: str,
        reason: str,
        idempotency_key: Any,
        fingerprint: str,
        result: dict[str, Any],
    ) -> None:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        moment = datetime.now().isoformat(timespec="seconds")
        with self.db.connection() as conn:
            cursor = conn.execute(
                """insert into approval_callback_receipts
                   (tenant_id,store_id,provider,idempotency_key,approval_id,decision,actor_id,
                    reason,request_fingerprint,result_json,created_at)
                   values(?,?,?,?,?,?,?,?,?,?,?)
                   on conflict (tenant_id,store_id,provider,idempotency_key) do nothing""",
                (self.tenant_id, self.store_id, self.provider, str(idempotency_key),
                 str(approval_id), decision, actor, reason, fingerprint, encoded, moment),
            )
            if cursor.rowcount:
                return
            row = conn.execute(
                """select * from approval_callback_receipts
                   where tenant_id=? and store_id=? and provider=? and idempotency_key=?""",
                (self.tenant_id, self.store_id, self.provider, str(idempotency_key)),
            ).fetchone()
        if not row:
            raise ApprovalCallbackError("idempotency_error", "无法读取回调回执", status_code=500)
        self._assert_same_request(row, fingerprint)

    # ---------------- 返回结构 ----------------

    def _response(
        self,
        approval_id: Any,
        decision: str,
        actor: str,
        reason: str,
        idempotency_key: Any,
        result: dict[str, Any],
        *,
        duplicate: bool,
        timestamp: int,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider,
            "approval_id": str(approval_id),
            "decision": decision,
            "decided_by": actor,
            "reason": reason,
            "idempotency_key": str(idempotency_key),
            "duplicate": bool(duplicate),
            "result": result,
            "timestamp": timestamp,
        }


class MockApprovalCallbackAdapter(ApprovalCallbackAdapter):
    """本地 mock 提供方；无需任何外部密钥，只用本地签名即可完整测试。"""

    def __init__(self, **kwargs: Any):
        super().__init__("mock", **kwargs)


class FeishuApprovalCallbackAdapter(ApprovalCallbackAdapter):
    """飞书事件/卡片回调：官方签名、Verification Token 与 Encrypt Key 解密。"""

    def __init__(self, *, verification_token: str = "", encrypt_key: str = "",
                 **kwargs: Any):
        super().__init__("feishu", **kwargs)
        self.verification_token = str(
            verification_token or os.getenv("FEISHU_VERIFICATION_TOKEN") or self.secret
        ).strip()
        self.encrypt_key = str(
            encrypt_key or os.getenv("FEISHU_ENCRYPT_KEY") or ""
        ).strip()

    def _signature_ok(self, raw: bytes, timestamp: Any, nonce: Any, signature: Any) -> bool:
        keys = (self.encrypt_key, self.verification_token, self.secret)
        for key in keys:
            if not key:
                continue
            material = f"{timestamp}{nonce}{key}".encode("utf-8") + raw
            expected = hashlib.sha256(material).hexdigest()
            if hmac.compare_digest(expected, str(signature)):
                return True
        return False

    @staticmethod
    def _decrypt_encrypt_value(value: Any, encrypt_key: str) -> dict[str, Any]:
        if not encrypt_key:
            raise ApprovalCallbackError("missing_encrypt_key", "飞书 Encrypt Key 未配置", status_code=500)
        blob = _base64_decode(value, "encrypt")
        if len(blob) < 32 or len(blob) % 16:
            raise ApprovalCallbackError("decryption_failed", "飞书密文长度非法")
        plaintext = _decode_aes_cbc(
            blob[16:], hashlib.sha256(encrypt_key.encode("utf-8")).digest(), blob[:16],
        )
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApprovalCallbackError("decryption_failed", "飞书明文不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ApprovalCallbackError("decryption_failed", "飞书明文必须是 JSON 对象")
        return payload

    def _prepare_inbound(self, raw: bytes, headers: Any) -> dict[str, Any]:
        outer = _json_object(raw)
        encrypted = _get_ci(outer, "encrypt")
        header = _as_dict(outer.get("header"))
        token = _get_ci(outer, "token") or _get_ci(header, "token")
        if not encrypted and token is None and not self.encrypt_key:
            return super()._prepare_inbound(raw, headers)

        signature = _required(
            _header(headers, SIGNATURE_HEADERS["feishu"])
            or _get_ci(outer, "signature", "sign"),
            "signature",
        )
        timestamp = _required(
            _header(headers, TIMESTAMP_HEADERS["feishu"])
            or _get_ci(outer, "timestamp", "ts"),
            "timestamp",
        )
        nonce = _required(
            _header(headers, NONCE_HEADERS["feishu"])
            or _get_ci(outer, "nonce"),
            "nonce",
        )
        try:
            moment = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise ApprovalCallbackError("verification_failed", "timestamp 非法") from exc
        if not _within_tolerance(moment, int(self._now()), self.tolerance):
            raise ApprovalCallbackError("verification_failed", "回调时间超出容忍窗口")
        if not self._signature_ok(raw, timestamp, nonce, signature):
            raise ApprovalCallbackError("verification_failed", "飞书签名不匹配")

        payload = self._decrypt_encrypt_value(encrypted, self.encrypt_key) if encrypted else outer
        payload_header = _as_dict(payload.get("header"))
        payload_token = _get_ci(payload, "token") or _get_ci(payload_header, "token")
        if self.verification_token and str(payload_token or "") != self.verification_token:
            raise ApprovalCallbackError("verification_failed", "飞书 Verification Token 不匹配")
        if not self.guard.consume(str(nonce), self.provider):
            raise ApprovalCallbackError("replay", "nonce 已使用（疑似重放）")

        challenge = _get_ci(payload, "challenge")
        if challenge is not None:
            return {"challenge": str(challenge), "timestamp": moment, "provider": self.provider}

        payload = dict(payload)
        payload.setdefault("signature", str(signature))
        payload.setdefault("timestamp", str(timestamp))
        payload.setdefault("nonce", str(nonce))
        return {"parsed": self._extract(payload, headers), "timestamp": moment}


class WeComApprovalCallbackAdapter(ApprovalCallbackAdapter):
    """企业微信 URL/XML 回调：SHA1 签名、EncodingAESKey 解密与 receiveid 校验。"""

    def __init__(self, *, token: str = "", encoding_aes_key: str = "",
                 receive_id: str = "", corp_id: str = "", **kwargs: Any):
        super().__init__("wecom", **kwargs)
        self.token = str(token or os.getenv("WECOM_CALLBACK_TOKEN") or self.secret).strip()
        self.encoding_aes_key = str(
            encoding_aes_key or os.getenv("WECOM_ENCODING_AES_KEY") or ""
        ).strip()
        self.receive_id = str(
            receive_id or corp_id or os.getenv("WECOM_CORP_ID") or ""
        ).strip()

    def _signature_ok(self, timestamp: Any, nonce: Any, signed_value: Any, signature: Any) -> bool:
        expected = hashlib.sha1(
            "".join(sorted((self.token, str(timestamp), str(nonce), str(signed_value))))
            .encode("utf-8")
        ).hexdigest()
        return hmac.compare_digest(expected, str(signature))

    def _decrypt(self, encrypted: Any) -> tuple[str, str]:
        if not self.encoding_aes_key:
            raise ApprovalCallbackError(
                "missing_encoding_aes_key", "企微 EncodingAESKey 未配置", status_code=500)
        encoded_key = self.encoding_aes_key.rstrip("=")
        key = _base64_decode(encoded_key + "=", "EncodingAESKey")
        if len(key) != 32:
            raise ApprovalCallbackError("decryption_failed", "企微 EncodingAESKey 长度非法")
        blob = _base64_decode(encrypted, "Encrypt")
        plaintext = _decode_aes_cbc(blob, key, key[:16], block_size=256)
        if len(plaintext) < 20:
            raise ApprovalCallbackError("decryption_failed", "企微明文结构非法")
        message_length = struct.unpack(">I", plaintext[16:20])[0]
        if message_length > len(plaintext) - 20:
            raise ApprovalCallbackError("decryption_failed", "企微明文长度非法")
        try:
            message = plaintext[20:20 + message_length].decode("utf-8")
            receive_id = plaintext[20 + message_length:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApprovalCallbackError("decryption_failed", "企微明文编码非法") from exc
        if not self.receive_id or receive_id != self.receive_id:
            raise ApprovalCallbackError("receive_id_mismatch", "企微 receiveid 校验失败")
        return message, receive_id

    def _prepare_inbound(self, raw: bytes, headers: Any) -> dict[str, Any]:
        params = _as_dict(headers)
        echostr = _get_ci(params, "echostr", "echoStr")
        if echostr is None and not raw.lstrip().startswith(b"<"):
            return super()._prepare_inbound(raw, headers)

        root = _xml_root(raw) if not echostr else None
        signed_value = _xml_encrypted(root) if root is not None else echostr
        signature = _required(
            _get_ci(params, "msg_signature", "msgSignature", "signature", "sign")
            or _header(headers, SIGNATURE_HEADERS["wecom"]),
            "signature",
        )
        timestamp = _required(
            _get_ci(params, "timestamp", "TimeStamp", "ts")
            or _header(headers, TIMESTAMP_HEADERS["wecom"]),
            "timestamp",
        )
        nonce = _required(
            _get_ci(params, "nonce") or _header(headers, NONCE_HEADERS["wecom"]),
            "nonce",
        )
        try:
            moment = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise ApprovalCallbackError("verification_failed", "timestamp 非法") from exc
        if not _within_tolerance(moment, int(self._now()), self.tolerance):
            raise ApprovalCallbackError("verification_failed", "回调时间超出容忍窗口")
        if not self._signature_ok(timestamp, nonce, signed_value, signature):
            raise ApprovalCallbackError("verification_failed", "企微签名不匹配")

        plaintext, _ = self._decrypt(signed_value)
        if not self.guard.consume(str(nonce), self.provider):
            raise ApprovalCallbackError("replay", "nonce 已使用（疑似重放）")
        if echostr is not None:
            return {"challenge": plaintext, "timestamp": moment, "provider": self.provider}
        payload = _xml_object(plaintext.encode("utf-8"))
        payload.setdefault("signature", str(signature))
        payload.setdefault("timestamp", str(timestamp))
        payload.setdefault("nonce", str(nonce))
        return {"parsed": self._extract(payload, headers), "timestamp": moment}


def create_approval_callback_adapter(provider: str, **kwargs: Any) -> ApprovalCallbackAdapter:
    """工厂：按 provider 创建统一适配器，避免调用方散落分支。"""
    normalized = str(provider or "").strip().lower()
    adapters = {
        "mock": MockApprovalCallbackAdapter,
        "feishu": FeishuApprovalCallbackAdapter,
        "wecom": WeComApprovalCallbackAdapter,
    }
    adapter_type = adapters.get(normalized)
    if adapter_type is None:
        raise ApprovalCallbackError("unknown_provider", f"不支持的审批回调提供方：{provider}")
    return adapter_type(**kwargs)