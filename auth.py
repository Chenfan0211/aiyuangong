#!/usr/bin/env python3
"""身份、会话、授权与审计（二期批次 7）。

这是**框架无关的安全核心**：口令哈希、会话令牌、RBAC 策略、租户/门店作用域、审计与脱敏。
批次 8 迁到 FastAPI + PostgreSQL 时，这一层可以整体复用，只替换传输层（HTTP 路由/依赖）。

设计要点：
  - 口令用标准库 `hashlib.scrypt` + 每用户盐哈希，绝不明文存储；
  - 会话令牌是 `secrets.token_urlsafe(32)`，数据库**只存 SHA-256 哈希**（泄露不可反推）；
  - 会话有 TTL（默认 12 小时）并在使用中滑动续期，支持撤销与过期清理；
  - 连续登录失败 5 次锁定 15 分钟（按用户名），成功即清零；
  - RBAC 是显式策略表（角色 → 权限点），权限点粒度到"看敏感财务指标""验收任务"这种程度；
  - 每次身份与权限事件写 `audit_events`，写入前对手机号/身份证/密钥字段脱敏。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from db import Database

ROLES = ("owner", "area_manager", "manager", "employee", "operator")

ROLE_LABELS = {
    "owner": "店主",
    "area_manager": "区域负责人",
    "manager": "店长",
    "employee": "店员",
    "operator": "线上运营",
}

PERMISSIONS = (
    "agent:chat",
    "metrics:read",            # 非敏感聚合：曝光/访问/订单/核销/转化率
    "metrics:read:financial",  # GMV / 退款 / 毛利等经营敏感项
    "quality:read",            # 数据质量体检（含全店数据）
    "report:read",             # 店长日报 / 周报
    "tasks:read:store",        # 查看全店任务
    "tasks:read:own",          # 只查看本人任务
    "tasks:accept",            # 接受/完成自己的任务
    "tasks:evidence:submit",
    "tasks:verify",            # 验收/驳回（仅管理者）
    "approvals:request",       # 提交高风险动作审批申请
    "approvals:decide",        # 改价/赔付/外发等审批
    "pricing:draft",           # 生成调价草稿（不含执行）
    "audit:read",
    "config:write",            # 修改门店配置
    "store:manage",            # 管理门店与成员
    "ops:import",              # 导入门店侧经营数据（开台/时段/库存/商圈）
    "competitor:match",        # 人工确认"本店套餐 ↔ 竞品套餐"的匹配（判断动作，仅店长级以上）
    "rules:read",             # 查看规则定义与版本（不含审批）
    "rules:write",            # 提交规则草稿（新增版本，不代表生效）
    "rules:decide",           # 审批/暂停/退役规则版本
    "inspection:read",        # 查看巡店模板与巡店任务
    "inspection:manage",      # 维护巡店模板（固定字段）
    "inspection:submit",      # 生成/提交巡店任务与证据
    "appeals:submit",         # 员工对奖惩草稿提申诉
    "appeals:decide",         # 受理申诉并给出结论
    "knowledge:read",         # 检索门店知识库（对话引用来源）
    "knowledge:manage",       # 维护知识库与导入文档
    "sop:read",               # 查看 SOP 定义与运行状态
    "sop:manage",             # 定义/版本化 SOP
    "sop:run",                # 发起与恢复 SOP 运行
    "tool:read",              # 查看工具模板与实例
    "tool:manage",            # 登记模板/派生实例/导入导出
    "tool:invoke",            # 在沙箱里调用工具实例
    "memory:read",             # 召回长期记忆
    "memory:write",            # 沉淀记忆（抽取后落库）
    "evolution:read",          # 查看进化提案
    "evolution:decide",        # 人工确认提案（只改提案状态）
    "workforce:read",          # 查看数字员工与团队
    "workforce:manage",        # 登记数字员工/建团队/派团队任务
)

_BASE_WORKER = {"agent:chat", "metrics:read", "tasks:read:own", "tasks:accept",
                "tasks:evidence:submit", "approvals:request"}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(PERMISSIONS),
    "area_manager": frozenset({
        "agent:chat", "metrics:read", "metrics:read:financial", "quality:read", "report:read",
        "tasks:read:store", "tasks:read:own", "tasks:accept", "tasks:evidence:submit", "tasks:verify",
        "approvals:request", "approvals:decide", "audit:read", "ops:import",
        "competitor:match",
        "rules:read", "rules:write", "rules:decide",
        "inspection:read", "inspection:manage", "inspection:submit", "appeals:decide",
        "knowledge:read", "knowledge:manage",
        "sop:read", "sop:manage", "sop:run",
        "tool:read", "tool:manage", "tool:invoke",
        "memory:read", "memory:write", "evolution:read", "evolution:decide",
        "workforce:read", "workforce:manage",
    }),
    "manager": frozenset({
        "agent:chat", "metrics:read", "metrics:read:financial", "quality:read", "report:read",
        "tasks:read:store", "tasks:read:own", "tasks:accept", "tasks:evidence:submit", "tasks:verify",
        "approvals:request", "approvals:decide", "pricing:draft", "ops:import",
        "competitor:match",
        "rules:read", "rules:write",
        "inspection:read", "inspection:manage", "inspection:submit", "appeals:decide",
            "knowledge:read", "knowledge:manage",
        "sop:read", "sop:manage", "sop:run",
        "tool:read", "tool:manage", "tool:invoke",
        "memory:read", "memory:write", "evolution:read", "evolution:decide",
        "workforce:read", "workforce:manage",
}),
    "operator": frozenset({
        "agent:chat", "metrics:read", "metrics:read:financial", "quality:read",
        "tasks:read:store", "tasks:read:own", "tasks:accept", "tasks:evidence:submit",
        "approvals:request", "pricing:draft", "ops:import",
        "rules:read",
        "inspection:read", "inspection:submit",
        "knowledge:read",
        "sop:read", "sop:run",
        "tool:read", "tool:invoke",
        "memory:read", "memory:write",
        "workforce:read",
    }),
    "employee": frozenset(_BASE_WORKER | {"inspection:read", "appeals:submit", "knowledge:read", "memory:read"}),
}

WEAK_PASSWORDS = {
    "password", "password1", "12345678", "123456789", "1234567890", "qwerty123",
    "admin123", "iloveyou", "11111111", "88888888", "abc12345", "store123",
}
MIN_PASSWORD_LENGTH = 10
MAX_LOGIN_FAILURES = 5
LOCK_MINUTES = 15
SESSION_TTL_HOURS = 12
ABSOLUTE_SESSION_TTL_DAYS = 7
MFA_ROLES = frozenset({"owner", "area_manager", "manager"})
MFA_CHALLENGE_MINUTES = 5
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
ID_CARD_RE = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[0-9Xx])(?!\d)")
SECRET_KEYS = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "credential")




def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_key(secret: str) -> bytes:
    padded = (secret or "").strip().upper() + "=" * ((8 - len(secret.strip()) % 8) % 8)
    return base64.b32decode(padded)


def totp_code(secret: str, timestamp: Optional[float] = None, digits: int = 6, period: int = 30) -> str:
    counter = int(timestamp if timestamp is not None else time.time()) // period
    digest = hmac.new(_totp_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def verify_totp(secret: str, code: str, timestamp: Optional[float] = None, window: int = 1) -> bool:
    candidate = re.sub(r"\D", "", str(code or ""))
    if len(candidate) != 6:
        return False
    moment = timestamp if timestamp is not None else time.time()
    return any(hmac.compare_digest(totp_code(secret, moment + offset * 30), candidate)
               for offset in range(-window, window + 1))


def _mfa_fernet() -> Fernet:
    raw = os.getenv("STORE_AGENT_MFA_KEY") or "development-only-mfa-key"
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    return Fernet(key)


def _recovery_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class AuthError(ValueError):
    """身份、口令、会话相关的可预期错误。"""


class CredentialsError(AuthError):
    """用户名或口令不正确（对外统一措辞，避免暴露账号是否存在）。"""


class AccountLocked(AuthError):
    """连续失败过多，账号被临时锁定。"""


class PasswordPolicyError(AuthError):
    """口令不符合策略。"""


class SessionInvalid(AuthError):
    """会话不存在、已过期或已撤销。"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def redact(text: Any) -> str:
    """对手机号、身份证号做脱敏（保留可核对的前后片段）。"""
    if text is None:
        return ""
    value = str(text)
    value = PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", value)
    value = ID_CARD_RE.sub(lambda m: f"{m.group(1)}********{m.group(2)}", value)
    return value


def redact_payload(payload: Any) -> Any:
    """递归脱敏字典/列表，命中敏感键名的值整体打码。"""
    if isinstance(payload, dict):
        result = {}
        for key, value in payload.items():
            if any(marker in str(key).lower() for marker in SECRET_KEYS):
                result[key] = "***"
            else:
                result[key] = redact_payload(value)
        return result
    if isinstance(payload, (list, tuple)):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, str):
        return redact(payload)
    return payload


def validate_password(password: str, username: str = "") -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"口令至少 {MIN_PASSWORD_LENGTH} 位")
    if password.lower() in WEAK_PASSWORDS:
        raise PasswordPolicyError("口令过于常见，请换一个")
    if username and username.lower() in password.lower():
        raise PasswordPolicyError("口令不能包含用户名")
    if len(set(password)) < 4:
        raise PasswordPolicyError("口令字符过于单一")


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str, str]:
    """返回 (algo, salt_hex, hash_hex)。scrypt 参数写进 algo 便于将来升级。"""
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    algo = "scrypt$16384$8$1"
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt_bytes, n=16384, r=8, p=1, dklen=32)
    return algo, salt_bytes.hex(), digest.hex()


def verify_password(password: str, algo: str, salt_hex: str, expected_hash: str) -> bool:
    try:
        _, n, r, p = algo.split("$")
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected_hash)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def permissions_for(role: str) -> frozenset[str]:
    if role not in ROLE_PERMISSIONS:
        raise AuthError(f"未知角色：{role}")
    return ROLE_PERMISSIONS[role]


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


@dataclass(frozen=True)
class SecurityContext:
    """一次请求的身份与作用域；所有数据访问都必须带上它。"""

    user_id: str
    tenant_id: str
    store_id: str
    role: str
    display_name: str
    session_id: str = ""
    expires_at: str = ""
    permissions: frozenset[str] = field(default_factory=frozenset)
    mfa_enrollment_required: bool = False
    must_change_password: bool = False

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str) -> None:
        if not self.has(permission):
            raise PermissionError(f"当前身份（{self.role_label}）缺少权限：{permission}")

    def to_user_dict(self) -> dict[str, str]:
        """兼容既有 Agent/工具集使用的用户字典形状。"""
        return {
            "user_id": self.user_id,
            "name": self.display_name,
            "role": self.role,
            "role_label": self.role_label,
        }


class AuthStore:
    """身份、成员关系、会话、登录节流与审计的存储层（库与任务库同文件）。"""

    def __init__(self, db_target, session_ttl_hours: int = SESSION_TTL_HOURS, tenant_id: str = ""):
        """`db_target` 可以是 SQLite 文件路径，也可以是 postgresql:// DSN。"""
        self.db = Database(db_target, tenant_id=tenant_id)
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self.session_ttl_hours = session_ttl_hours
        self._init_schema()
        self._migrate()

    def _migrate(self) -> None:
        with self._connection() as conn:
            for table in ("users", "memberships", "auth_sessions", "audit_events", "user_mfa", "mfa_challenges"):
                self.db.ensure_tenant_column(conn, table)
            self.db.add_column_if_missing(conn, "users", "must_change_password", "must_change_password integer not null default 0")
            self.db.add_column_if_missing(conn, "auth_sessions", "absolute_expires_at", "absolute_expires_at text not null default ''")

    def _connection(self):
        """统一走 Database：提交/回滚后必定关闭连接（详见 db.py）。"""
        return self.db.connection()

    def _migrate(self) -> None:
        with self._connection() as conn:
            for table in ("users", "memberships", "auth_sessions", "audit_events"):
                self.db.ensure_tenant_column(conn, table)

    def _init_schema(self) -> None:
        if self.db.dialect == "postgresql":
            with self._connection() as conn:
                self.db.apply_schema(conn)
            return
        with self._connection() as conn:
            conn.executescript(
                """
                create table if not exists users (
                  user_id text primary key,
                  tenant_id text not null,
                  username text not null,
                  display_name text not null,
                  password_algo text not null,
                  password_salt text not null,
                  password_hash text not null,
                  status text not null default 'active',
                  created_at text not null,
                  password_changed_at text not null,
                  must_change_password integer not null default 0,
                  last_login_at text,
                  unique (tenant_id, username)
                );

                create table if not exists memberships (
                  tenant_id text not null,
                  store_id text not null,
                  user_id text not null,
                  role text not null,
                  status text not null default 'active',
                  created_at text not null,
                  primary key (tenant_id, store_id, user_id)
                );
                create index if not exists idx_memberships_user on memberships(user_id, status);

                create table if not exists auth_sessions (
                  token_hash text primary key,
                  user_id text not null,
                  tenant_id text not null,
                  store_id text not null,
                  role text not null,
                  created_at text not null,
                  expires_at text not null,
                  absolute_expires_at text not null,
                  last_seen_at text not null,
                  revoked_at text,
                  revoked_reason text
                );
                create index if not exists idx_auth_sessions_user on auth_sessions(user_id, expires_at);

                create table if not exists login_attempts (
                  scope_key text primary key,
                  fail_count integer not null default 0,
                  first_failed_at text not null,
                  last_failed_at text not null,
                  locked_until text
                );

                create table if not exists audit_events (
                  id integer primary key autoincrement,
                  tenant_id text,
                  store_id text,
                  actor_type text not null,
                  actor_id text not null,
                  event_type text not null,
                  object_type text not null,
                  object_id text not null,
                  before_json text,
                  after_json text,
                  request_id text,
                  created_at text not null
                );
                create index if not exists idx_audit_scope on audit_events(tenant_id, store_id, created_at);
                create index if not exists idx_audit_event on audit_events(event_type, created_at);

                create table if not exists user_mfa (
                  user_id text primary key,
                  tenant_id text not null,
                  secret_enc text not null,
                  recovery_hashes_json text not null default '[]',
                  enabled_at text,
                  last_verified_at text,
                  created_at text not null,
                  updated_at text not null
                );

                create table if not exists mfa_challenges (
                  challenge_hash text primary key,
                  user_id text not null,
                  tenant_id text not null,
                  store_id text not null,
                  role text not null,
                  created_at text not null,
                  expires_at text not null,
                  consumed_at text
                );
                create index if not exists idx_mfa_challenges_user on mfa_challenges(user_id, expires_at);
                """
            )

    # ---------------- 用户与成员 ----------------

    def create_user(
        self,
        username: str,
        password: str,
        display_name: str,
        role: str,
        tenant_id: str,
        store_id: str,
        user_id: Optional[str] = None,
        must_change_password: bool = False,
    ) -> dict[str, Any]:
        username = (username or "").strip()
        if not username:
            raise AuthError("用户名不能为空")
        if role not in ROLES:
            raise AuthError(f"未知角色：{role}（可用：{'、'.join(ROLES)}）")
        validate_password(password, username)
        algo, salt, digest = hash_password(password)
        moment = now_iso()
        user_id = user_id or f"u-{secrets.token_hex(6)}"
        with self._connection() as conn:
            existing = conn.execute(
                "select user_id from users where tenant_id=? and username=?", (tenant_id, username)
            ).fetchone()
            if existing:
                raise AuthError(f"用户已存在：{username}")
            conn.execute(
                """insert into users(user_id,tenant_id,username,display_name,password_algo,password_salt,
                   password_hash,status,created_at,password_changed_at,must_change_password)
                   values(?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, tenant_id, username, display_name, algo, salt, digest, "active", moment, moment,
                 bool(must_change_password) if self.db.dialect == "postgresql" else int(must_change_password)),
            )
            conn.execute(
                """insert into memberships(tenant_id,store_id,user_id,role,status,created_at)
                   values(?,?,?,?,?,?)""",
                (tenant_id, store_id, user_id, role, "active", moment),
            )
        self.record_audit(
            event_type="user_created", actor_type="system", actor_id="auth-init",
            tenant_id=tenant_id, store_id=store_id, object_type="user", object_id=user_id,
            after={"username": username, "role": role},
        )
        return {"user_id": user_id, "username": username, "display_name": display_name,
                "role": role, "tenant_id": tenant_id, "store_id": store_id,
                "must_change_password": bool(must_change_password)}

    def set_password(self, user_id: str, new_password: str, actor_id: str = "system",
                     must_change_password: bool = False) -> None:
        with self._connection() as conn:
            row = conn.execute("select username from users where user_id=?", (user_id,)).fetchone()
            if not row:
                raise AuthError(f"用户不存在：{user_id}")
            validate_password(new_password, row["username"])
            algo, salt, digest = hash_password(new_password)
            conn.execute(
                """update users set password_algo=?, password_salt=?, password_hash=?, password_changed_at=?,
                   must_change_password=? where user_id=?""",
                (algo, salt, digest, now_iso(),
                 bool(must_change_password) if self.db.dialect == "postgresql" else int(must_change_password),
                 user_id),
            )
            conn.execute(
                "update auth_sessions set revoked_at=?, revoked_reason=? where user_id=? and revoked_at is null",
                (now_iso(), "password_changed", user_id),
            )
        self.record_audit(event_type="password_changed", actor_type="user", actor_id=actor_id,
                          object_type="user", object_id=user_id)

    def change_password(self, user_id: str, current_password: str, new_password: str) -> None:
        with self._connection() as conn:
            row = conn.execute(
                "select username,password_algo,password_salt,password_hash from users where user_id=?",
                (user_id,),
            ).fetchone()
        if not row or not verify_password(current_password, row["password_algo"], row["password_salt"],
                                         row["password_hash"]):
            raise CredentialsError("当前口令不正确")
        validate_password(new_password, row["username"])
        self.set_password(user_id, new_password, actor_id=user_id, must_change_password=False)

    def list_users(self, tenant_id: str, store_id: Optional[str] = None) -> list[dict[str, Any]]:
        sql = """select u.user_id,u.username,u.display_name,u.status,u.last_login_at,
                        u.must_change_password,m.store_id,m.role,m.status as membership_status
                 from users u join memberships m on m.user_id=u.user_id
                 where u.tenant_id=?"""
        params: list[Any] = [tenant_id]
        if store_id:
            sql += " and m.store_id=?"
            params.append(store_id)
        sql += " order by u.username"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def set_user_status(self, user_id: str, status: str, actor_id: str = "system") -> dict[str, Any]:
        if status not in {"active", "disabled"}:
            raise AuthError("用户状态只能是 active 或 disabled")
        with self._connection() as conn:
            row = conn.execute("select user_id,status from users where user_id=?", (user_id,)).fetchone()
            if not row:
                raise AuthError(f"用户不存在：{user_id}")
            conn.execute("update users set status=? where user_id=?", (status, user_id))
            if status != "active":
                conn.execute(
                    "update auth_sessions set revoked_at=?, revoked_reason='user_disabled' "
                    "where user_id=? and revoked_at is null",
                    (now_iso(), user_id),
                )
        self.record_audit(event_type="user_status_changed", actor_type="user", actor_id=actor_id,
                          object_type="user", object_id=user_id, after={"status": status})
        return {"user_id": user_id, "status": status}

    def update_membership(self, tenant_id: str, store_id: str, user_id: str, *, role: Optional[str] = None,
                          display_name: Optional[str] = None, status: Optional[str] = None,
                          actor_id: str = "system") -> dict[str, Any]:
        if role is not None and role not in ROLES:
            raise AuthError(f"未知角色：{role}")
        if status is not None and status not in {"active", "disabled"}:
            raise AuthError("成员状态只能是 active 或 disabled")
        with self._connection() as conn:
            row = conn.execute(
                "select u.user_id,u.display_name,m.role,m.status from users u join memberships m "
                "on m.user_id=u.user_id where u.user_id=? and u.tenant_id=? and m.store_id=?",
                (user_id, tenant_id, store_id),
            ).fetchone()
            if not row:
                raise AuthError(f"成员不存在：{user_id}")
            before = dict(row)
            if display_name is not None:
                conn.execute("update users set display_name=? where user_id=?", (display_name, user_id))
            if role is not None or status is not None:
                conn.execute(
                    "update memberships set role=coalesce(?,role),status=coalesce(?,status) "
                    "where tenant_id=? and store_id=? and user_id=?",
                    (role, status, tenant_id, store_id, user_id),
                )
            if status == "disabled":
                conn.execute(
                    "update auth_sessions set revoked_at=?, revoked_reason='membership_disabled' "
                    "where user_id=? and revoked_at is null",
                    (now_iso(), user_id),
                )
            updated = conn.execute(
                "select u.user_id,u.username,u.display_name,u.status,m.role,m.status as membership_status "
                "from users u join memberships m on m.user_id=u.user_id "
                "where u.user_id=? and u.tenant_id=? and m.store_id=?",
                (user_id, tenant_id, store_id),
            ).fetchone()
        self.record_audit(event_type="membership_updated", actor_type="user", actor_id=actor_id,
                          tenant_id=tenant_id, store_id=store_id, object_type="user", object_id=user_id,
                          before=before, after=dict(updated))
        return dict(updated)

    def count_active_owners(self, tenant_id: str, store_id: str) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "select count(*) as n from users u join memberships m on m.user_id=u.user_id "
                "where u.tenant_id=? and m.store_id=? and m.role='owner' "
                "and u.status='active' and m.status='active'",
                (tenant_id, store_id),
            ).fetchone()
        return int(row["n"])

    def _membership(self, username: str, store_id: Optional[str]) -> dict[str, Any]:
        with self._connection() as conn:
            rows = conn.execute(
                """select u.user_id,u.username,u.display_name,u.status,u.tenant_id,
                          m.store_id,m.role,m.status as membership_status
                   from users u join memberships m on m.user_id=u.user_id
                   where u.username=? and m.status='active'""",
                (username,),
            ).fetchall()
        active = [row for row in rows if row["status"] == "active"]
        if not active:
            raise CredentialsError("用户名或口令不正确")
        if store_id:
            matched = [row for row in active if row["store_id"] == store_id]
            if not matched:
                raise CredentialsError(f"该账号没有门店 {store_id} 的成员关系")
            return dict(matched[0])
        if len(active) > 1:
            raise AuthError("该账号属于多个门店，请指定 store_id：" + "、".join(sorted(r["store_id"] for r in active)))
        return dict(active[0])

    # ---------------- 登录节流 ----------------

    def _check_throttle(self, scope_key: str) -> None:
        with self._connection() as conn:
            row = conn.execute("select * from login_attempts where scope_key=?", (scope_key,)).fetchone()
        if row and row["locked_until"] and row["locked_until"] > now_iso():
            raise AccountLocked(f"登录失败次数过多，请于 {row['locked_until']} 之后再试")

    def _record_failure(self, scope_key: str) -> None:
        moment = now_iso()
        with self._connection() as conn:
            row = conn.execute("select * from login_attempts where scope_key=?", (scope_key,)).fetchone()
            if not row:
                conn.execute(
                    """insert into login_attempts(scope_key,fail_count,first_failed_at,last_failed_at,locked_until)
                       values(?,?,?,?,?)""",
                    (scope_key, 1, moment, moment, None),
                )
                return
            count = int(row["fail_count"]) + 1
            locked_until = None
            if count >= MAX_LOGIN_FAILURES:
                locked_until = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
            conn.execute(
                "update login_attempts set fail_count=?, last_failed_at=?, locked_until=? where scope_key=?",
                (count, moment, locked_until, scope_key),
            )

    def _reset_failures(self, scope_key: str) -> None:
        with self._connection() as conn:
            conn.execute("delete from login_attempts where scope_key=?", (scope_key,))

    def lock_status(self, username: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from login_attempts where scope_key=?", (username,)).fetchone()
        if not row:
            return {"username": username, "fail_count": 0, "locked_until": None}
        return {"username": username, "fail_count": int(row["fail_count"]), "locked_until": row["locked_until"]}

    # ---------------- MFA ----------------

    def mfa_enforced(self) -> bool:
        return os.getenv("STORE_AGENT_MFA_ENFORCE", "").strip().lower() in {"1", "true", "yes", "on"}

    def mfa_required_for(self, role: str) -> bool:
        return self.mfa_enforced() and role in MFA_ROLES

    def is_mfa_enabled(self, user_id: str) -> bool:
        with self._connection() as conn:
            row = conn.execute("select enabled_at from user_mfa where user_id=?", (user_id,)).fetchone()
        return bool(row and row["enabled_at"])

    def begin_mfa_enrollment(self, user_id: str) -> dict[str, str]:
        secret = generate_totp_secret()
        encrypted = _mfa_fernet().encrypt(secret.encode("utf-8")).decode("ascii")
        moment = now_iso()
        with self._connection() as conn:
            row = conn.execute("select tenant_id from users where user_id=?", (user_id,)).fetchone()
            if not row:
                raise AuthError("用户不存在")
            conn.execute(
                """insert into user_mfa(user_id,tenant_id,secret_enc,recovery_hashes_json,enabled_at,last_verified_at,created_at,updated_at)
                   values(?,?,?,?,null,null,?,?)
                   on conflict(user_id) do update set secret_enc=excluded.secret_enc,
                     recovery_hashes_json='[]',enabled_at=null,updated_at=excluded.updated_at""",
                (user_id, row["tenant_id"], encrypted, "[]", moment, moment),
            )
        label = "门店运营系统"
        uri = f"otpauth://totp/{label}:{user_id}?secret={secret}&issuer={label}&digits=6&period=30"
        return {"secret": secret, "otpauth_uri": uri}

    def confirm_mfa_enrollment(self, user_id: str, code: str) -> list[str]:
        with self._connection() as conn:
            row = conn.execute("select secret_enc from user_mfa where user_id=?", (user_id,)).fetchone()
        if not row:
            raise AuthError("尚未开始 MFA 绑定")
        try:
            secret = _mfa_fernet().decrypt(row["secret_enc"].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise AuthError("MFA 密钥无法解密，请重新绑定") from exc
        if not verify_totp(secret, code):
            raise CredentialsError("验证码不正确")
        recovery = [secrets.token_hex(2) + "-" + secrets.token_hex(2) + "-" + secrets.token_hex(2) for _ in range(8)]
        hashes = [_recovery_hash(item) for item in recovery]
        with self._connection() as conn:
            conn.execute(
                "update user_mfa set recovery_hashes_json=?, enabled_at=?, updated_at=? where user_id=?",
                (json.dumps(hashes), now_iso(), now_iso(), user_id),
            )
        return recovery

    def verify_mfa(self, user_id: str, code: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "select secret_enc,recovery_hashes_json,enabled_at from user_mfa where user_id=?", (user_id,)
            ).fetchone()
        if not row or not row["enabled_at"]:
            return False
        try:
            secret = _mfa_fernet().decrypt(row["secret_enc"].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return False
        if verify_totp(secret, code):
            with self._connection() as conn:
                conn.execute("update user_mfa set last_verified_at=? where user_id=?", (now_iso(), user_id))
            return True
        candidate_hash = _recovery_hash((code or "").strip())
        hashes = json.loads(row["recovery_hashes_json"] or "[]")
        remaining = [item for item in hashes if not hmac.compare_digest(item, candidate_hash)]
        if len(remaining) != len(hashes):
            with self._connection() as conn:
                conn.execute("update user_mfa set recovery_hashes_json=?,last_verified_at=?,updated_at=? where user_id=?",
                             (json.dumps(remaining), now_iso(), now_iso(), user_id))
            return True
        return False

    def reset_mfa(self, user_id: str) -> None:
        with self._connection() as conn:
            conn.execute("delete from user_mfa where user_id=?", (user_id,))

    def _create_mfa_challenge(self, row: Any, membership: dict[str, Any]) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        moment = datetime.now()
        expires = moment + timedelta(minutes=MFA_CHALLENGE_MINUTES)
        with self._connection() as conn:
            conn.execute(
                """insert into mfa_challenges(challenge_hash,user_id,tenant_id,store_id,role,created_at,expires_at)
                   values(?,?,?,?,?,?,?)""",
                (hash_token(token), row["user_id"], membership["tenant_id"], membership["store_id"],
                 membership["role"], moment.isoformat(timespec="seconds"),
                 expires.isoformat(timespec="seconds")),
            )
        return {"mfa_required": True, "mfa_token": token, "expires_at": expires.isoformat(timespec="seconds")}

    def complete_mfa(self, mfa_token: str, code: str, actor_ip: str = "") -> dict[str, Any]:
        challenge_hash = hash_token(mfa_token or "")
        moment = datetime.now()
        with self._connection() as conn:
            challenge = conn.execute(
                "select * from mfa_challenges where challenge_hash=?", (challenge_hash,)
            ).fetchone()
            if not challenge or challenge["consumed_at"] or challenge["expires_at"] <= moment.isoformat(timespec="seconds"):
                raise SessionInvalid("MFA 验证已过期，请重新登录")
            user = conn.execute("select * from users where user_id=?", (challenge["user_id"],)).fetchone()
            membership = conn.execute(
                "select * from memberships where user_id=? and store_id=? and status='active'",
                (challenge["user_id"], challenge["store_id"]),
            ).fetchone()
        if not user or not membership or not self.verify_mfa(challenge["user_id"], code):
            raise CredentialsError("验证码不正确")
        with self._connection() as conn:
            conn.execute("update mfa_challenges set consumed_at=? where challenge_hash=?",
                         (now_iso(), challenge_hash))
        return self._issue_session(user, dict(membership), actor_ip=actor_ip)

    def _issue_session(self, row: Any, membership: dict[str, Any], actor_ip: str = "") -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        moment = datetime.now()
        expires = moment + timedelta(hours=self.session_ttl_hours)
        absolute_expires = moment + timedelta(days=ABSOLUTE_SESSION_TTL_DAYS)
        with self._connection() as conn:
            conn.execute(
                """insert into auth_sessions(token_hash,user_id,tenant_id,store_id,role,created_at,
                   expires_at,absolute_expires_at,last_seen_at) values(?,?,?,?,?,?,?,?,?)""",
                (hash_token(token), row["user_id"], membership["tenant_id"], membership["store_id"],
                 membership["role"], moment.isoformat(timespec="seconds"),
                 expires.isoformat(timespec="seconds"), absolute_expires.isoformat(timespec="seconds"),
                 moment.isoformat(timespec="seconds")),
            )
            conn.execute("update users set last_login_at=? where user_id=?",
                         (moment.isoformat(timespec="seconds"), row["user_id"]))
        context = SecurityContext(
            user_id=row["user_id"], tenant_id=membership["tenant_id"], store_id=membership["store_id"],
            role=membership["role"], display_name=row["display_name"],
            session_id=hash_token(token)[:12], expires_at=expires.isoformat(timespec="seconds"),
            permissions=permissions_for(membership["role"]),
            mfa_enrollment_required=self.mfa_required_for(membership["role"]) and not self.is_mfa_enabled(row["user_id"]),
            must_change_password=bool(row["must_change_password"]),
        )
        self.record_audit(event_type="login_success", actor_type="user", actor_id=row["user_id"],
                          tenant_id=membership["tenant_id"], store_id=membership["store_id"],
                          object_type="session", object_id=context.session_id, after={"ip": actor_ip})
        return {"token": token, "expires_at": context.expires_at, "context": context, "mfa_required": False}

    # ---------------- 会话 ----------------

    def login(
        self,
        username: str,
        password: str,
        store_id: Optional[str] = None,
        actor_ip: str = "",
    ) -> dict[str, Any]:
        username = (username or "").strip()
        try:
            self._check_throttle(username)
        except AccountLocked as exc:
            self.record_audit(event_type="login_locked", actor_type="anonymous", actor_id=username,
                              object_type="session", object_id=username, after={"reason": str(exc)})
            raise
        membership: Optional[dict[str, Any]] = None
        try:
            membership = self._membership(username, store_id)
            with self._connection() as conn:
                row = conn.execute("select * from users where user_id=?", (membership["user_id"],)).fetchone()
            if not row or not verify_password(password, row["password_algo"], row["password_salt"], row["password_hash"]):
                raise CredentialsError("用户名或口令不正确")
        except CredentialsError:
            self._record_failure(username)
            # 已知成员关系时记录租户/门店，门店主才能审计到自己门店的失败登录；
            # 未知用户名不泄露任何归属信息，只记 NULL。
            self.record_audit(
                event_type="login_failed", actor_type="anonymous", actor_id=username,
                tenant_id=(membership or {}).get("tenant_id"),
                store_id=(membership or {}).get("store_id"),
                object_type="session", object_id=username, after={"ip": actor_ip},
            )
            raise

        self._reset_failures(username)
        if self.mfa_required_for(membership["role"]) and self.is_mfa_enabled(row["user_id"]):
            return self._create_mfa_challenge(row, membership)
        return self._issue_session(row, membership, actor_ip=actor_ip)

    def resolve(self, token: str) -> SecurityContext:
        """校验令牌并返回上下文；过期/撤销/未知一律 SessionInvalid。"""
        if not token:
            raise SessionInvalid("缺少会话令牌")
        token_hash = hash_token(token)
        moment = datetime.now()
        with self._connection() as conn:
            row = conn.execute("select * from auth_sessions where token_hash=?", (token_hash,)).fetchone()
            if not row or row["revoked_at"]:
                raise SessionInvalid("会话无效或已退出")
            if row["expires_at"] <= moment.isoformat(timespec="seconds"):
                raise SessionInvalid("会话已过期，请重新登录")
            absolute_raw = row["absolute_expires_at"] if "absolute_expires_at" in row.keys() else ""
            absolute = datetime.fromisoformat(absolute_raw) if absolute_raw else datetime.fromisoformat(row["expires_at"])
            if absolute <= moment:
                raise SessionInvalid("会话已达最长有效期，请重新登录")
            # 滑动续期：剩余不足一半时延长，但绝不能超过绝对有效期
            expires = datetime.fromisoformat(row["expires_at"])
            ttl = timedelta(hours=self.session_ttl_hours)
            if expires - moment < ttl / 2:
                expires = min(moment + ttl, absolute)
                conn.execute(
                    "update auth_sessions set expires_at=?, last_seen_at=? where token_hash=?",
                    (expires.isoformat(timespec="seconds"), moment.isoformat(timespec="seconds"), token_hash),
                )
            else:
                conn.execute("update auth_sessions set last_seen_at=? where token_hash=?",
                             (moment.isoformat(timespec="seconds"), token_hash))
            user = conn.execute(
                "select status,display_name,must_change_password from users where user_id=?",
                (row["user_id"],),
            ).fetchone()
        if not user or user["status"] != "active":
            raise SessionInvalid("账号已被停用")
        return SecurityContext(
            user_id=row["user_id"], tenant_id=row["tenant_id"], store_id=row["store_id"], role=row["role"],
            display_name=user["display_name"], session_id=token_hash[:12],
            expires_at=expires.isoformat(timespec="seconds"), permissions=permissions_for(row["role"]),
            mfa_enrollment_required=self.mfa_required_for(row["role"]) and not self.is_mfa_enabled(row["user_id"]),
            must_change_password=bool(user["must_change_password"]),
        )

    def logout(self, token: str, reason: str = "user_logout") -> bool:
        token_hash = hash_token(token) if token else ""
        with self._connection() as conn:
            cursor = conn.execute(
                "update auth_sessions set revoked_at=?, revoked_reason=? where token_hash=? and revoked_at is null",
                (now_iso(), reason, token_hash),
            )
        revoked = cursor.rowcount > 0
        if revoked:
            self.record_audit(event_type="logout", actor_type="user", actor_id=token_hash[:12],
                              object_type="session", object_id=token_hash[:12], after={"reason": reason})
        return revoked

    def revoke_expired(self, moment: Optional[str] = None) -> int:
        moment = moment or now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                "update auth_sessions set revoked_at=?, revoked_reason='expired' where expires_at < ? and revoked_at is null",
                (moment, moment),
            )
        return cursor.rowcount

    def active_sessions(self, user_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """select token_hash,store_id,role,created_at,expires_at,last_seen_at from auth_sessions
                   where user_id=? and revoked_at is null and expires_at > ? order by created_at desc""",
                (user_id, now_iso()),
            ).fetchall()
        return [{**dict(row), "session_id": row["token_hash"][:12], "token_hash": None} for row in rows]

    # ---------------- 审计 ----------------

    def record_audit(
        self,
        event_type: str,
        actor_type: str,
        actor_id: str,
        object_type: str,
        object_id: str,
        tenant_id: Optional[str] = None,
        store_id: Optional[str] = None,
        before: Any = None,
        after: Any = None,
        request_id: Optional[str] = None,
    ) -> int:
        with self._connection() as conn:
            return self.db.insert_returning_id(
                conn,
                """insert into audit_events(tenant_id,store_id,actor_type,actor_id,event_type,object_type,
                   object_id,before_json,after_json,request_id,created_at)
                   values(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tenant_id, store_id, actor_type, redact(actor_id), event_type, object_type, redact(object_id),
                    json.dumps(redact_payload(before), ensure_ascii=False) if before is not None else None,
                    json.dumps(redact_payload(after), ensure_ascii=False) if after is not None else None,
                    request_id, now_iso(),
                ),
            )

    def list_audit(
        self,
        tenant_id: Optional[str] = None,
        store_id: Optional[str] = None,
        limit: int = 100,
        event_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        sql = "select * from audit_events where 1=1"
        params: list[Any] = []
        if tenant_id:
            sql += " and tenant_id=?"
            params.append(tenant_id)
        if store_id:
            sql += " and store_id=?"
            params.append(store_id)
        if event_type:
            sql += " and event_type=?"
            params.append(event_type)
        sql += " order by id desc limit ?"
        params.append(max(1, min(limit, 500)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
