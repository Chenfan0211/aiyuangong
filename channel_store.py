#!/usr/bin/env python3
"""渠道身份绑定仓储（AI 店长第一阶段）。

职责：把外部聊天渠道（企微/飞书/mock 等）的账号和门店内部用户绑定起来，
使入站消息能解析出"哪个租户、哪个门店、哪个用户/角色"，从而按现有权限查经营数据。

设计约束：
  * 单一身份键 = `tenant_id + store_id + provider + external_user_id`，同一作用域不可重复绑定；
  * 手机号**不存明文**，只存 HMAC-SHA256 哈希（辅助校验 + 唯一性）和脱敏值 `138****8000`；
    哈希密钥优先 `STORE_AGENT_MOBILE_HASH_KEY`，其次 `STORE_AGENT_INBOUND_SIGNING_SECRET`，
    都未配置时退化为确定性 SHA-256（测试可复现，不用于生产）；
  * `resolve()` 必须 join `users` / `memberships`，只在绑定启用、用户启用、会员启用时返回，
    跨门店、跨租户、缺会员一律返回 `None`（上层据此只回绑定提示，不泄露任何门店数据）；
  * 表结构在 SQLite / PostgreSQL 上由本模块的幂等 DDL 建立（两库同一份语句）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Database

BINDING_STATUSES = ("active", "disabled")
MOBILE_HASH_ENV_KEYS = ("STORE_AGENT_MOBILE_HASH_KEY", "STORE_AGENT_INBOUND_SIGNING_SECRET")
MOBILE_PATTERN = re.compile(r"1[3-9][0-9]{9}\Z")
MOBILE_STRIP_PATTERN = re.compile(r"[\s\-]")

CHANNEL_BINDING_DDL = """
create table if not exists channel_bindings (
  binding_id text primary key,
  tenant_id text not null default '',
  store_id text not null,
  provider text not null,
  external_user_id text not null,
  display_name text not null default '',
  mobile_hash text not null default '',
  mobile_masked text not null default '',
  user_id text,
  status text not null check (status in ('active','disabled')),
  created_by text not null default '',
  created_at text not null,
  updated_at text not null,
  unique (tenant_id, store_id, provider, external_user_id)
);
create unique index if not exists ux_channel_bindings_mobile
  on channel_bindings(tenant_id, store_id, provider, mobile_hash) where mobile_hash <> '';
create index if not exists idx_channel_bindings_user on channel_bindings(user_id);
"""

SELECT_COLUMNS = ("binding_id, tenant_id, store_id, provider, external_user_id, display_name, "
                  "mobile_hash, mobile_masked, user_id, status, created_by, created_at, updated_at")


class ChannelBindingError(ValueError):
    """绑定重复、绑定不存在等业务错误。"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_mobile(raw: Any) -> str:
    """归一化中国 11 位手机号：允许空格/短横线/`86` 或 `+86` 前缀，非法抛 ValueError。"""
    if raw is None:
        raise ValueError("手机号格式不合法")
    text = MOBILE_STRIP_PATTERN.sub("", str(raw).strip())
    if text.startswith("+"):
        text = text[1:]
    if len(text) == 13 and text.startswith("86"):
        text = text[2:]
    if not MOBILE_PATTERN.match(text):
        raise ValueError("手机号格式不合法")            # 不回显原始值，避免写入日志
    return text


def mask_mobile(raw: Any) -> str:
    """脱敏展示值：`13800138000` → `138****8000`。"""
    normalized = normalize_mobile(raw)
    return f"{normalized[:3]}****{normalized[-4:]}"


def mobile_hash(raw: Any) -> str:
    """手机号哈希：先归一化，再用环境密钥做 HMAC-SHA256，无配置时退化为确定性 SHA-256。"""
    normalized = normalize_mobile(raw)
    key = ""
    for name in MOBILE_HASH_ENV_KEYS:
        key = (os.getenv(name) or "").strip()
        if key:
            break
    if key:
        return hmac.new(key.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(f"channel-binding|{normalized}".encode("utf-8")).hexdigest()


def _public(row: Any) -> Optional[dict[str, Any]]:
    """对外的行形态：剔除哈希，只留脱敏手机号。"""
    if row is None:
        return None
    data = dict(row)
    data.pop("mobile_hash", None)
    return data


def _required(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ChannelBindingError(f"{field} 不能为空")
    return text


class ChannelBindingStore:
    """渠道绑定仓储。`db_target` 可以是 SQLite 文件路径，也可以是 PostgreSQL DSN。"""

    def __init__(self, db_target: Any, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connection(self):
        return self.db.connection()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(CHANNEL_BINDING_DDL)

    # ---------------- 绑定管理 ----------------

    def create(
        self,
        *,
        tenant_id: str,
        store_id: str,
        provider: str,
        external_user_id: str,
        display_name: str = "",
        mobile: Any = None,
        user_id: Optional[str] = None,
        created_by: str = "system",
    ) -> dict[str, Any]:
        tenant_id = _required(tenant_id, "tenant_id")
        store_id = _required(store_id, "store_id")
        provider = _required(provider, "provider")
        external_user_id = _required(external_user_id, "external_user_id")
        display_name = str(display_name or "")

        mobile_digest = ""
        masked = ""
        if mobile is not None and str(mobile).strip():
            normalized = normalize_mobile(mobile)
            mobile_digest = mobile_hash(normalized)
            masked = mask_mobile(normalized)

        binding_id = f"cb-{secrets.token_hex(8)}"
        moment = now_iso()
        with self._connection() as conn:
            existing = conn.execute(
                "select binding_id from channel_bindings "
                "where tenant_id=? and store_id=? and provider=? and external_user_id=?",
                (tenant_id, store_id, provider, external_user_id),
            ).fetchone()
            if existing:
                raise ChannelBindingError(f"该渠道账号已绑定：{provider}/{external_user_id}")
            if mobile_digest:
                duplicate = conn.execute(
                    "select binding_id from channel_bindings "
                    "where tenant_id=? and store_id=? and provider=? and mobile_hash=?",
                    (tenant_id, store_id, provider, mobile_digest),
                ).fetchone()
                if duplicate:
                    raise ChannelBindingError("该手机号在本门店已绑定")
            conn.execute(
                """insert into channel_bindings(binding_id,tenant_id,store_id,provider,external_user_id,
                   display_name,mobile_hash,mobile_masked,user_id,status,created_by,created_at,updated_at)
                   values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (binding_id, tenant_id, store_id, provider, external_user_id, display_name,
                 mobile_digest, masked, user_id, "active", str(created_by or ""), moment, moment),
            )
            row = conn.execute(f"select {SELECT_COLUMNS} from channel_bindings where binding_id=?",
                               (binding_id,)).fetchone()
        return _public(row) or {}

    def list(self, tenant_id: str, store_id: Optional[str] = None) -> list[dict[str, Any]]:
        sql = f"select {SELECT_COLUMNS} from channel_bindings where tenant_id=?"
        params: list[Any] = [tenant_id]
        if store_id is not None:
            sql += " and store_id=?"
            params.append(store_id)
        sql += " order by created_at, binding_id"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_public(row) for row in rows]

    def update(
        self,
        binding_id: str,
        *,
        display_name: Optional[str] = None,
        mobile: Any = None,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        if status is not None and status not in BINDING_STATUSES:
            raise ChannelBindingError("绑定状态只能是 active 或 disabled")
        with self._connection() as conn:
            current = conn.execute(
                f"select {SELECT_COLUMNS} from channel_bindings where binding_id=?", (binding_id,)
            ).fetchone()
            if current is None:
                raise ChannelBindingError(f"绑定不存在：{binding_id}")

            fields: dict[str, Any] = {}
            if display_name is not None:
                fields["display_name"] = str(display_name)
            if mobile is not None:
                if not str(mobile).strip():
                    fields["mobile_hash"] = ""
                    fields["mobile_masked"] = ""
                else:
                    normalized = normalize_mobile(mobile)
                    digest = mobile_hash(normalized)
                    duplicate = conn.execute(
                        "select binding_id from channel_bindings where tenant_id=? and store_id=? "
                        "and provider=? and mobile_hash=? and binding_id<>?",
                        (current["tenant_id"], current["store_id"], current["provider"],
                         digest, binding_id),
                    ).fetchone()
                    if duplicate:
                        raise ChannelBindingError("该手机号在本门店已绑定")
                    fields["mobile_hash"] = digest
                    fields["mobile_masked"] = mask_mobile(normalized)
            if status is not None:
                fields["status"] = status

            if fields:
                fields["updated_at"] = now_iso()
                assignments = ", ".join(f"{column}=?" for column in fields)
                conn.execute(f"update channel_bindings set {assignments} where binding_id=?",
                             (*fields.values(), binding_id))

            row = conn.execute(f"select {SELECT_COLUMNS} from channel_bindings where binding_id=?",
                               (binding_id,)).fetchone()
        return _public(row) or {}

    def disable(self, binding_id: str) -> dict[str, Any]:
        return self.update(binding_id, status="disabled")

    # ---------------- 身份解析 ----------------

    def resolve(self, tenant_id: str, store_id: str, provider: str, external_user_id: str, mobile: Any = None):
        """按作用域解析绑定；绑定停用、用户停用、缺会员、跨门店/租户都返回 None。"""
        with self._connection() as conn:
            row = conn.execute(
                """select b.binding_id, b.tenant_id, b.store_id, b.provider, b.external_user_id,
                          b.display_name, b.mobile_hash, b.mobile_masked, b.user_id, b.status, b.created_by,
                          b.created_at, b.updated_at, u.username, u.display_name as user_display_name,
                          m.role
                   from channel_bindings b
                   join users u on u.user_id=b.user_id and u.tenant_id=b.tenant_id
                   join memberships m on m.user_id=b.user_id and m.tenant_id=b.tenant_id
                                     and m.store_id=b.store_id
                   where b.tenant_id=? and b.store_id=? and b.provider=? and b.external_user_id=?
                     and b.status='active' and u.status='active' and m.status='active'""",
                (tenant_id, store_id, provider, external_user_id),
            ).fetchone()
        if row is not None and mobile is not None and str(mobile).strip():
            if not hmac.compare_digest(str(row.get("mobile_hash") or ""), mobile_hash(mobile)):
                return None
        return _public(row)