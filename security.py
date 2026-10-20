#!/usr/bin/env python3
"""安全闸门支撑（P0 第 4/5/7 项）：Webhook 验签与重放防护、凭据轮换流程、危险能力自检。

三块：

* **Webhook 验签**：HMAC-SHA256 + 时间窗 + nonce 去重（防重放）。对外回调（例如飞书卡片审批）
  接入时直接用 `verify_inbound()`；nonce 落在 `webhook_nonces` 表里，重复请求会被拒绝。
* **凭据轮换流程**：`rotation_checklist()` 给出可执行步骤与验证命令；`rotate_pg_role_password()`
  是真能跑的一步（ALTER ROLE），轮换后必须验证新口令可连、旧口令不可连。
* **自检（给闸门做证据）**：扫描运行时模块里是否出现 shell 调用/任意删除（第 5 项"危险能力关闭"）、
  仓库里是否有明文密钥（第 4 项），以及（给了 DSN 时）PostgreSQL 是否开了 RLS、连接角色是否是所有者。

注意：扫描只看**运行时模块**（排除 test_*.py 与 scripts/），因为测试里调用 subprocess 是正常行为。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from db import Database

SIGNATURE_TOLERANCE_SECONDS = 300
NONCE_RETENTION_HOURS = 24

NONCE_DDL = """
create table if not exists webhook_nonces (
  nonce text primary key,
  source text not null default 'unknown',
  received_at text not null
);
create index if not exists idx_webhook_nonces_time on webhook_nonces(received_at);
"""

# 危险能力：运行时模块里出现这些就说明"危险能力没有被关闭"
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bsubprocess\.(run|Popen|call|check_output)\b", "调用外部进程"),
    (r"\bos\.system\b", "执行 shell 命令"),
    (r"\bos\.popen\b", "执行 shell 管道"),
    (r"\beval\s*\(", "动态执行代码（eval 类）"),
    (r"\bexec\s*\(", "动态执行代码（exec 类）"),
    (r"\bshutil\.rmtree\b", "递归删除目录"),
    (r"\bos\.remove\b|\bos\.unlink\b", "删除文件（os 接口）"),
    (r"\bPath\([^)]*\)\.unlink\b", "删除文件（Path 接口）"),
)

# 明文密钥特征：只做"高置信度"匹配，避免把普通字符串当密钥
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"sk-[A-Za-z0-9]{16,}", "疑似 OpenAI/DeepSeek 密钥"),
    (r"AKIA[0-9A-Z]{16}", "疑似 AWS Access Key"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "私钥内容"),
    (r"(?i)\b(password|passwd|secret|api_key|apikey|token)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]",
     "疑似硬编码口令/密钥（非空值）"),
)
SKIP_DIRS = {".git", "__pycache__", "data", "node_modules", ".venv"}
RUNTIME_SKIP_PREFIXES = ("test_",)
# 行内显式豁免：确实需要且已评审的能力，在那一行写 `# noqa: dangerous` / `# noqa: secret`
NOQA_DANGEROUS = "noqa: dangerous"
NOQA_SECRET = "noqa: secret"
# 已知的测试夹具口令（不是真实凭据）；真实密钥不允许进白名单
ALLOWED_SECRET_VALUES = {
    "ShiZiZuo-2026-store",      # 测试夹具口令
    "Brand-New-Pass-2026",
    "rls-probe-pass",
    "probe-pass",
    "not-a-real-token",         # 测试里故意用的假令牌
}


class WebhookVerificationError(ValueError):
    """验签失败（对外统一措辞，不暴露具体哪一步失败）。"""


# ---------------- 验签与重放防护 ----------------

def sign_webhook(secret: str, body: bytes, timestamp: Optional[int] = None) -> dict[str, Any]:
    """按 `HMAC-SHA256(secret, f"{timestamp}.{body}")` 生成签名（与飞书/企微回调同思路）。"""
    moment = int(timestamp if timestamp is not None else time.time())
    message = f"{moment}.".encode("utf-8") + body
    signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return {"timestamp": moment, "signature": signature}


def verify_signature(secret: str, body: bytes, signature: str, timestamp: Any,
                     now: Optional[int] = None, tolerance: int = SIGNATURE_TOLERANCE_SECONDS) -> dict[str, Any]:
    """校验签名与时间窗；不落库、不消费 nonce（给纯函数场景用）。"""
    try:
        moment = int(timestamp)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "timestamp 非法"}
    current = int(now if now is not None else time.time())
    if abs(current - moment) > tolerance:
        return {"ok": False, "reason": f"时间戳超出容忍窗口（±{tolerance}s）"}
    expected = sign_webhook(secret, body, moment)["signature"]
    if not hmac.compare_digest(expected, str(signature or "")):
        return {"ok": False, "reason": "签名不匹配"}
    return {"ok": True, "reason": "ok", "timestamp": moment}


class ReplayGuard:
    """nonce 去重：同一 nonce 只允许成功消费一次（防重放）。"""

    def __init__(self, db_target: Any, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)
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
            conn.executescript(NONCE_DDL)

    def consume(self, nonce: str, source: str = "unknown") -> bool:
        """占用一个 nonce；已被占用返回 False（调用方据此判定重放）。"""
        if not nonce:
            return False
        now = datetime.now().isoformat(timespec="seconds")
        with self._connection() as conn:
            existing = conn.execute("select nonce from webhook_nonces where nonce=?", (nonce,)).fetchone()
            if existing:
                return False
            conn.execute("insert into webhook_nonces(nonce,source,received_at) values(?,?,?)",
                         (nonce, source, now))
        return True

    def prune(self, hours: int = NONCE_RETENTION_HOURS) -> int:
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._connection() as conn:
            cursor = conn.execute("delete from webhook_nonces where received_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def count(self) -> int:
        with self._connection() as conn:
            return int(conn.execute("select count(*) as c from webhook_nonces").fetchone()["c"])


def verify_inbound(secret: str, body: bytes, signature: str, timestamp: Any, nonce: str,
                   guard: ReplayGuard, now: Optional[int] = None,
                   tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
                   source: str = "unknown") -> dict[str, Any]:
    """一次完整的入站校验：签名 → 时间窗 → nonce 去重（顺序固定，失败一律拒绝）。"""
    result = verify_signature(secret, body, signature, timestamp, now=now, tolerance=tolerance)
    if not result["ok"]:
        return result
    if not guard.consume(nonce, source):
        return {"ok": False, "reason": "nonce 已使用（疑似重放）"}
    return {"ok": True, "reason": "ok", "timestamp": result["timestamp"]}


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


# ---------------- 凭据轮换 ----------------

def rotation_checklist() -> list[dict[str, Any]]:
    """凭据轮换流程：每一步都给出可执行的验证方式，避免"换了但没验"。"""
    return [
        {"target": "PostgreSQL 应用角色口令",
         "action": "ALTER ROLE store_app PASSWORD '<新口令>'（或调用 rotate_pg_role_password()）",
         "verify": "用新口令能连上并只看到自己租户的行；旧口令连接失败",
         "automated": True},
        {"target": "大模型 API Key",
         "action": "在服务商后台新建 Key → 更新部署环境变量 STORE_AGENT_LLM_API_KEY → 重启服务",
         "verify": "/api/bootstrap 的 llm_mode 不为 off；旧 Key 在服务商后台禁用后调用应失败",
         "automated": False},
        {"target": "飞书/企微 Webhook 地址或签名密钥",
         "action": "在群机器人设置里重置 → 更新 FEISHU_WEBHOOK_URL / WECOM_WEBHOOK_URL（与签名密钥）",
         "verify": "store_cli.py notify --kind daily --force 能成功发送；旧地址返回失败",
         "automated": False},
        {"target": "门店成员口令",
         "action": "store_cli.py auth-passwd --username <账号>",
         "verify": "该用户全部旧会话立即失效（重新登录才行）",
         "automated": True},
        {"target": "备份与轮换记录",
         "action": "轮换后立刻执行 store_cli.py backup，并在审计里留下记录",
         "verify": "store_cli.py drill 演练通过；audit 里能看到轮换时间",
         "automated": True},
    ]


def rotate_pg_role_password(dsn: str, role: str, new_password: str) -> dict[str, Any]:
    """轮换 PostgreSQL 角色口令（真执行），返回验证提示。"""
    import psycopg

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", role):
        raise ValueError(f"角色名不合法：{role}")
    if len(new_password) < 10:
        raise ValueError("新口令至少 10 位")
    from psycopg import sql

    # ALTER ROLE ... PASSWORD 不接受占位符（PostgreSQL 限制），用 sql.Identifier/Literal 安全拼接
    statement = sql.SQL("alter role {} with password {}").format(
        sql.Identifier(role), sql.Literal(new_password))
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(statement)
    return {"role": role, "rotated_at": datetime.now().isoformat(timespec="seconds"),
            "verify": f"用新口令连接 {role}；并确认旧口令已无法连接"}


# ---------------- 自检（给闸门做证据） ----------------

def _iter_files(root: Path, patterns: tuple[str, ...], include_tests: bool) -> list[Path]:
    files: list[Path] = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.suffix not in patterns:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not include_tests and any(path.name.startswith(prefix) for prefix in RUNTIME_SKIP_PREFIXES):
            continue
        files.append(path)
    return files


def scan_dangerous_capabilities(root: Path) -> dict[str, Any]:
    """第 5 项：运行时模块里不得出现 shell 调用、任意文件删除、动态执行。"""
    findings: list[dict[str, Any]] = []
    for path in _iter_files(root, (".py",), include_tests=False):
        if "scripts" in path.relative_to(root).parts:
            continue  # reviewed operational scripts are excluded from dangerous capability scan
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern, label in DANGEROUS_PATTERNS:
            for match in re.finditer(pattern, text):
                if NOQA_DANGEROUS in text.splitlines()[text[:match.start()].count("\n")]:
                    continue
                line = text[:match.start()].count("\n") + 1
                findings.append({"file": str(path.relative_to(root)), "line": line,
                                 "label": label, "snippet": match.group(0)})
    return {"ok": not findings, "findings": findings, "scanned": len(_iter_files(root, (".py",), include_tests=False))}


def scan_secrets(root: Path) -> dict[str, Any]:
    """第 4 项：仓库里不得有明文密钥（测试文件同样属于仓库，因此一起扫）。"""
    findings: list[dict[str, Any]] = []
    for path in _iter_files(root, (".py", ".json", ".ps1", ".example", ".env", ".toml", ".md"), include_tests=True):
        if path.name in ("package-lock.json",):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern, label in SECRET_PATTERNS:
            for match in re.finditer(pattern, text):
                line_no = text[:match.start()].count("\n") + 1
                line_text = text.splitlines()[line_no - 1]
                if NOQA_SECRET in line_text:
                    continue
                snippet = match.group(0)
                if re.search(r'''[:=]\s*['"]\s*['"]''', snippet):      # 空值不算泄露
                    continue
                if any(allowed in snippet for allowed in ALLOWED_SECRET_VALUES):
                    continue
                findings.append({"file": str(path.relative_to(root)), "line": line_no, "label": label,
                                 "snippet": snippet[:40] + ("…" if len(snippet) > 40 else "")})
    return {"ok": not findings, "findings": findings}


def check_postgres_hardening(dsn: str) -> dict[str, Any]:
    """确认 PostgreSQL 侧：业务连接不是表所有者、且 RLS 已开启。"""
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("select current_user, usesuper from pg_user where usename = current_user")
        role, is_super = cur.fetchone()
        cur.execute("select tableowner from pg_tables where schemaname='public' limit 1")
        owner_row = cur.fetchone()
        # 只统计"带 tenant_id 的表"：全局表（login_attempts / webhook_nonces）本就不做租户隔离
        cur.execute(
            """select count(*) from pg_tables t
               where t.schemaname='public' and t.rowsecurity = true
                 and exists (select 1 from pg_attribute a
                             where a.attrelid = (quote_ident(t.schemaname)||'.'||quote_ident(t.tablename))::regclass
                               and a.attname = 'tenant_id' and not a.attisdropped)"""
        )
        rls_tables = int(cur.fetchone()[0])
        cur.execute(
            """select count(*) from pg_tables t
               where t.schemaname='public'
                 and exists (select 1 from pg_attribute a
                             where a.attrelid = (quote_ident(t.schemaname)||'.'||quote_ident(t.tablename))::regclass
                               and a.attname = 'tenant_id' and not a.attisdropped)"""
        )
        total = int(cur.fetchone()[0])
    problems: list[str] = []
    if is_super or (owner_row and owner_row[0] == role):
        problems.append(f"业务连接用的是所有者/超级用户角色 {role}：RLS 会被绕过，请改用应用角色")
    if total and rls_tables < total:
        problems.append(f"只有 {rls_tables}/{total} 张表开启了 RLS")
    return {"ok": not problems, "role": role, "rls_tables": rls_tables, "public_tables": total,
            "problems": problems}


def check_retention_policy(config_path: Path) -> dict[str, Any]:
    """确认门店配置里有明确的保留期（P0 第 6 项：数据最小化要有期限）。"""
    from retention import policy_from_config

    problems: list[str] = []
    try:
        import json

        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001 - 配置问题也要出报告
        return {"ok": False, "problems": [f"无法读取门店配置：{exc}"]}
    if "retention" not in config:
        problems.append("门店配置里没有 retention 段：各数据类型的保留期未明确")
    try:
        policy = policy_from_config(config)
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "problems": [f"保留策略不合法：{exc}"]}
    if policy.get("audit_events", 0) <= 0:
        problems.append("审计日志被设为永久保留或未配置清理：需要明确期限")
    finite = {table: days for table, days in policy.items() if days > 0}
    return {"ok": not problems, "problems": problems, "configured_tables": len(policy),
            "tables_with_expiry": len(finite),
            "forever_tables": sorted(table for table, days in policy.items() if days <= 0)}


def _env_value(env: dict[str, str], key: str) -> str:
    return str(env.get(key) or "").strip()


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        not lowered
        or lowered.startswith(("replace-", "change-me", "changeme", "example-", "<"))
        or "replace-with" in lowered
        or "development-only" in lowered
    )


def check_integration_config(env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """校验显式选择的生产集成；mock/outbox/local 默认无需密钥。"""
    values = dict(os.environ if env is None else env)
    problems: list[str] = []

    provider = (_env_value(values, "STORE_AGENT_APPROVAL_CALLBACK_PROVIDER") or "mock").lower()
    if provider not in {"mock", "feishu", "wecom"}:
        problems.append(f"不支持的 STORE_AGENT_APPROVAL_CALLBACK_PROVIDER：{provider}")
    elif provider == "feishu":
        if not _env_value(values, "FEISHU_VERIFICATION_TOKEN"):
            problems.append("审批回调 provider=feishu 缺少 FEISHU_VERIFICATION_TOKEN")
        if not _env_value(values, "FEISHU_ENCRYPT_KEY"):
            problems.append("审批回调 provider=feishu 缺少 FEISHU_ENCRYPT_KEY")
    elif provider == "wecom":
        if not _env_value(values, "WECOM_CALLBACK_TOKEN"):
            problems.append("审批回调 provider=wecom 缺少 WECOM_CALLBACK_TOKEN")
        if not _env_value(values, "WECOM_ENCODING_AES_KEY"):
            problems.append("审批回调 provider=wecom 缺少 WECOM_ENCODING_AES_KEY")
        if not _env_value(values, "WECOM_CORP_ID"):
            problems.append("审批回调 provider=wecom 缺少 WECOM_CORP_ID")

    alert_channel = (_env_value(values, "STORE_AGENT_ALERT_CHANNEL") or "outbox").lower()
    if alert_channel not in {"outbox", "feishu", "wecom"}:
        problems.append(f"不支持的 STORE_AGENT_ALERT_CHANNEL：{alert_channel}")
    elif alert_channel == "feishu":
        if not (_env_value(values, "FEISHU_WEBHOOK_URL")
                or _env_value(values, "STORE_AGENT_FEISHU_WEBHOOK_URL")):
            problems.append("告警渠道 feishu 缺少 FEISHU_WEBHOOK_URL")
    elif alert_channel == "wecom":
        if not (_env_value(values, "WECOM_WEBHOOK_URL")
                or _env_value(values, "STORE_AGENT_WECOM_WEBHOOK_URL")):
            problems.append("告警渠道 wecom 缺少 WECOM_WEBHOOK_URL")

    production = (_env_value(values, "STORE_AGENT_ENV") or "development").lower() in {"production", "prod"}
    mfa_enforced = (_env_value(values, "STORE_AGENT_MFA_ENFORCE") or "").lower() in {"1", "true", "yes", "on"}
    if production and mfa_enforced:
        mfa_key = _env_value(values, "STORE_AGENT_MFA_KEY")
        if _looks_like_placeholder(mfa_key) or len(mfa_key) < 32:
            problems.append("生产环境启用 MFA 时必须配置至少 32 位的非占位符 STORE_AGENT_MFA_KEY")

    if production:
        database_password = _env_value(values, "POSTGRES_PASSWORD")
        if _looks_like_placeholder(database_password) or len(database_password) < 16:
            problems.append("生产环境必须配置非占位符且至少 16 位的 POSTGRES_PASSWORD")

        domain = _env_value(values, "DOMAIN").lower()
        if _looks_like_placeholder(domain) or "." not in domain or "example" in domain:
            problems.append("生产环境必须配置真实域名 DOMAIN，不能使用示例占位符")
        email = _env_value(values, "ACME_EMAIL").lower()
        if _looks_like_placeholder(email) or "@" not in email or "example" in email:
            problems.append("生产环境必须配置真实 ACME_EMAIL，不能使用示例占位符")

        base_url = _env_value(values, "STORE_AGENT_BASE_URL")
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        invalid_host = (
            not host
            or host in {"localhost", "127.0.0.1", "::1"}
            or host.endswith(".example.com")
            or host.endswith(".example.invalid")
            or host.endswith(".invalid")
        )
        if parsed.scheme != "https" or invalid_host:
            problems.append("生产环境 STORE_AGENT_BASE_URL 必须是公网 HTTPS 地址，不能使用回环或示例域名")

        for path_name in (
            "STORE_AGENT_EVIDENCE_DIR",
            "STORE_AGENT_IMPORT_INBOX",
            "STORE_AGENT_BACKUP_LOCAL_ROOT",
            "STORE_AGENT_OPS_LOCK",
        ):
            if not _env_value(values, path_name):
                problems.append(f"生产环境缺少运行路径 {path_name}")

    backup_storage = (_env_value(values, "STORE_AGENT_BACKUP_STORAGE") or "local").lower()
    if backup_storage not in {"local", "s3"}:
        problems.append(f"不支持的 STORE_AGENT_BACKUP_STORAGE：{backup_storage}")
    elif backup_storage == "s3":
        if not _env_value(values, "S3_BUCKET"):
            problems.append("S3 备份缺少 S3_BUCKET")
        if not _env_value(values, "S3_ACCESS_KEY_ID"):
            problems.append("S3 备份缺少 S3_ACCESS_KEY_ID")
        if not _env_value(values, "S3_SECRET_ACCESS_KEY"):
            problems.append("S3 备份缺少 S3_SECRET_ACCESS_KEY")
        if not (_env_value(values, "BACKUP_ENCRYPTION_KEY")
                or _env_value(values, "BACKUP_ENCRYPTION_KEY_FILE")):
            problems.append("S3 备份缺少 BACKUP_ENCRYPTION_KEY 或 BACKUP_ENCRYPTION_KEY_FILE")

    return {
        "ok": not problems,
        "problems": problems,
        "selected": {
            "approval_callback_provider": provider,
            "alert_channel": alert_channel,
            "backup_storage": backup_storage,
        },
    }


def gate_report(root: Path, dsn: Optional[str] = None, config_path: Optional[Path] = None) -> dict[str, Any]:
    """把自检项汇总成给闸门用的一份报告。"""
    capabilities = scan_dangerous_capabilities(root)
    secrets_result = scan_secrets(root)
    retention = check_retention_policy(Path(config_path or Path(root) / "store_config.json"))
    integrations = check_integration_config()
    items = [
        {"gate": "危险能力关闭", "ok": capabilities["ok"], "detail": capabilities},
        {"gate": "仓库无明文密钥", "ok": secrets_result["ok"], "detail": secrets_result},
        {"gate": "数据保留策略已配置", "ok": retention["ok"], "detail": retention},
        {"gate": "生产集成配置已闭合", "ok": integrations["ok"], "detail": integrations},
    ]
    if dsn:
        try:
            hardening = check_postgres_hardening(dsn)
        except Exception as exc:                                   # noqa: BLE001 - 连不上也要出报告
            hardening = {"ok": False, "problems": [f"无法连接 PostgreSQL：{exc}"]}
        items.append({"gate": "PostgreSQL RLS 与应用角色", "ok": hardening["ok"], "detail": hardening})
    return {"ok": all(item["ok"] for item in items), "items": items,
            "generated_at": datetime.now().isoformat(timespec="seconds")}
