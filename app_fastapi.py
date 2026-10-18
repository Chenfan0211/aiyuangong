#!/usr/bin/env python3
"""FastAPI 传输层（二期批次 8）：与 agent_server.py 等价，但带 OpenAPI 文档。

与 stdlib 版的关系：
  * 业务逻辑完全复用 `auth.py` / `agent_tools.py` / `task_store.py` / `agent.html`；
  * 只替换传输层（路由、依赖注入、错误码）；`agent_server.py` 作为零依赖备用入口保留；
  * 认证语义与 stdlib 版一致：无令牌 401、权限不足 403、任务状态冲突 409、口令锁定 423。

启动：
    python -m uvicorn app_fastapi:create_app --factory --host 127.0.0.1 --port 8766
    # 交互式 API 文档： http://127.0.0.1:8766/docs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_tools import PermissionDenied, StoreTools
from approval_callbacks import ApprovalCallbackError, create_approval_callback_adapter
from evidence_store import EvidenceError, EvidenceNotFoundError, EvidenceStore
from outcome_tracker import OutcomeTracker, OutcomeTrackerError
from ops_store import OpsStore, OpsStoreError
from auth import AuthError, AuthStore, AccountLocked, CredentialsError, ROLES, SessionInvalid, SecurityContext
from governance import ApprovalStore, AuditCenter, ToolRunLog, apply_decision, log_tool_call
from platform_data import PlatformMetricStore
from llm_agent import OpenAIToolResponder, ToolCallingAgent
from reporting import StoreReporter
from registry import RegistryError, RegistryStore
from snapshot_builder import SnapshotError, load_config, save_config
from store_agent import AgentSessionStore, OpenAICompatibleResponder, StoreOperationsAgent
from task_store import TaskStore, TaskStoreError

ROOT = Path(__file__).resolve().parent
ACCESS_LOGGER = logging.getLogger("store_ops.access")
WRITE_STATUSES = ("accepted", "done", "verified", "rejected", "canceled", "ready")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}




def _deep_update(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
    return target




def _approval_callback_secret(provider: str) -> str:
    shared = os.getenv("STORE_AGENT_APPROVAL_CALLBACK_SECRET", "").strip()
    if provider == "mock":
        return shared or "mock-callback-secret"
    if provider == "feishu":
        return (os.getenv("FEISHU_VERIFICATION_TOKEN") or os.getenv("FEISHU_ENCRYPT_KEY")
                or os.getenv("FEISHU_APP_SECRET") or shared).strip()
    if provider == "wecom":
        return (os.getenv("WECOM_CALLBACK_TOKEN") or os.getenv("WECOM_ENCODING_AES_KEY")
                or os.getenv("WECOM_SECRET") or shared).strip()
    return ""


def validate_startup(host: str, auth_mode: str) -> None:
    """Reject combinations that would expose demo identity spoofing to the network."""
    if auth_mode == "demo" and host not in LOOPBACK_HOSTS:
        raise ValueError("demo 认证模式只能绑定回环地址（127.0.0.1 / localhost / ::1）")


def _is_production() -> bool:
    return os.getenv("STORE_AGENT_ENV", "development").strip().lower() in {"production", "prod"}



# ---------------- 请求模型（同时生成 OpenAPI 文档） ----------------

class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="口令")
    store_id: Optional[str] = Field(None, description="多门店账号需指定门店")


class ChatRequest(BaseModel):
    session_id: str = Field(..., description="会话 ID（浏览器生成，用于多轮与身份绑定）")
    message: str = Field(..., max_length=2000)


class EvidenceRequest(BaseModel):
    session_id: str
    evidence_type: str = Field(..., description="证据类型，例如：脱敏订单号 / 平台回复截图")
    object_ref: str = Field(..., description="证据引用（脱敏单号、文件路径或对象存储 key）")
    note: Optional[str] = None


class FeedbackRequest(BaseModel):
    decision: str = Field(..., description="accepted / rejected / false_positive")
    reason: str = Field("", max_length=1000)


class ConfigPatchRequest(BaseModel):
    config: dict[str, Any] = Field(..., description="需要合并到当前门店配置的字段")
    revision: Optional[str] = Field(None, description="当前配置 revision；不一致返回 409")


class MemberCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    display_name: str = Field(..., min_length=1, max_length=40)
    role: str = Field(..., description="owner / area_manager / manager / operator / employee")


class MfaCodeRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=32)


class MfaVerifyRequest(BaseModel):
    mfa_token: str = Field(..., min_length=20)
    code: str = Field(..., min_length=6, max_length=32)


class ApprovalCreate(BaseModel):
    action_type: str = Field(..., description="动作类型，例如 pricing:change")
    payload: dict[str, Any] = Field(default_factory=dict, description="动作载荷（会脱敏后留痕）")
    task_id: Optional[str] = None
    reason: str = ""


class ApprovalDecision(BaseModel):
    decision: str = Field(..., description="approved 或 rejected")
    reason: str = ""


class StatusRequest(BaseModel):
    session_id: str
    status: str = Field(..., description="accepted / done 由负责人；verified / rejected 仅管理者")
    reason: str = ""


class AppContext:
    """一次进程内共享的依赖容器。"""

    def __init__(self, config_path: Path, metrics_db: Path, store_db: Path, chat_db: Path,
                 auth_mode: str = "session", evidence_dir: Path = ROOT / "evidence"):
        self.config_path = Path(config_path)
        file_config = load_config(self.config_path)
        tenant_id = str(file_config.get("tenant_id") or "")
        self.registry = RegistryStore(store_db, tenant_id=tenant_id)
        self.config = self.registry.effective_config(file_config)
        self.config_revision = self.registry.current_revision(tenant_id, str(self.config.get("store_id") or ""))
        tenant_id = str(self.config.get("tenant_id") or "")
        self.metric_store = PlatformMetricStore(metrics_db, tenant_id=tenant_id)
        self.task_store = TaskStore(store_db, tenant_id=tenant_id)
        self.evidence = EvidenceStore(store_db, root_dir=evidence_dir, tenant_id=tenant_id)
        self.outcomes = OutcomeTracker(store_db, tenant_id=tenant_id)
        self.auth = AuthStore(store_db, tenant_id=tenant_id)
        self.tools = StoreTools(self.config, self.metric_store, self.task_store, config_path=self.config_path)
        self.ops = OpsStore(store_db, tenant_id=tenant_id, store_id=self.store_id)
        self.sessions = AgentSessionStore(chat_db, tenant_id=tenant_id)
        self.approvals = ApprovalStore(store_db, tenant_id=tenant_id)
        self.tool_log = ToolRunLog(store_db, tenant_id=tenant_id)
        self.audit_center = AuditCenter(store_db, tenant_id=tenant_id)
        self.reporter = StoreReporter(self.config, self.metric_store, self.task_store,
                                      self.approvals, config_path=self.config_path)
        llm_responder = OpenAICompatibleResponder.from_environment()
        tool_responder = OpenAIToolResponder.from_environment()
        tool_agent = None
        if tool_responder is not None:
            tool_agent = ToolCallingAgent(self.tools, tool_responder, tool_log=self.tool_log,
                                          audit=self.auth, tenant_id=tenant_id)
        self.agent = StoreOperationsAgent.from_tools(self.config, self.tools, llm_responder, tool_agent)
        self.auth_mode = auth_mode

    @property
    def store_id(self) -> str:
        return self.tools.store_id


def create_app(
    config_path: Path = ROOT / "store_config.json",
    metrics_db: Path = ROOT / "data" / "platform_metrics.db",
    store_db: Path = ROOT / "data" / "store_ops.db",
    chat_db: Path = ROOT / "data" / "agent_demo.db",
    auth_mode: str = "session",
    evidence_dir: Path = ROOT / "evidence",
) -> FastAPI:
    ctx = AppContext(config_path, metrics_db, store_db, chat_db, auth_mode, evidence_dir)
    production = _is_production()
    app = FastAPI(
        title="门店运营 Agent API",
        version="0.2.0",
        description="武汉狮子座店单店试点：平台经营数据、任务闭环、数据质量与店长日报。"
                    "除 /api/auth/login 外，所有数据接口都需要 Bearer 令牌。",
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.state.ctx = ctx

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        unsafe = request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
        path = request.url.path
        csrf_exempt = (path in {"/api/auth/login", "/api/auth/mfa/verify"}
                       or path.startswith("/api/webhooks/"))
        bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
        if (unsafe and path.startswith("/api/") and not csrf_exempt and not bearer
                and request.cookies.get("store_ops_session")):
            expected = request.cookies.get("store_ops_csrf", "")
            actual = request.headers.get("x-csrf-token", "")
            if not expected or not secrets.compare_digest(expected, actual):
                return JSONResponse(status_code=403, content={"detail": "CSRF 校验失败"})
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        csp = ("default-src 'self'; script-src 'self'; style-src 'self'; "
               "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
               "base-uri 'none'; frame-ancestors 'none'")
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Request-ID"] = request_id
        if _is_production():
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        ACCESS_LOGGER.info(json.dumps({
            "event": "http_request", "request_id": request_id, "method": request.method,
            "path": request.url.path, "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }, ensure_ascii=False))
        return response

    @app.get("/livez", include_in_schema=False)
    def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        try:
            ctx.task_store.list_tasks(ctx.store_id, limit=1)
            return {"status": "ready", "database": "ok"}
        except Exception as exc:
            return JSONResponse(status_code=503, content={"status": "not_ready", "error": str(exc)})

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> PlainTextResponse:
        return PlainTextResponse("mall_ops_up 1\nmall_ops_store_info{store_id=\"" + ctx.store_id + "\"} 1\n")

    # ---------------- 依赖 ----------------

    def _demo_context(user_id: str) -> SecurityContext:
        from auth import permissions_for
        user = ctx.agent.user(user_id or "manager-001")
        return SecurityContext(
            user_id=user["user_id"], tenant_id=str(ctx.config.get("tenant_id") or ""),
            store_id=ctx.store_id, role=user["role"], display_name=user["name"],
            session_id="demo-mode", permissions=permissions_for(user["role"]),
        )

    def current_context(
        request: Request,
        authorization: str = Header(default=""),
        demo_user_id: Optional[str] = Query(default=None, alias="user_id", description="仅 demo 模式使用"),
    ) -> SecurityContext:
        if ctx.auth_mode == "demo":
            try:
                return _demo_context(demo_user_id or "manager-001")
            except ValueError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not token:
            token = request.cookies.get("store_ops_session", "")
        if not token:
            raise HTTPException(status_code=401, detail="缺少认证令牌，请先登录")
        try:
            return ctx.auth.resolve(token)
        except (SessionInvalid, AuthError) as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def _log(tool_name: str, context: SecurityContext, session_id: Optional[str] = None,
             input_payload: Any = None, output_payload: Any = None) -> None:
        log_tool_call(ctx.tool_log, context.store_id, tool_name, actor_id=context.user_id,
                      session_id=session_id, input_payload=input_payload, output_payload=output_payload)

    def require(permission: Optional[str] = None):
        def dependency(request: Request, context: SecurityContext = Depends(current_context)) -> SecurityContext:
            allowed_mfa = request.url.path.startswith("/api/auth/mfa/") or request.url.path in {
                "/api/auth/me", "/api/auth/logout",
            }
            if context.mfa_enrollment_required and not allowed_mfa:
                raise HTTPException(status_code=403, detail="必须先完成 MFA 绑定")
            if permission and not context.has(permission):
                ctx.auth.record_audit(
                    event_type="permission_denied", actor_type="user", actor_id=context.user_id,
                    tenant_id=context.tenant_id, store_id=context.store_id,
                    object_type="endpoint", object_id=f"{request.method} {request.url.path}",
                    after={"required_permission": permission, "role": context.role},
                )
                raise HTTPException(status_code=403,
                                    detail=f"当前身份（{context.role_label}）缺少权限：{permission}")
            return context
        return dependency

    def _require_visible(context: SecurityContext, task: dict[str, Any]) -> None:
        if task["store_id"] != context.store_id:
            raise HTTPException(status_code=403, detail=f"任务 {task['task_id']} 不属于当前门店")

    # ---------------- 认证 ----------------

    def _session_payload(result: dict[str, Any]) -> dict[str, Any]:
        context = result["context"]
        return {
            "token": result["token"], "expires_at": result["expires_at"], "mfa_required": False,
            "user": context.to_user_dict(), "tenant_id": context.tenant_id, "store_id": context.store_id,
            "permissions": sorted(context.permissions),
            "mfa_enrollment_required": context.mfa_enrollment_required,
        }

    def _set_session_cookie(response: Response, token: str, expires_at: str) -> None:
        max_age = max(60, int((datetime.fromisoformat(expires_at) - datetime.now()).total_seconds()))
        csrf = secrets.token_urlsafe(24)
        response.set_cookie("store_ops_session", token, max_age=max_age, httponly=True,
                            secure=_is_production(), samesite="strict", path="/")
        response.set_cookie("store_ops_csrf", csrf, max_age=max_age, httponly=False,
                            secure=_is_production(), samesite="strict", path="/")

    @app.post("/api/auth/login", tags=["认证"], summary="用户名口令登录")
    def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
        try:
            result = ctx.auth.login(payload.username, payload.password, payload.store_id)
        except AccountLocked as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if result.get("mfa_required"):
            return result
        context = result["context"]
        if context.store_id != ctx.store_id:
            raise HTTPException(status_code=401,
                                detail=f"该账号登录的门店是 {context.store_id}，本服务只服务 {ctx.store_id}")
        _set_session_cookie(response, result["token"], result["expires_at"])
        return _session_payload(result)

    @app.post("/api/auth/mfa/verify", tags=["认证"], summary="完成 MFA 两步登录")
    def mfa_verify(payload: MfaVerifyRequest, response: Response) -> dict[str, Any]:
        try:
            result = ctx.auth.complete_mfa(payload.mfa_token, payload.code)
        except (CredentialsError, SessionInvalid, AuthError) as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        _set_session_cookie(response, result["token"], result["expires_at"])
        return _session_payload(result)

    @app.post("/api/auth/mfa/setup", tags=["认证"], summary="开始绑定 TOTP")
    def mfa_setup(context: SecurityContext = Depends(require())) -> dict[str, str]:
        return ctx.auth.begin_mfa_enrollment(context.user_id)

    @app.post("/api/auth/mfa/enable", tags=["认证"], summary="确认并启用 TOTP")
    def mfa_enable(payload: MfaCodeRequest,
                   context: SecurityContext = Depends(require())) -> dict[str, Any]:
        try:
            codes = ctx.auth.confirm_mfa_enrollment(context.user_id, payload.code)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.auth.record_audit(event_type="mfa_enabled", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="user", object_id=context.user_id)
        return {"enabled": True, "recovery_codes": codes}

    @app.post("/api/auth/logout", tags=["认证"], summary="撤销当前令牌")
    def logout(request: Request, response: Response, authorization: str = Header(default=""),
               context: SecurityContext = Depends(require())) -> dict[str, Any]:
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not token:
            token = request.cookies.get("store_ops_session", "")
        if ctx.auth_mode == "demo":
            return {"revoked": False, "auth_mode": "demo"}
        revoked = ctx.auth.logout(token)
        response.set_cookie("store_ops_session", "", max_age=0, expires=0, httponly=True,
                            secure=_is_production(), samesite="strict", path="/")
        response.set_cookie("store_ops_csrf", "", max_age=0, expires=0, httponly=False,
                            secure=_is_production(), samesite="strict", path="/")
        ctx.auth.record_audit(event_type="logout", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="session", object_id=context.session_id)
        return {"revoked": revoked}

    @app.get("/api/auth/me", tags=["认证"], summary="当前身份与权限")
    def me(context: SecurityContext = Depends(require())) -> dict[str, Any]:
        return {
            "user": context.to_user_dict(), "tenant_id": context.tenant_id, "store_id": context.store_id,
            "session_id": context.session_id, "expires_at": context.expires_at,
            "permissions": sorted(context.permissions),
        }

    @app.post("/api/webhooks/approvals/{provider}", tags=["治理"], summary="审批回调（签名、nonce、幂等）")
    async def approval_webhook(provider: str, request: Request) -> dict[str, Any]:
        normalized = str(provider or "").lower()
        if normalized not in {"mock", "feishu", "wecom"}:
            raise HTTPException(status_code=404, detail="不支持的审批回调提供方")
        secret = _approval_callback_secret(normalized)
        if not secret:
            raise HTTPException(status_code=503, detail="审批回调密钥尚未配置")
        body = await request.body()
        adapter = create_approval_callback_adapter(
            normalized, secret=secret, db_target=ctx.registry.db.target,
            approval_store=ctx.approvals, task_store=ctx.task_store,
            tenant_id=str(ctx.config.get("tenant_id") or ""), store_id=ctx.store_id,
            decision_applier=lambda approval_id, decision, actor, reason: apply_decision(
                ctx.approvals, ctx.task_store, approval_id, decision, actor, reason),
        )
        try:
            result = adapter.handle(body, dict(request.headers))
        except ApprovalCallbackError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
        ctx.auth.record_audit(event_type="approval_callback", actor_type="external", actor_id=normalized,
                              tenant_id=str(ctx.config.get("tenant_id") or ""), store_id=ctx.store_id,
                              object_type="approval", object_id=result.get("approval_id", ""),
                              after={"decision": result.get("decision"), "duplicate": result.get("duplicate")})
        return {"result": result}

    # ---------------- 管理后台 ----------------

    @app.get("/api/admin/config", tags=["管理"], summary="读取门店配置")
    def admin_config_get(context: SecurityContext = Depends(require("config:write"))) -> dict[str, Any]:
        return {"config": ctx.config, "revision": ctx.config_revision,
                "store_id": ctx.store_id, "tenant_id": ctx.config.get("tenant_id")}

    @app.patch("/api/admin/config", tags=["管理"], summary="修改门店配置并写审计")
    def admin_config_patch(payload: ConfigPatchRequest,
                           context: SecurityContext = Depends(require("config:write"))) -> dict[str, Any]:
        before = json.loads(json.dumps(ctx.config, ensure_ascii=False))
        merged = _deep_update(json.loads(json.dumps(ctx.config, ensure_ascii=False)), payload.config)
        try:
            ctx.registry.update_config(merged, expected_revision=payload.revision)
        except RegistryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _deep_update(ctx.config, payload.config)
        ctx.config_revision = ctx.registry.current_revision(context.tenant_id, ctx.store_id)
        ctx.auth.record_audit(event_type="store_config_updated", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="store_config", object_id=ctx.store_id,
                              before=before, after=merged)
        return {"config": ctx.config, "revision": ctx.config_revision, "updated": True}

    @app.get("/api/admin/members", tags=["管理"], summary="列出门店成员")
    def admin_members(context: SecurityContext = Depends(require("store:manage"))) -> dict[str, Any]:
        return {"members": ctx.auth.list_users(context.tenant_id, context.store_id)}

    @app.post("/api/admin/members", tags=["管理"], summary="创建门店成员并返回一次性临时口令")
    def admin_member_create(payload: MemberCreateRequest,
                            context: SecurityContext = Depends(require("store:manage"))) -> dict[str, Any]:
        if payload.role not in ROLES:
            raise HTTPException(status_code=400, detail="未知角色")
        temporary = secrets.token_urlsafe(12)
        try:
            member = ctx.auth.create_user(payload.username, temporary, payload.display_name, payload.role,
                                          context.tenant_id, context.store_id)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.auth.record_audit(event_type="member_created", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="user", object_id=member["user_id"], after={"role": payload.role})
        return {"member": member, "temporary_password": temporary}

    @app.post("/api/admin/members/{user_id}/reset-password", tags=["管理"], summary="重置成员口令")
    def admin_member_reset_password(user_id: str,
                                    context: SecurityContext = Depends(require("store:manage"))) -> dict[str, Any]:
        temporary = secrets.token_urlsafe(12)
        try:
            ctx.auth.set_password(user_id, temporary, actor_id=context.user_id)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.auth.record_audit(event_type="member_password_reset", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="user", object_id=user_id)
        return {"temporary_password": temporary, "sessions_revoked": True}

    @app.post("/api/admin/members/{user_id}/reset-mfa", tags=["管理"], summary="重置成员 MFA")
    def admin_member_reset_mfa(user_id: str,
                               context: SecurityContext = Depends(require("store:manage"))) -> dict[str, Any]:
        ctx.auth.reset_mfa(user_id)
        ctx.auth.record_audit(event_type="member_mfa_reset", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="user", object_id=user_id)
        return {"mfa_enabled": ctx.auth.is_mfa_enabled(user_id)}

    @app.get("/api/admin/ops-runs", tags=["管理"], summary="列出运维任务运行记录")
    def admin_ops_runs(limit: int = Query(100, ge=1, le=1000),
                       job_name: Optional[str] = Query(None),
                       context: SecurityContext = Depends(require("audit:read"))) -> dict[str, Any]:
        return {"runs": ctx.ops.list_runs(job_name=job_name, limit=limit, ascending=False)}

    @app.get("/api/admin/ops-runs/{run_id}", tags=["管理"], summary="查看运维任务运行详情")
    def admin_ops_run(run_id: int,
                      context: SecurityContext = Depends(require("audit:read"))) -> dict[str, Any]:
        record = ctx.ops.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        return {"run": record}

    @app.post("/api/admin/ops-runs/{run_id}/retry", tags=["管理"], summary="请求重跑运维任务")
    def admin_ops_run_retry(run_id: int,
                            context: SecurityContext = Depends(require("config:write"))) -> dict[str, Any]:
        source = ctx.ops.get_run(run_id)
        if source is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        if source["status"] not in {"failed", "skipped"}:
            raise HTTPException(status_code=409, detail="只有失败或跳过的任务可以重试")
        queued = ctx.ops.record_run("daily_sync", "retry-request", status="skipped",
                                    details={"source_run_id": run_id, "requested_by": context.user_id},
                                    alert_status="not_needed")
        ctx.auth.record_audit(event_type="ops_retry_requested", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="ops_run", object_id=str(run_id))
        return {"accepted": True, "queued_run": queued}

    # ---------------- 门店与数据 ----------------

    @app.get("/api/bootstrap", tags=["门店"], summary="门店、可见任务数与数据状态")
    def bootstrap(context: SecurityContext = Depends(require())) -> dict[str, Any]:
        user = context.to_user_dict()
        tasks = ctx.tools.tasks_for(user, limit=50)
        return {
            "user": user, "store": ctx.agent.snapshot["store"], "task_count": len(tasks),
            "mode": "tools", "auth_mode": ctx.auth_mode,
            "llm_enabled": ctx.agent.llm_responder is not None,
            "llm_mode": ("tool_calling" if ctx.agent.tool_agent is not None
                         else ("plain" if ctx.agent.llm_responder is not None else "off")),
            "data_status": ctx.tools.data_status() if context.has("metrics:read") else None,
            "permissions": sorted(context.permissions),
        }

    @app.get("/api/data-status", tags=["数据"], summary="平台数据新鲜度")
    def data_status(context: SecurityContext = Depends(require("metrics:read"))) -> dict[str, Any]:
        return ctx.tools.data_status()

    @app.get("/api/metrics/summary", tags=["数据"], summary="统一口径指标摘要（财务字段按权限裁剪）")
    def metrics_summary(days: int = Query(7, ge=1, le=90),
                        context: SecurityContext = Depends(require("metrics:read"))) -> dict[str, Any]:
        summary = ctx.tools.metric_summary(days=days, include_financial=context.has("metrics:read:financial"))
        _log("api:metrics_summary", context, input_payload={"days": days},
             output_payload={"has_data": summary.get("has_data")})
        return summary

    @app.get("/api/data-quality", tags=["数据"], summary="数据质量体检：缺日/跳变/批次/新鲜度")
    def data_quality(days: int = Query(30, ge=1, le=365),
                     context: SecurityContext = Depends(require("quality:read"))) -> dict[str, Any]:
        quality = ctx.tools.data_quality(days=days)
        _log("api:data_quality", context, input_payload={"days": days},
             output_payload={"missing_day_count": quality["summary"]["missing_day_count"],
                             "anomaly_count": quality["summary"]["anomaly_count"]})
        return quality

    @app.get("/api/reports/daily", tags=["数据"], summary="店长日报")
    def daily_report(date: str = Query("today", description="today / yesterday / YYYY-MM-DD"),
                     context: SecurityContext = Depends(require("report:read"))) -> dict[str, Any]:
        try:
            report = ctx.tools.daily_report(date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _log("api:daily_report", context, input_payload={"date": report["date"]},
             output_payload={"open_task_count": report["summary"]["open_task_count"]})
        ctx.auth.record_audit(event_type="report_generated", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="daily_report", object_id=report["date"])
        return report

    # ---------------- 任务 ----------------

    @app.get("/api/tasks/today", tags=["任务"], summary="今日任务（店长看全店，店员只看本人）")
    def tasks_today(status: Optional[str] = Query(None),
                    context: SecurityContext = Depends(require("tasks:read:own"))) -> dict[str, Any]:
        user = context.to_user_dict()
        try:
            tasks = ctx.tools.tasks_for(user, status=status, limit=50)
        except (TaskStoreError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _log("api:tasks_today", context, input_payload={"status": status}, output_payload={"count": len(tasks)})
        return {"user": user, "store": ctx.agent.snapshot["store"], "status_filter": status or "all",
                "count_by_status": ctx.task_store.count_by_status(context.store_id), "tasks": tasks}

    @app.get("/api/tasks/{task_id}", tags=["任务"], summary="任务详情（含事件与证据）")
    def task_detail(task_id: str,
                    context: SecurityContext = Depends(require("tasks:read:own"))) -> dict[str, Any]:
        try:
            return ctx.tools.task_detail(task_id, context.to_user_dict())
        except PermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TaskStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/tasks/{task_id}/evidence", tags=["任务"], summary="提交证据")
    def submit_evidence(task_id: str, payload: EvidenceRequest,
                        context: SecurityContext = Depends(require("tasks:read:own"))) -> dict[str, Any]:
        user = context.to_user_dict()
        try:
            task = ctx.task_store.get_task(task_id)
        except TaskStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _require_visible(context, task)
        if not ctx.tools.can_submit_evidence(user, task):
            raise HTTPException(status_code=403, detail="只有任务负责人或管理者可以提交证据")
        ctx.task_store.bind_session(payload.session_id, context.user_id, context.role)
        ctx.task_store.add_evidence(task_id, payload.evidence_type.strip(), payload.object_ref.strip(),
                                    context.display_name, payload.note)
        rows = ctx.task_store.evidence(task_id)
        ctx.auth.record_audit(event_type="task_evidence_submitted", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="action_task", object_id=task_id,
                              after={"evidence_type": payload.evidence_type, "object_ref": payload.object_ref})
        return {"evidence": rows[-1], "task": ctx.task_store.get_task(task_id)}

    @app.post("/api/tasks/{task_id}/evidence-upload", tags=["任务"], summary="上传图片证据并绑定任务")
    async def upload_evidence(task_id: str, request: Request,
                              filename: str = Query(..., min_length=1, max_length=255),
                              note: str = Query("", max_length=500),
                              session_id: str = Query("", max_length=80),
                              context: SecurityContext = Depends(require("tasks:read:own"))) -> dict[str, Any]:
        user = context.to_user_dict()
        try:
            task = ctx.task_store.get_task(task_id)
        except TaskStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _require_visible(context, task)
        if not ctx.tools.can_submit_evidence(user, task):
            raise HTTPException(status_code=403, detail="只有任务负责人或管理者可以上传证据")
        if session_id:
            ctx.task_store.bind_session(session_id, context.user_id, context.role)
        data = await request.body()
        try:
            record = ctx.evidence.save(task_id, filename, data, context.display_name,
                                       store_id=context.store_id, tenant_id=context.tenant_id,
                                       mime_type=request.headers.get("content-type"))
        except EvidenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.task_store.add_evidence(task_id, "图片", record["evidence_id"], context.display_name, note)
        rows = ctx.task_store.evidence(task_id)
        ctx.auth.record_audit(event_type="task_image_evidence_submitted", actor_type="user",
                              actor_id=context.user_id, tenant_id=context.tenant_id,
                              store_id=context.store_id, object_type="action_task", object_id=task_id,
                              after={"evidence_id": record["evidence_id"], "mime_type": record["mime_type"]})
        return {"evidence_file": record, "evidence": rows[-1], "task": ctx.task_store.get_task(task_id)}

    @app.get("/api/evidence/{evidence_id}", tags=["任务"], summary="读取图片证据")
    def download_evidence(evidence_id: str, task_id: str = Query(..., min_length=1),
                          context: SecurityContext = Depends(require("tasks:read:own"))):
        try:
            task = ctx.task_store.get_task(task_id)
        except TaskStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _require_visible(context, task)
        if not ctx.tools.can_submit_evidence(context.to_user_dict(), task):
            raise HTTPException(status_code=403, detail="无权读取该任务证据")
        try:
            data = ctx.evidence.read(evidence_id, store_id=context.store_id, task_id=task_id,
                                     tenant_id=context.tenant_id)
        except EvidenceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except EvidenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        meta = next((item for item in ctx.evidence.list_evidence(store_id=context.store_id, task_id=task_id,
                                                                 tenant_id=context.tenant_id)
                     if item["evidence_id"] == evidence_id), {})
        return Response(content=data, media_type=meta.get("mime_type", "application/octet-stream"),
                        headers={"Content-Disposition": f'inline; filename="{evidence_id}"'})

    @app.post("/api/tasks/{task_id}/feedback", tags=["任务"], summary="记录任务采纳/驳回/误报反馈")
    def task_feedback(task_id: str, payload: FeedbackRequest,
                      context: SecurityContext = Depends(require("tasks:read:own"))) -> dict[str, Any]:
        try:
            task = ctx.task_store.get_task(task_id)
        except TaskStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _require_visible(context, task)
        try:
            feedback = ctx.outcomes.record_feedback(context.store_id, task_id, context.user_id,
                                                    payload.decision, payload.reason)
        except OutcomeTrackerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.auth.record_audit(event_type="task_feedback_recorded", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="action_task", object_id=task_id,
                              after={"decision": payload.decision, "reason": payload.reason})
        return {"feedback": feedback}

    @app.get("/api/tasks/{task_id}/outcomes", tags=["任务"], summary="任务 D+7/D+28 结果归因")
    def task_outcomes(task_id: str,
                      context: SecurityContext = Depends(require("tasks:read:own"))) -> dict[str, Any]:
        try:
            task = ctx.task_store.get_task(task_id)
        except TaskStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _require_visible(context, task)
        return {"outcomes": ctx.outcomes.list_outcomes(context.store_id, task_id=task_id)}

    @app.get("/api/outcomes/summary", tags=["看板"], summary="试点采纳率、误报率和结果缺失汇总")
    def outcomes_summary(context: SecurityContext = Depends(require("report:read"))) -> dict[str, Any]:
        return {"summary": ctx.outcomes.outcome_summary(context.store_id)}

    @app.post("/api/tasks/{task_id}/status", tags=["任务"], summary="推进任务状态")
    def change_status(task_id: str, payload: StatusRequest,
                      context: SecurityContext = Depends(require("tasks:read:own"))) -> dict[str, Any]:
        if payload.status not in WRITE_STATUSES:
            raise HTTPException(status_code=400, detail=f"不支持的流转目标：{payload.status}")
        user = context.to_user_dict()
        try:
            task = ctx.task_store.get_task(task_id)
        except TaskStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _require_visible(context, task)
        if not ctx.tools.can_change_status(user, task, payload.status):
            raise HTTPException(status_code=403, detail=f"当前身份无权把任务改为 {payload.status}")
        ctx.task_store.bind_session(payload.session_id, context.user_id, context.role)
        before = task["status"]
        try:
            updated = ctx.task_store.change_status(task_id, payload.status, context.display_name, payload.reason)
        except TaskStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ctx.auth.record_audit(event_type="task_status_changed", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="action_task", object_id=task_id,
                              before={"status": before},
                              after={"status": payload.status, "reason": payload.reason})
        return updated

    # ---------------- 看板与报表 ----------------

    @app.get("/api/dashboard", tags=["看板"], summary="经营看板：KPI/环比/趋势/套餐/任务/数据质量")
    def dashboard(days: int = Query(7, ge=1, le=90),
                  context: SecurityContext = Depends(require("metrics:read"))) -> dict[str, Any]:
        include_financial = context.has("metrics:read:financial")
        data = ctx.reporter.dashboard(days=days, include_financial=include_financial)
        _log("api:dashboard", context, input_payload={"days": days, "financial": include_financial},
             output_payload={"has_data": data["has_data"], "trend_points": len(data["trend"])})
        return data

    def _build_report(kind: str, anchor_date: str, context: SecurityContext) -> dict[str, Any]:
        try:
            builder = ctx.reporter.weekly_report if kind == "weekly" else ctx.reporter.monthly_report
            report = builder(anchor_date, include_financial=context.has("metrics:read:financial"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _log(f"api:report_{kind}", context, input_payload={"date": anchor_date},
             output_payload={"period": report["period"], "has_data": report["summary"]["has_data"]})
        ctx.auth.record_audit(event_type="report_generated", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type=f"{kind}_report", object_id=report["period"]["end"])
        return report

    @app.get("/api/reports/weekly", tags=["看板"], summary="周报")
    def weekly_report(date: str = Query("today"),
                      context: SecurityContext = Depends(require("report:read"))) -> dict[str, Any]:
        return _build_report("weekly", date, context)

    @app.get("/api/reports/monthly", tags=["看板"], summary="月报")
    def monthly_report(date: str = Query("today"),
                       context: SecurityContext = Depends(require("report:read"))) -> dict[str, Any]:
        return _build_report("monthly", date, context)

    # ---------------- 治理：审批与审计 ----------------

    @app.get("/api/approvals", tags=["治理"], summary="审批队列（需 approvals:decide）")
    def approvals(status: Optional[str] = Query(None, description="pending / approved / rejected"),
                  context: SecurityContext = Depends(require("approvals:decide"))) -> dict[str, Any]:
        try:
            items = ctx.approvals.list(context.store_id, status=status, limit=100)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _log("api:approvals", context, input_payload={"status": status},
             output_payload={"count": len(items)})
        return {"store_id": context.store_id, "status_filter": status or "all",
                "count_by_status": ctx.approvals.count_by_status(context.store_id), "approvals": items}

    @app.post("/api/approvals", tags=["治理"], summary="提交审批申请（需 approvals:request）")
    def approval_create(payload: ApprovalCreate,
                        context: SecurityContext = Depends(require("approvals:request"))) -> dict[str, Any]:
        approval = ctx.approvals.request(
            store_id=context.store_id, action_type=payload.action_type, payload=payload.payload,
            requested_by=context.display_name, task_id=payload.task_id, reason=payload.reason,
        )
        ctx.auth.record_audit(event_type="approval_requested", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="approval", object_id=approval["approval_id"],
                              after={"action_type": payload.action_type, "task_id": approval.get("task_id")})
        return approval

    @app.post("/api/approvals/{approval_id}/decide", tags=["治理"], summary="审批决策（需 approvals:decide）")
    def approval_decide(approval_id: str, payload: ApprovalDecision,
                        context: SecurityContext = Depends(require("approvals:decide"))) -> dict[str, Any]:
        try:
            result = apply_decision(ctx.approvals, ctx.task_store, approval_id, payload.decision,
                                    context.display_name, payload.reason)
        except TaskStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.auth.record_audit(event_type="approval_decided", actor_type="user", actor_id=context.user_id,
                              tenant_id=context.tenant_id, store_id=context.store_id,
                              object_type="approval", object_id=approval_id, before={"status": "pending"},
                              after={"status": result["approval"]["status"], "reason": payload.reason,
                                     "task_status": (result["task"] or {}).get("status")})
        return result

    @app.get("/api/audit", tags=["治理"], summary="审计流水（需 audit:read）")
    def audit(limit: int = Query(100, ge=1, le=500), days: int = Query(7, ge=1, le=365),
              event_type: Optional[str] = None, actor_id: Optional[str] = None,
              context: SecurityContext = Depends(require("audit:read"))) -> dict[str, Any]:
        events = ctx.audit_center.list_events(context.store_id, limit=limit,
                                              event_type=event_type, actor_id=actor_id)
        _log("api:audit", context, input_payload={"event_type": event_type},
             output_payload={"count": len(events)})
        return {"store_id": context.store_id, "events": events,
                "event_types": ctx.audit_center.event_types(context.store_id),
                "summary": ctx.audit_center.summary(context.store_id, days=days)}

    @app.get("/api/tool-runs", tags=["治理"], summary="工具调用留痕（需 audit:read）")
    def tool_runs(limit: int = Query(50, ge=1, le=500), tool_name: Optional[str] = None,
                  context: SecurityContext = Depends(require("audit:read"))) -> dict[str, Any]:
        runs = ctx.tool_log.list(context.store_id, limit=limit, tool_name=tool_name)
        return {"store_id": context.store_id,
                "count_by_tool": ctx.tool_log.count_by_tool(context.store_id), "runs": runs}

    # ---------------- 对话 ----------------

    @app.post("/api/chat", tags=["对话"], summary="向 Agent 提问")
    def chat(payload: ChatRequest,
             context: SecurityContext = Depends(require("agent:chat"))) -> dict[str, Any]:
        try:
            ctx.task_store.bind_session(payload.session_id, context.user_id, context.role)
        except TaskStoreError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        history = ctx.sessions.history(payload.session_id, context.user_id, limit=10)
        result = ctx.agent.reply(payload.message, context.user_id, history=history,
                                 user=context.to_user_dict())
        ctx.sessions.append(payload.session_id, context.user_id, "user", payload.message)
        ctx.sessions.append(payload.session_id, context.user_id, "assistant", result["answer"], result["intent"])
        _log(f"agent:{result['intent']}", context, session_id=payload.session_id,
             input_payload={"message": payload.message, "length": len(payload.message)},
             output_payload={"requires_approval": result["requires_approval"]})
        return result

    @app.get("/api/history", tags=["对话"], summary="会话历史")
    def history(session_id: str = Query(..., min_length=1, max_length=80),
                context: SecurityContext = Depends(require())) -> dict[str, Any]:
        return {"messages": ctx.sessions.history(session_id, context.user_id)}

    # ---------------- 页面 ----------------

    @app.get("/", include_in_schema=False)
    @app.get("/agent.html", include_in_schema=False)
    def page() -> HTMLResponse:
        return HTMLResponse((ROOT / "agent.html").read_text(encoding="utf-8"))

    @app.get("/dashboard", include_in_schema=False)
    @app.get("/dashboard.html", include_in_schema=False)
    def dashboard_page() -> HTMLResponse:
        return HTMLResponse((ROOT / "dashboard.html").read_text(encoding="utf-8"))

    @app.get("/settings", include_in_schema=False)
    @app.get("/settings.html", include_in_schema=False)
    def settings_page() -> HTMLResponse:
        return HTMLResponse((ROOT / "settings.html").read_text(encoding="utf-8"))

    @app.get("/governance", include_in_schema=False)
    @app.get("/governance.html", include_in_schema=False)
    def governance_page() -> HTMLResponse:
        return HTMLResponse((ROOT / "governance.html").read_text(encoding="utf-8"))

    @app.get("/index.html", include_in_schema=False)
    def plan_page() -> HTMLResponse:
        return HTMLResponse((ROOT / "index.html").read_text(encoding="utf-8"))

    @app.exception_handler(TaskStoreError)
    def task_store_error(request: Request, exc: TaskStoreError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": str(exc)})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="启动门店运营 Agent（FastAPI）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--config", type=Path, default=Path(os.getenv("STORE_AGENT_CONFIG", ROOT / "store_config.json")))
    parser.add_argument("--metrics-db", default=os.getenv("STORE_AGENT_METRICS_DB", str(ROOT / "data" / "platform_metrics.db")),
                        help="平台指标库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--store-db", default=os.getenv("STORE_AGENT_STORE_DB", str(ROOT / "data" / "store_ops.db")),
                        help="任务/证据/身份库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--chat-db", default=os.getenv("STORE_AGENT_CHAT_DB", str(ROOT / "data" / "agent_demo.db")),
                        help="对话消息库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--auth-mode", choices=("session", "demo"), default="session")
    args = parser.parse_args()
    validate_startup(args.host, args.auth_mode)
    logging.basicConfig(level=os.getenv("STORE_AGENT_LOG_LEVEL", "INFO"),
                        format="%(message)s")

    import uvicorn

    if args.auth_mode == "demo":
        print("!! 警告：demo 认证模式允许 ?user_id= 伪身份，仅限本机演示。")
    print(f"门店运营 Agent（FastAPI）启动：http://{args.host}:{args.port}/ · 文档 /docs · auth={args.auth_mode}")
    uvicorn.run(
        create_app(args.config, args.metrics_db, args.store_db, args.chat_db, args.auth_mode),
        host=args.host, port=args.port,
    )


if __name__ == "__main__":
    main()
