#!/usr/bin/env python3
"""审批回调适配层：统一接收 mock / 飞书 / 企微回调并调用现有审批决策链路。

设计约束：

* 厂商协议解析只在 adapter 内完成，核心审批逻辑不依赖任何厂商 SDK；
* 复用 ``security.py`` 的 HMAC-SHA256、时间窗和 nonce 去重能力；
* idempotency key 在本地回执表中持久化，重复投递不会再次推进审批或任务；
* 回调只允许 approved / rejected 两种固定决策，未知动作直接拒绝。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

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
        decision = (_get_ci(value, "decision", "action", "result")
                    or _get_ci(payload, "decision", "action", "result"))
        actor = (_get_ci(value, "decided_by", "decidedBy", "operator_id", "operatorId",
                         "user_id", "userId", "open_id", "openId")
                 or _get_ci(operator, "open_id", "openId", "user_id", "userId", "id")
                 or _get_ci(payload, "decided_by", "decidedBy", "operator_id", "operatorId",
                            "user_id", "userId", "open_id", "openId"))
        reason = (_get_ci(value, "reason", "comment", "note", "message")
                  or _get_ci(payload, "reason", "comment", "note", "message") or "")
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
                                      "event_id", "eventId", "msg_id", "msgId")
                           or _get_ci(payload, "idempotency_key", "idempotencyKey",
                                      "event_id", "eventId", "msg_id", "msgId")
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

    def handle(self, body: Any, headers: Any = None) -> dict[str, Any]:
        raw = _body_bytes(body)
        parsed = self._extract(_json_object(raw), headers or {})

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
        timestamp = int(verification["timestamp"])

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
    """飞书卡片回调协议适配器；只做字段映射，不引入厂商 SDK。"""

    def __init__(self, **kwargs: Any):
        super().__init__("feishu", **kwargs)


class WeComApprovalCallbackAdapter(ApprovalCallbackAdapter):
    """企业微信审批回调协议适配器；只做字段映射，不引入厂商 SDK。"""

    def __init__(self, **kwargs: Any):
        super().__init__("wecom", **kwargs)


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