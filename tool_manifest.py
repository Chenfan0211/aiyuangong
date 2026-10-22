#!/usr/bin/env python3
"""工具模板 manifest（第 8 期）：校验、导出（清空 secret）与导入。

模板只描述**受控入口**（见 `tool_sandbox.ENTRYPOINTS`）与资源/网络策略，不携带可执行代码，
因此导入导出不需要动态执行，也不会引入任意代码执行能力。
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Iterable, Optional

from tool_sandbox import ENTRYPOINTS, MAX_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, NETWORK_POLICIES

MANIFEST_VERSION = 1
REQUIRED_FIELDS = ("template_key", "name", "entrypoint")
OPTIONAL_FIELDS = ("description", "network_required", "network_policy", "timeout_seconds", "limits",
                   "permissions", "params")
DEFAULT_LIMITS = {"max_read_bytes": 1024 * 1024, "max_file_bytes": 10 * 1024 * 1024,
                  "max_workspace_bytes": 100 * 1024 * 1024, "max_entries": 1000,
                  "max_result_bytes": 200 * 1024}
SECRET_FIELDS = ("secret", "credentials", "api_key", "token", "password")


class ManifestError(ValueError):
    """manifest 不合法。"""


def validate_manifest(manifest: Any) -> dict[str, Any]:
    """规范化 + 校验；只保留已知字段（未知字段一律忽略，不做隐式透传）。"""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest 必须是对象")
    for field in REQUIRED_FIELDS:
        if not str(manifest.get(field) or "").strip():
            raise ManifestError(f"manifest 缺少必填字段：{field}")
    entrypoint = str(manifest["entrypoint"]).strip()
    if entrypoint not in ENTRYPOINTS:
        raise ManifestError(f"入口不在受控白名单内：{entrypoint}（可用：{'、'.join(sorted(ENTRYPOINTS))}）")
    network_policy = str(manifest.get("network_policy") or "deny")
    if network_policy not in NETWORK_POLICIES:
        raise ManifestError(f"network_policy 非法：{network_policy!r}")
    try:
        timeout = float(manifest.get("timeout_seconds") or 30)
    except (TypeError, ValueError) as exc:
        raise ManifestError("timeout_seconds 必须是数字") from exc
    if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ManifestError(f"timeout_seconds 必须在 {MIN_TIMEOUT_SECONDS}-{MAX_TIMEOUT_SECONDS} 秒之间")
    limits = {**DEFAULT_LIMITS, **{key: int(value) for key, value in (manifest.get("limits") or {}).items()}}
    normalized = {
        "manifest_version": MANIFEST_VERSION,
        "template_key": str(manifest["template_key"]).strip(),
        "name": str(manifest["name"]).strip(),
        "entrypoint": entrypoint,
        "description": str(manifest.get("description") or ""),
        "network_required": bool(manifest.get("network_required")),
        "network_policy": network_policy,
        "timeout_seconds": timeout,
        "limits": limits,
        "permissions": [str(item) for item in (manifest.get("permissions") or [])],
        "params": dict(manifest.get("params") or {}),
    }
    return normalized


def _public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """导出用：任何 secret 字段都清空（不留空串以外的痕迹）。"""
    public = {key: value for key, value in manifest.items() if key not in SECRET_FIELDS}
    for field in SECRET_FIELDS:
        if field in manifest:
            public[field] = "" if not isinstance(manifest[field], dict) else {}
    return public


def export_template_zip(manifest: dict[str, Any],
                        secrets: Optional[dict[str, Any]] = None) -> bytes:
    """导出模板 ZIP：`manifest.json` + `secrets.json`（**secret 字段一律清空**）。"""
    normalized = validate_manifest(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(_public_manifest(normalized), ensure_ascii=False, indent=2))
        masked = {key: "" for key in (secrets or {})}
        archive.writestr("secrets.json", json.dumps(masked, ensure_ascii=False, indent=2))
    return buffer.getvalue()


def import_template_zip(data: bytes) -> dict[str, Any]:
    """导入模板 ZIP：校验 manifest，凭据只保留哈希（不落明文）。返回 manifest 与凭据摘要。"""
    import hashlib
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ManifestError("不是合法的 ZIP 包") from exc
    names = set(archive.namelist())
    if "manifest.json" not in names:
        raise ManifestError("ZIP 包缺少 manifest.json")
    try:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("manifest.json 不是合法 JSON") from exc
    secrets_raw = archive.read("secrets.json").decode("utf-8") if "secrets.json" in names else "{}"
    try:
        secrets = json.loads(secrets_raw or "{}")
    except json.JSONDecodeError:
        secrets = {}
    digests = {key: hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]
               for key, value in secrets.items() if str(value or "")}
    return {"manifest": validate_manifest(manifest), "credentials": digests}