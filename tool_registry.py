#!/usr/bin/env python3
"""工具模板/实例注册表（第 8 期）：可登记、可部署、可隔离执行。

三条边界：
1. **模板与实例两级**：模板是 manifest（受控入口 + 策略），实例是"模板 + 门店 + 凭据引用"。
2. **凭据隔离**：`tool_credentials` 只存 sha256 哈希与掩码，**不落明文**；导出 ZIP 里 secret 一律为空。
3. **隔离执行**：调用走 `tool_sandbox.run_sandbox()`（独立子进程 + 超时 + 网络策略），
   越权 / 网络策略拒绝 / 超时都会写 `agent_tool_runs` 留痕。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Database
from governance import log_tool_call
from tool_manifest import import_template_zip, export_template_zip, validate_manifest
from tool_sandbox import MAX_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, SandboxError, run_sandbox

INSTANCE_STATUSES = ("active", "disabled")


class ToolRegistryError(ValueError):
    """模板/实例的参数或状态错误。"""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def mask_secret(secret: Any) -> str:
    text = str(secret or "")
    if len(text) <= 4:
        return "****"
    return f"{text[:2]}****{text[-2:]}"


def hash_secret(secret: Any) -> str:
    return hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()


class ToolRegistry:
    """`tool_templates` / `tool_instances` / `tool_credentials` 的读写与受控调用。"""

    def __init__(self, db_target: Any, tenant_id: str = "", *, tool_log: Any = None):
        self.db = Database(db_target, tenant_id=tenant_id)
        self.tenant_id = tenant_id
        self.tool_log = tool_log
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connection(self):
        return self.db.connection()

    def _init_schema(self) -> None:
        if self.db.dialect == "postgresql":
            with self._connection() as conn:
                self.db.apply_schema(conn)
            return
        with self._connection() as conn:
            conn.executescript("""
                create table if not exists tool_templates (
                  tenant_id text not null default '',
                  template_id text not null,
                  store_id text not null,
                  template_key text not null,
                  name text not null,
                  entrypoint text not null,
                  manifest_json text not null,
                  network_policy text not null default 'deny',
                  network_required integer not null default 0,
                  timeout_seconds real not null default 30,
                  limits_json text not null default '{}',
                  status text not null default 'active',
                  created_by text not null default '',
                  created_at text not null,
                  primary key (tenant_id, template_id),
                  unique (tenant_id, store_id, template_key)
                );
                create table if not exists tool_instances (
                  tenant_id text not null default '',
                  instance_id text not null,
                  store_id text not null,
                  template_id text not null,
                  instance_key text not null,
                  credential_id text,
                  network_policy text not null default '',
                  status text not null default 'active' check (status in ('active','disabled')),
                  created_by text not null default '',
                  created_at text not null,
                  primary key (tenant_id, instance_id),
                  unique (tenant_id, store_id, instance_key)
                );
                create table if not exists tool_credentials (
                  tenant_id text not null default '',
                  credential_id text not null,
                  store_id text not null,
                  instance_id text not null,
                  secret_hash text not null,
                  secret_mask text not null default '',
                  created_at text not null,
                  primary key (tenant_id, credential_id)
                );
                """)

    def _require_text(self, value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ToolRegistryError(f"{field} 不能为空")
        return text

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ---------------- 模板 ----------------

    def register_template(self, store_id: str, manifest: Any, *, actor: str = "") -> dict[str, Any]:
        store_id = self._require_text(store_id, "store_id")
        normalized = validate_manifest(manifest)
        moment = self._now()
        with self._connection() as conn:
            existing = conn.execute(
                "select * from tool_templates where tenant_id=? and store_id=? and template_key=?",
                (self.tenant_id, store_id, normalized["template_key"])).fetchone()
            if existing:
                return self._template(dict(existing))
            template_id = f"tpl-{uuid.uuid4().hex[:12]}"
            conn.execute(
                """insert into tool_templates (tenant_id,template_id,store_id,template_key,name,
                   entrypoint,manifest_json,network_policy,network_required,timeout_seconds,limits_json,
                   status,created_by,created_at) values (?,?,?,?,?,?,?,?,?,?,?,'active',?,?)""",
                (self.tenant_id, template_id, store_id, normalized["template_key"], normalized["name"],
                 normalized["entrypoint"], _dump(normalized), normalized["network_policy"],
                 1 if normalized["network_required"] else 0, normalized["timeout_seconds"],
                 _dump(normalized["limits"]), str(actor or ""), moment))
        return self.get_template(template_id)

    def get_template(self, template_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from tool_templates where tenant_id=? and template_id=?",
                               (self.tenant_id, str(template_id))).fetchone()
        if not row:
            raise ToolRegistryError(f"工具模板不存在：{template_id}")
        return self._template(dict(row))

    @staticmethod
    def _template(row: dict[str, Any]) -> dict[str, Any]:
        row["manifest"] = _loads(row.get("manifest_json"), {})
        row["limits"] = _loads(row.get("limits_json"), {})
        row["network_required"] = bool(row.get("network_required"))
        return row

    def list_templates(self, store_id: str) -> list[dict[str, Any]]:
        store_id = self._require_text(store_id, "store_id")
        with self._connection() as conn:
            rows = conn.execute(
                "select * from tool_templates where tenant_id=? and store_id=? order by template_key",
                (self.tenant_id, store_id)).fetchall()
        return [self._template(dict(row)) for row in rows]

    # ---------------- 实例与凭据 ----------------

    def create_instance(self, store_id: str, template_id: str, instance_key: str, *,
                        credentials: Optional[dict[str, Any]] = None, network_policy: str = "",
                        actor: str = "") -> dict[str, Any]:
        store_id = self._require_text(store_id, "store_id")
        instance_key = self._require_text(instance_key, "instance_key")
        template = self.get_template(template_id)
        if template["store_id"] != store_id:
            raise ToolRegistryError("模板不属于当前门店")
        moment = self._now()
        instance_id = f"tins-{uuid.uuid4().hex[:12]}"
        credential_id = ""
        with self._connection() as conn:
            if credentials:
                credential_id = f"tcred-{uuid.uuid4().hex[:12]}"
                for _, secret in credentials.items():
                    conn.execute(
                        """insert into tool_credentials (tenant_id,credential_id,store_id,instance_id,
                           secret_hash,secret_mask,created_at) values (?,?,?,?,?,?,?)""",
                        (self.tenant_id, credential_id, store_id, instance_id,
                         hash_secret(secret), mask_secret(secret), moment))
                    break
            conn.execute(
                """insert into tool_instances (tenant_id,instance_id,store_id,template_id,instance_key,
                   credential_id,network_policy,status,created_by,created_at)
                   values (?,?,?,?,?,?,?,'active',?,?)""",
                (self.tenant_id, instance_id, store_id, template["template_id"], instance_key,
                 credential_id or None, str(network_policy or ""), str(actor or ""), moment))
        return self.get_instance(instance_id)

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from tool_instances where tenant_id=? and instance_id=?",
                               (self.tenant_id, str(instance_id))).fetchone()
            if not row:
                raise ToolRegistryError(f"工具实例不存在：{instance_id}")
            record = dict(row)
            credential = conn.execute(
                "select * from tool_credentials where tenant_id=? and instance_id=?",
                (self.tenant_id, str(instance_id))).fetchone()
        record["credential"] = dict(credential) if credential else None
        return record

    def list_instances(self, store_id: str) -> list[dict[str, Any]]:
        store_id = self._require_text(store_id, "store_id")
        with self._connection() as conn:
            rows = conn.execute(
                "select * from tool_instances where tenant_id=? and store_id=? order by instance_key",
                (self.tenant_id, store_id)).fetchall()
        return [self.get_instance(row["instance_id"]) for row in rows]

    # ---------------- 受控调用 ----------------

    def invoke(self, instance_id: str, payload: Optional[dict[str, Any]] = None, *,
               actor: str = "", timeout: Optional[float] = None,
               store_id: Optional[str] = None) -> dict[str, Any]:
        instance = self.get_instance(instance_id)
        template = self.get_template(instance["template_id"])
        if store_id and instance["store_id"] != store_id:
            self._log(instance, actor, status="blocked", error="实例不属于当前门店", payload=payload)
            raise ToolRegistryError("实例不属于当前门店")
        if instance["status"] != "active":
            self._log(instance, actor, status="blocked", error="实例已停用", payload=payload)
            raise ToolRegistryError(f"实例已停用：{instance_id}")
        policy = instance.get("network_policy") or template["network_policy"]
        seconds = float(timeout if timeout is not None else template["timeout_seconds"])
        if not MIN_TIMEOUT_SECONDS <= seconds <= MAX_TIMEOUT_SECONDS:
            self._log(instance, actor, status="blocked", error="超时参数越界", payload=payload)
            raise ToolRegistryError(f"timeout 必须在 {MIN_TIMEOUT_SECONDS}-{MAX_TIMEOUT_SECONDS} 秒之间")
        try:
            result = run_sandbox(template["entrypoint"], payload, timeout=seconds, network=policy,
                                 network_required=template["network_required"],
                                 limits=template["limits"])
        except SandboxError as exc:
            self._log(instance, actor, status="blocked", error=str(exc), payload=payload)
            raise ToolRegistryError(str(exc)) from exc
        self._log(instance, actor, status="success", payload=payload, output=result)
        return {**result, "instance_id": instance["instance_id"], "template_id": template["template_id"],
                "instance_key": instance["instance_key"]}

    def _log(self, instance: dict[str, Any], actor: str, *, status: str,
             payload: Any = None, output: Any = None, error: Optional[str] = None) -> None:
        log_tool_call(self.tool_log, instance["store_id"],
                      f"tool_instance:{instance['instance_key']}", actor,
                      input_payload=payload, output_payload=output, status=status, error=error)

    # ---------------- 导入导出 ----------------

    def export_template(self, template_id: str, *, secrets: Optional[dict[str, Any]] = None) -> bytes:
        template = self.get_template(template_id)
        return export_template_zip(template["manifest"], secrets=secrets)

    def import_template(self, data: bytes, store_id: str, *, actor: str = "") -> dict[str, Any]:
        imported = import_template_zip(data)
        template = self.register_template(store_id, imported["manifest"], actor=actor)
        return {"template": template, "credentials": imported["credentials"]}