#!/usr/bin/env python3
"""门店运营 Agent 本地服务（带真实登录与 RBAC）。

认证模式：
  - `--auth-mode session`（默认）：所有接口都要 `Authorization: Bearer <token>`；
    令牌由 `POST /api/auth/login` 签发，服务端只存哈希，可过期/撤销。
  - `--auth-mode demo`：仅本机演示用的降级模式，允许继续用 `?user_id=` 伪身份，
    启动时会打印警告 —— 只能跑在 127.0.0.1，不得对外。

作用域：所有数据访问都用**会话里的 tenant_id/store_id**，前端传什么都无法越权跨店。

接口一览：
  POST /api/auth/login                用户名口令登录，返回令牌
  POST /api/auth/logout               撤销当前令牌
  GET  /api/auth/me                   当前身份、角色与权限清单
  GET  /api/bootstrap                 门店、可见任务数、平台数据状态
  GET  /api/data-status               平台数据新鲜度
  GET  /api/metrics/summary           统一口径指标摘要（metrics:read）
  GET  /api/data-quality              数据质量体检（quality:read）
  GET  /api/reports/daily             店长日报（report:read）
  GET  /api/tasks/today               今日任务（tasks:read:store / tasks:read:own）
  GET  /api/tasks/{task_id}           任务详情（越权 403）
  POST /api/tasks/{task_id}/evidence  提交证据（tasks:evidence:submit）
  POST /api/tasks/{task_id}/status    推进状态（tasks:accept / tasks:verify）
  GET  /api/history                   会话历史
  POST /api/chat                      对话（agent:chat）
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from agent_tools import PermissionDenied, StoreTools
from auth import AuthError, AuthStore, AccountLocked, SessionInvalid, SecurityContext, permissions_for
from governance import ApprovalStore, AuditCenter, ToolRunLog, apply_decision, log_tool_call
from platform_data import PlatformMetricStore
from llm_agent import OpenAIToolResponder, ToolCallingAgent
from reporting import StoreReporter
from registry import RegistryStore
from snapshot_builder import load_config
from store_agent import AgentSessionStore, OpenAICompatibleResponder, StoreOperationsAgent
from task_store import TaskStore, TaskStoreError

ROOT = Path(__file__).resolve().parent
MAX_BODY = 64 * 1024
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TASK_ID_RE = re.compile(r"^/api/tasks/([A-Za-z0-9_.\-]{1,120})$")
TASK_ACTION_RE = re.compile(r"^/api/tasks/([A-Za-z0-9_.\-]{1,120})/(evidence|status)$")
APPROVAL_DECIDE_RE = re.compile(r"^/api/approvals/(apr-[A-Za-z0-9]{1,60})/decide$")
WRITE_STATUSES = ("accepted", "done", "verified", "rejected", "canceled", "ready")


class AgentHandler(BaseHTTPRequestHandler):
    agent: StoreOperationsAgent
    sessions: AgentSessionStore
    tools: StoreTools
    task_store: TaskStore
    auth: AuthStore
    approvals: ApprovalStore
    tool_log: ToolRunLog
    audit_center: AuditCenter
    reporter: StoreReporter
    auth_mode: str = "session"

    # ---------------- 基础设施 ----------------

    def _headers(self, status: int, content_type: str = "application/json; charset=utf-8",
                 extra: Optional[list[tuple[str, str]]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in extra or []:
            self.send_header(key, value)
        self.end_headers()

    def _json(self, status: int, payload: dict, extra: Optional[list[tuple[str, str]]] = None) -> None:
        self._headers(status, extra=extra)
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("请求大小不合法")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _static(self, path: Path) -> None:
        data = path.read_bytes()
        self._headers(200, mimetypes.guess_type(path.name)[0] or "text/html")
        self.wfile.write(data)

    @property
    def _client_ip(self) -> str:
        return str(self.client_address[0]) if self.client_address else ""

    def _log_tool(self, tool_name: str, context: SecurityContext, session_id: str = "",
                  input_payload: Any = None, output_payload: Any = None,
                  status: str = "success", error: str = "") -> None:
        log_tool_call(self.tool_log, context.store_id, tool_name, actor_id=context.user_id,
                      session_id=session_id or None, input_payload=input_payload,
                      output_payload=output_payload, status=status, error=error or None)

    # ---------------- 身份 ----------------

    def _bearer_token(self) -> str:
        header = self.headers.get("Authorization", "") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        cookies = {}
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                cookies[key.strip()] = value.strip()
        return cookies.get("store_ops_session", "")

    def _demo_context(self, user_id: str) -> SecurityContext:
        user = self.agent.user(user_id or "manager-001")     # 未知用户抛 ValueError
        return SecurityContext(
            user_id=user["user_id"],
            tenant_id=str(self.tools.config.get("tenant_id") or ""),
            store_id=self.tools.store_id,
            role=user["role"],
            display_name=user["name"],
            session_id="demo-mode",
            permissions=permissions_for(user["role"]),
        )

    def _context(self, query: dict, body: dict | None = None) -> SecurityContext:
        if self.auth_mode == "demo":
            user_id = (query.get("user_id") or [""])[0] or str((body or {}).get("user_id") or "")
            return self._demo_context(user_id)
        token = self._bearer_token()
        if not token:
            raise SessionInvalid("缺少认证令牌，请先登录")
        return self.auth.resolve(token)

    def _require(self, permission: str | None = None, query: dict | None = None,
                 body: dict | None = None) -> SecurityContext | None:
        """解析身份并校验权限；失败时已写出响应，返回 None。"""
        try:
            context = self._context(query or {}, body)
        except (SessionInvalid, AuthError) as exc:
            self._json(401, {"error": str(exc)})
            return None
        except ValueError as exc:
            self._json(403, {"error": str(exc)})
            return None
        if permission and not context.has(permission):
            self.auth.record_audit(
                event_type="permission_denied", actor_type="user", actor_id=context.user_id,
                tenant_id=context.tenant_id, store_id=context.store_id,
                object_type="endpoint", object_id=f"{self.command} {self.path}",
                after={"required_permission": permission, "role": context.role},
            )
            self._json(403, {"error": f"当前身份（{context.role_label}）缺少权限：{permission}"})
            return None
        return context

    # ---------------- GET ----------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        task_match = TASK_ID_RE.match(parsed.path)

        if parsed.path == "/api/auth/me":
            context = self._require(query=query)
            if context is None:
                return
            self._json(200, {
                "user": context.to_user_dict(),
                "tenant_id": context.tenant_id,
                "store_id": context.store_id,
                "session_id": context.session_id,
                "expires_at": context.expires_at,
                "permissions": sorted(context.permissions),
            })
            return

        if parsed.path == "/api/bootstrap":
            context = self._require(query=query)
            if context is None:
                return
            user = context.to_user_dict()
            tasks = self.tools.tasks_for(user, limit=50)
            self._json(200, {
                "user": user,
                "store": self.agent.snapshot["store"],
                "task_count": len(tasks),
                "mode": "tools",
                "auth_mode": self.auth_mode,
                "llm_enabled": self.agent.llm_responder is not None,
                "llm_mode": ("tool_calling" if self.agent.tool_agent is not None
                             else ("plain" if self.agent.llm_responder is not None else "off")),
                "data_status": self.tools.data_status() if context.has("metrics:read") else None,
                "permissions": sorted(context.permissions),
            })
            return

        if parsed.path == "/api/data-status":
            context = self._require("metrics:read", query=query)
            if context is None:
                return
            self._json(200, self.tools.data_status())
            return

        if parsed.path == "/api/metrics/summary":
            context = self._require("metrics:read", query=query)
            if context is None:
                return
            try:
                days = int(query.get("days", ["7"])[0])
                summary = self.tools.metric_summary(
                    days=days, include_financial=context.has("metrics:read:financial"))
                self._log_tool("api:metrics_summary", context, input_payload={"days": days},
                               output_payload={"has_data": summary.get("has_data")})
                self._json(200, summary)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/data-quality":
            context = self._require("quality:read", query=query)
            if context is None:
                return
            try:
                days = int(query.get("days", ["30"])[0])
                quality = self.tools.data_quality(days=days)
                self._log_tool("api:data_quality", context, input_payload={"days": days},
                               output_payload={"missing_day_count": quality["summary"]["missing_day_count"],
                                               "anomaly_count": quality["summary"]["anomaly_count"]})
                self._json(200, quality)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/reports/daily":
            context = self._require("report:read", query=query)
            if context is None:
                return
            try:
                report = self.tools.daily_report(query.get("date", ["today"])[0])
                self._log_tool("api:daily_report", context, input_payload={"date": report["date"]},
                               output_payload={"has_data": report["summary"]["has_data"],
                                               "open_task_count": report["summary"]["open_task_count"]})
                self.auth.record_audit(
                    event_type="report_generated", actor_type="user", actor_id=context.user_id,
                    tenant_id=context.tenant_id, store_id=context.store_id,
                    object_type="daily_report", object_id=report["date"],
                )
                self._json(200, report)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/dashboard":
            context = self._require("metrics:read", query=query)
            if context is None:
                return
            try:
                days = int(query.get("days", ["7"])[0])
                include_financial = context.has("metrics:read:financial")
                data = self.reporter.dashboard(days=days, include_financial=include_financial)
                self._log_tool("api:dashboard", context,
                               input_payload={"days": days, "financial": include_financial},
                               output_payload={"has_data": data["has_data"], "trend_points": len(data["trend"])})
                self._json(200, data)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if parsed.path in ("/api/reports/weekly", "/api/reports/monthly"):
            context = self._require("report:read", query=query)
            if context is None:
                return
            try:
                anchor_date = query.get("date", ["today"])[0]
                include_financial = context.has("metrics:read:financial")
                kind = parsed.path.rsplit("/", 1)[-1]
                builder = self.reporter.weekly_report if kind == "weekly" else self.reporter.monthly_report
                report = builder(anchor_date, include_financial=include_financial)
                self._log_tool(f"api:report_{kind}", context, input_payload={"date": anchor_date},
                               output_payload={"period": report["period"], "has_data": report["summary"]["has_data"]})
                self.auth.record_audit(
                    event_type="report_generated", actor_type="user", actor_id=context.user_id,
                    tenant_id=context.tenant_id, store_id=context.store_id,
                    object_type=f"{kind}_report", object_id=report["period"]["end"])
                self._json(200, report)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/approvals":
            context = self._require("approvals:decide", query=query)
            if context is None:
                return
            try:
                status = query.get("status", [None])[0]
                items = self.approvals.list(context.store_id, status=status, limit=100)
                self._log_tool("api:approvals", context, input_payload={"status": status},
                               output_payload={"count": len(items)})
                self._json(200, {
                    "store_id": context.store_id,
                    "status_filter": status or "all",
                    "count_by_status": self.approvals.count_by_status(context.store_id),
                    "approvals": items,
                })
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/audit":
            context = self._require("audit:read", query=query)
            if context is None:
                return
            try:
                limit = int(query.get("limit", ["100"])[0])
                days = int(query.get("days", ["7"])[0])
                event_type = query.get("event_type", [None])[0]
                actor_id = query.get("actor_id", [None])[0]
                events = self.audit_center.list_events(context.store_id, limit=limit,
                                                       event_type=event_type, actor_id=actor_id)
                self._log_tool("api:audit", context, input_payload={"event_type": event_type},
                               output_payload={"count": len(events)})
                self._json(200, {
                    "store_id": context.store_id,
                    "events": events,
                    "event_types": self.audit_center.event_types(context.store_id),
                    "summary": self.audit_center.summary(context.store_id, days=days),
                })
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/tool-runs":
            context = self._require("audit:read", query=query)
            if context is None:
                return
            try:
                limit = int(query.get("limit", ["50"])[0])
                runs = self.tool_log.list(context.store_id, limit=limit,
                                          tool_name=query.get("tool_name", [None])[0])
                self._json(200, {
                    "store_id": context.store_id,
                    "count_by_tool": self.tool_log.count_by_tool(context.store_id),
                    "runs": runs,
                })
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/tasks/today":
            context = self._require("tasks:read:own", query=query)
            if context is None:
                return
            try:
                user = context.to_user_dict()
                tasks = self.tools.tasks_for(user, status=query.get("status", [None])[0], limit=50)
                self._log_tool("api:tasks_today", context, input_payload={"status": query.get("status", [None])[0]},
                               output_payload={"count": len(tasks)})
                self._json(200, {
                    "user": user,
                    "store": self.agent.snapshot["store"],
                    "status_filter": query.get("status", [None])[0] or "all",
                    "count_by_status": self.task_store.count_by_status(context.store_id),
                    "tasks": tasks,
                })
            except (TaskStoreError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return

        if task_match:
            context = self._require("tasks:read:own", query=query)
            if context is None:
                return
            try:
                self._json(200, self.tools.task_detail(task_match.group(1), context.to_user_dict()))
            except PermissionDenied as exc:
                self._json(403, {"error": str(exc)})
            except (ValueError, TaskStoreError) as exc:
                self._json(404, {"error": str(exc)})
            return

        if parsed.path == "/api/history":
            context = self._require(query=query)
            if context is None:
                return
            session_id = query.get("session_id", [""])[0]
            if not SESSION_RE.fullmatch(session_id):
                self._json(400, {"error": "会话不合法"})
                return
            self._json(200, {"messages": self.sessions.history(session_id, context.user_id)})
            return

        if parsed.path.startswith("/static/"):
            candidate = (ROOT / "static" / parsed.path[len("/static/"):]).resolve()
            static_root = (ROOT / "static").resolve()
            if static_root != candidate and static_root in candidate.parents and candidate.is_file():
                self._static(candidate)
            else:
                self._json(404, {"error": "not found"})
            return

        if parsed.path in ("/", "/agent.html"):
            self._static(ROOT / "agent.html")
        elif parsed.path in ("/dashboard", "/dashboard.html"):
            self._static(ROOT / "dashboard.html")
        elif parsed.path in ("/governance", "/governance.html"):
            self._static(ROOT / "governance.html")
        elif parsed.path in ("/settings", "/settings.html"):
            self._static(ROOT / "settings.html")
        elif parsed.path == "/index.html":
            self._static(ROOT / "index.html")
        else:
            self._json(404, {"error": "not found"})

    # ---------------- POST ----------------

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            self._login()
            return
        if parsed.path == "/api/auth/logout":
            self._logout()
            return
        if parsed.path == "/api/chat":
            self._chat()
            return
        if parsed.path == "/api/approvals":
            self._approval_create()
            return
        decide_match = APPROVAL_DECIDE_RE.match(parsed.path)
        if decide_match:
            self._approval_decide(decide_match.group(1))
            return
        action_match = TASK_ACTION_RE.match(parsed.path)
        if action_match:
            self._task_write(action_match.group(2), action_match.group(1))
            return
        self._json(404, {"error": "not found"})

    def _login(self) -> None:
        try:
            body = self._read_json()
            username = str(body.get("username", "")).strip()
            password = str(body.get("password", ""))
            store_id = body.get("store_id") or None
            result = self.auth.login(username, password, store_id, actor_ip=self._client_ip)
            context = result["context"]
            if context.store_id != self.tools.store_id:
                # 令牌只对签发的门店有效；当前服务只挂载一个门店
                raise AuthError(f"该账号登录的门店是 {context.store_id}，本服务只服务 {self.tools.store_id}")
            max_age = 43200
            csrf = secrets.token_urlsafe(24)
            cookies = [
                ("Set-Cookie", f"store_ops_session={result['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"),
                ("Set-Cookie", f"store_ops_csrf={csrf}; Path=/; SameSite=Strict; Max-Age={max_age}"),
            ]
            self._json(200, {
                "token": result["token"],
                "expires_at": result["expires_at"],
                "user": context.to_user_dict(),
                "tenant_id": context.tenant_id,
                "store_id": context.store_id,
                "permissions": sorted(context.permissions),
            }, extra=cookies)
        except AccountLocked as exc:
            self._json(423, {"error": str(exc)})
        except (AuthError, ValueError, json.JSONDecodeError) as exc:
            self._json(401, {"error": str(exc)})

    def _logout(self) -> None:
        token = self._bearer_token()
        if self.auth_mode == "demo":
            self._json(200, {"revoked": False, "auth_mode": "demo"})
            return
        if not token:
            self._json(401, {"error": "缺少认证令牌"})
            return
        context = self._require()
        if context is None:
            return
        revoked = self.auth.logout(token)
        self.auth.record_audit(event_type="logout", actor_type="user", actor_id=context.user_id,
                               tenant_id=context.tenant_id, store_id=context.store_id,
                               object_type="session", object_id=context.session_id)
        self._json(200, {"revoked": revoked}, extra=[
            ("Set-Cookie", "store_ops_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"),
            ("Set-Cookie", "store_ops_csrf=; Path=/; SameSite=Strict; Max-Age=0"),
        ])

    def _chat(self) -> None:
        body: dict = {}
        try:
            body = self._read_json()
            context = self._require("agent:chat", body=body)
            if context is None:
                return
            session_id = str(body.get("session_id", ""))
            message = str(body.get("message", ""))
            if not SESSION_RE.fullmatch(session_id):
                raise ValueError("会话不合法")
            if len(message) > 2000:
                raise ValueError("单条消息不能超过 2000 字")
            # 会话绑定到认证身份；换人直接拒绝
            self.task_store.bind_session(session_id, context.user_id, context.role)
            history = self.sessions.history(session_id, context.user_id, limit=10)
            result = self.agent.reply(message, context.user_id, history=history,
                                      user=context.to_user_dict())
            self.sessions.append(session_id, context.user_id, "user", message)
            self.sessions.append(session_id, context.user_id, "assistant", result["answer"], result["intent"])
            self._log_tool(f"agent:{result['intent']}", context, session_id=session_id,
                           input_payload={"message": message, "length": len(message)},
                           output_payload={"requires_approval": result["requires_approval"]})
            self._json(200, result)
        except PermissionDenied as exc:
            self._json(403, {"error": str(exc)})
        except TaskStoreError as exc:
            self._json(403, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})

    def _approval_create(self) -> None:
        try:
            body = self._read_json()
            context = self._require("approvals:request", body=body)
            if context is None:
                return
            action_type = str(body.get("action_type", "")).strip()
            if not action_type:
                raise ValueError("action_type 不能为空")
            approval = self.approvals.request(
                store_id=context.store_id, action_type=action_type,
                payload=body.get("payload") or {}, requested_by=context.display_name,
                task_id=body.get("task_id") or None, reason=str(body.get("reason", "")),
            )
            self.auth.record_audit(
                event_type="approval_requested", actor_type="user", actor_id=context.user_id,
                tenant_id=context.tenant_id, store_id=context.store_id,
                object_type="approval", object_id=approval["approval_id"],
                after={"action_type": action_type, "task_id": approval.get("task_id"),
                       "skipped": approval.get("skipped")},
            )
            self._json(200, approval)
        except PermissionDenied as exc:
            self._json(403, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})

    def _approval_decide(self, approval_id: str) -> None:
        try:
            body = self._read_json()
            context = self._require("approvals:decide", body=body)
            if context is None:
                return
            decision = str(body.get("decision", "")).strip()
            reason = str(body.get("reason", ""))
            result = apply_decision(self.approvals, self.task_store, approval_id, decision,
                                    context.display_name, reason)
            self.auth.record_audit(
                event_type="approval_decided", actor_type="user", actor_id=context.user_id,
                tenant_id=context.tenant_id, store_id=context.store_id,
                object_type="approval", object_id=approval_id, before={"status": "pending"},
                after={"status": result["approval"]["status"], "reason": reason,
                       "task_status": (result["task"] or {}).get("status")},
            )
            self._json(200, result)
        except PermissionDenied as exc:
            self._json(403, {"error": str(exc)})
        except TaskStoreError as exc:
            self._json(409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})

    def _task_write(self, action: str, task_id: str) -> None:
        body: dict = {}
        try:
            body = self._read_json()
            context = self._require("tasks:read:own", body=body)
            if context is None:
                return
            user = context.to_user_dict()
            task = self.task_store.get_task(task_id)
            if task["store_id"] != context.store_id:
                raise PermissionDenied(f"任务 {task_id} 不属于当前门店")
            if action == "evidence":
                if not self.tools.can_submit_evidence(user, task):
                    raise PermissionDenied("只有任务负责人或管理者可以提交证据")
                evidence_type = str(body.get("evidence_type", "")).strip()
                object_ref = str(body.get("object_ref", "")).strip()
                self.task_store.add_evidence(task_id, evidence_type, object_ref, context.display_name, body.get("note"))
                rows = self.task_store.evidence(task_id)
                self.auth.record_audit(
                    event_type="task_evidence_submitted", actor_type="user", actor_id=context.user_id,
                    tenant_id=context.tenant_id, store_id=context.store_id,
                    object_type="action_task", object_id=task_id,
                    after={"evidence_type": evidence_type, "object_ref": object_ref},
                )
                self._json(200, {"evidence": rows[-1], "task": self.task_store.get_task(task_id)})
                return
            to_status = str(body.get("status", "")).strip()
            if to_status not in WRITE_STATUSES:
                raise ValueError(f"不支持的流转目标：{to_status}")
            if not self.tools.can_change_status(user, task, to_status):
                raise PermissionDenied(f"当前身份无权把任务改为 {to_status}")
            before = task["status"]
            updated = self.task_store.change_status(task_id, to_status, context.display_name,
                                                    str(body.get("reason", "")))
            self.auth.record_audit(
                event_type="task_status_changed", actor_type="user", actor_id=context.user_id,
                tenant_id=context.tenant_id, store_id=context.store_id,
                object_type="action_task", object_id=task_id,
                before={"status": before}, after={"status": to_status, "reason": body.get("reason", "")},
            )
            self._json(200, updated)
        except PermissionDenied as exc:
            self._json(403, {"error": str(exc)})
        except TaskStoreError as exc:
            self._json(409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="启动门店运营 Agent 本地服务")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--config", type=Path, default=ROOT / "store_config.json")
    parser.add_argument("--db", default=str(ROOT / "data" / "agent_demo.db"),
                        help="对话消息库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--store-db", default=str(ROOT / "data" / "store_ops.db"),
                        help="任务/证据/审计/身份库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--metrics-db", default=str(ROOT / "data" / "platform_metrics.db"),
                        help="平台指标库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--auth-mode", choices=("session", "demo"), default="session",
                        help="session=必须登录（默认）；demo=允许伪身份，仅限本机演示")
    args = parser.parse_args()

    config = load_config(args.config)
    tenant_id = str(config.get("tenant_id") or "")
    # 注册门店并做跨租户守卫（同一 store_id 不允许出现在两个租户下）
    RegistryStore(args.store_db, tenant_id=tenant_id).register(config)
    metric_store = PlatformMetricStore(args.metrics_db, tenant_id=tenant_id)
    task_store = TaskStore(args.store_db, tenant_id=tenant_id)
    auth_store = AuthStore(args.store_db, tenant_id=tenant_id)
    tools = StoreTools(config, metric_store, task_store, config_path=args.config)
    llm_responder = OpenAICompatibleResponder.from_environment()

    AgentHandler.tools = tools
    AgentHandler.task_store = task_store
    AgentHandler.auth = auth_store
    AgentHandler.approvals = ApprovalStore(args.store_db, tenant_id=tenant_id)
    AgentHandler.tool_log = ToolRunLog(args.store_db, tenant_id=tenant_id)
    AgentHandler.audit_center = AuditCenter(args.store_db, tenant_id=tenant_id)
    AgentHandler.reporter = StoreReporter(config, metric_store, task_store,
                                          ApprovalStore(args.store_db, tenant_id=tenant_id),
                                          config_path=args.config)
    AgentHandler.auth_mode = args.auth_mode
    AgentHandler.sessions = AgentSessionStore(args.db, tenant_id=tenant_id)

    # 工具调用（批次 13）：模型可用时才启用；tool_log 必须已就绪
    tool_responder = OpenAIToolResponder.from_environment()
    tool_agent = None
    if tool_responder is not None:
        tool_agent = ToolCallingAgent(tools, tool_responder, tool_log=AgentHandler.tool_log,
                                      audit=auth_store, tenant_id=tenant_id)
    AgentHandler.agent = StoreOperationsAgent.from_tools(config, tools, llm_responder, tool_agent)

    if args.auth_mode == "demo":
        print("!! 警告：当前为 demo 认证模式，任意请求都能用 ?user_id= 冒充身份，仅可用于本机演示。")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), AgentHandler)
    llm_status = ("已接入大模型（含工具调用）" if tool_agent is not None
                  else ("已接入大模型（纯文本）" if llm_responder else "规则模式（未配置大模型）"))
    print(f"门店运营 Agent 已启动：http://127.0.0.1:{args.port}/ · {llm_status} · "
          f"store={tools.store_id} · auth={args.auth_mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
