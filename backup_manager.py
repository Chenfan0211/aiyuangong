#!/usr/bin/env python3
"""加密备份、异地存储适配与恢复演练。

本模块不修改现有数据库和备份实现，而是在其之上提供一层生产化封装：

* BackupStorage 统一本地卷与 S3 兼容对象存储；
* AES-GCM 加密整个备份包，文件 SHA-256 在使用前强制校验；
* 备份包同时包含数据库逻辑导出、门店配置和图片证据；
* 恢复只允许写入全新目标库，拒绝覆盖来源库或任何非空生产库；
* drill 会恢复到临时数据库并逐表、逐文件校验。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from db import Database
from ops_backup import (
    BackupError as OpsBackupError,
    create_backup as create_plain_backup,
    load_backup,
    restore_backup as restore_plain_backup,
    verify_backup as verify_plain_backup,
)

BACKUP_FORMAT = "aiyg-encrypted-backup"
BACKUP_VERSION = 1
ENCRYPTION_MAGIC = b"AIYGBK1\n"
AAD = b"aiyg-backup-v1"
DEFAULT_DAILY_KEEP = 30
DEFAULT_MONTHLY_KEEP = 12
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")


class BackupError(Exception):
    """备份相关错误基类。"""


class BackupConfigError(BackupError, ValueError):
    """备份配置不完整或参数不合法。"""


class BackupStorageError(BackupError, ValueError):
    """存储适配器错误。"""


class BackupNotFoundError(BackupStorageError, FileNotFoundError):
    """备份对象不存在。"""


class BackupIntegrityError(BackupError):
    """内容哈希、加密认证或备份格式校验失败。"""


class BackupProductionRestoreError(BackupError):
    """恢复目标看起来是生产库或已有数据，拒绝覆盖。"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def derive_encryption_key(secret: str | bytes) -> bytes:
    """把环境变量转换为 32 字节密钥。

    32 字节 bytes 原值、Base64/URL-safe Base64 或 64 位十六进制会按原值使用；
    其它口令使用 SHA-256 派生，便于无第三方密钥时本地启动。
    """
    if isinstance(secret, bytes):
        if len(secret) == 32:
            return secret
        try:
            secret = secret.decode("utf-8")
        except UnicodeDecodeError:
            return hashlib.sha256(secret).digest()
    text = str(secret).strip()
    if not text:
        raise BackupConfigError("备份加密密钥不能为空")
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            padded = text + "=" * (-len(text) % 4)
            decoded = decoder(padded)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass
    try:
        if len(text) == 64:
            decoded = bytes.fromhex(text)
            if len(decoded) == 32:
                return decoded
    except ValueError:
        pass
    return hashlib.sha256(text.encode("utf-8")).digest()


def load_or_create_encryption_key(path: Path) -> bytes:
    """无外部密钥时，在受限文件中生成并复用本地 AES-GCM 密钥。"""
    path = Path(path)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return derive_encryption_key(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    temporary.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def encrypt_payload(plaintext: bytes, key: bytes) -> bytes:
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, AAD)
    return ENCRYPTION_MAGIC + nonce + ciphertext


def decrypt_payload(blob: bytes, key: bytes) -> bytes:
    if not blob.startswith(ENCRYPTION_MAGIC):
        raise BackupIntegrityError("备份文件不是本系统的加密格式")
    body = blob[len(ENCRYPTION_MAGIC):]
    if len(body) < 13:
        raise BackupIntegrityError("备份文件被截断")
    nonce, ciphertext = body[:12], body[12:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, AAD)
    except InvalidTag as exc:
        raise BackupIntegrityError("备份加密认证失败（密钥错误或内容被篡改）") from exc


def _validate_key(key: str) -> str:
    key = str(key).replace("\\", "/")
    path = PurePosixPath(key)
    if not key or key.startswith("/") or path.is_absolute() or ".." in path.parts:
        raise BackupStorageError(f"非法备份对象 key：{key}")
    if not _KEY_RE.match(key):
        raise BackupStorageError(f"备份对象 key 含非法字符：{key}")
    return path.as_posix()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if not value or path.is_absolute() or ".." in path.parts:
        raise BackupIntegrityError(f"备份包内存在非法相对路径：{value}")
    return path


class BackupStorage(ABC):
    """备份对象存储接口。"""

    @abstractmethod
    def put(self, key: str, data: bytes, metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get(self, key: str) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def stat(self, key: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def list(self, prefix: str = "") -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> bool:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        try:
            self.stat(key)
            return True
        except BackupNotFoundError:
            return False


class LocalBackupStorage(BackupStorage):
    """受控数据卷上的备份存储，适合无 S3 密钥的默认模式。"""

    def __init__(self, root_dir: Path | str):
        self.root = Path(root_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _data_path(self, key: str) -> tuple[str, Path]:
        clean = _validate_key(key)
        path = (self.root / clean).resolve()
        if path != self.root and self.root not in path.parents:
            raise BackupStorageError(f"备份路径越出根目录：{key}")
        return clean, path

    @staticmethod
    def _meta_path(path: Path) -> Path:
        return path.with_name(path.name + ".meta.json")

    def put(self, key: str, data: bytes, metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        clean, path = self._data_path(key)
        record = {
            "key": clean,
            "sha256": sha256_bytes(data),
            "size": len(data),
            "created_at": (metadata or {}).get("created_at") or now_iso(),
            "storage": "local",
        }
        record.update({str(k): str(v) for k, v in (metadata or {}).items()})
        _atomic_write(path, data)
        _atomic_write(self._meta_path(path), json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        return record

    def get(self, key: str) -> bytes:
        clean, path = self._data_path(key)
        if not path.is_file():
            raise BackupNotFoundError(f"备份对象不存在：{clean}")
        data = path.read_bytes()
        meta_path = self._meta_path(path)
        if meta_path.is_file():
            try:
                record = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BackupIntegrityError(f"备份元数据损坏：{clean}") from exc
            expected = record.get("sha256")
            if expected and sha256_bytes(data) != expected:
                raise BackupIntegrityError(f"备份对象 SHA-256 校验失败：{clean}")
        return data

    def stat(self, key: str) -> dict[str, Any]:
        clean, path = self._data_path(key)
        if not path.is_file():
            raise BackupNotFoundError(f"备份对象不存在：{clean}")
        meta_path = self._meta_path(path)
        if meta_path.is_file():
            try:
                record = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BackupIntegrityError(f"备份元数据损坏：{clean}") from exc
            record.setdefault("key", clean)
            record.setdefault("size", path.stat().st_size)
            return record
        return {"key": clean, "size": path.stat().st_size, "storage": "local", "sha256": None}

    def list(self, prefix: str = "") -> list[dict[str, Any]]:
        clean_prefix = str(prefix or "").replace("\\", "/").lstrip("/")
        records: list[dict[str, Any]] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.endswith(".meta.json"):
                continue
            key = path.relative_to(self.root).as_posix()
            if clean_prefix and not key.startswith(clean_prefix):
                continue
            try:
                records.append(self.stat(key))
            except BackupNotFoundError:
                continue
        return records

    def delete(self, key: str) -> bool:
        clean, path = self._data_path(key)
        if not path.is_file():
            return False
        path.unlink()  # noqa: dangerous
        self._meta_path(path).unlink(missing_ok=True)  # noqa: dangerous
        return True

    def location(self) -> str:
        return f"local:{self.root}"


class S3BackupStorage(BackupStorage):
    """S3 / OSS / COS / MinIO 兼容存储。

    真实模式按需导入 boto3；测试或私有部署可注入任意符合 S3 最小契约的客户端。
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,
        endpoint_url: Optional[str] = None,
        region: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        path_style: bool = True,
    ):
        if not bucket:
            raise BackupConfigError("S3_BUCKET 不能为空")
        self.bucket = str(bucket)
        self.prefix = str(prefix or "").strip("/")
        self.endpoint_url = endpoint_url
        self.region = region
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.path_style = bool(path_style)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - 仅真实 S3 模式
                raise BackupConfigError("使用 S3 备份需要安装 boto3") from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url or None,
                region_name=self.region or None,
                aws_access_key_id=self.access_key_id or None,
                aws_secret_access_key=self.secret_access_key or None,
            )
        return self._client

    def _object_key(self, key: str) -> str:
        clean = _validate_key(key).lstrip("/")
        if self.prefix:
            if clean == self.prefix or clean.startswith(self.prefix + "/"):
                return clean
            return f"{self.prefix}/{clean}"
        return clean

    @staticmethod
    def _body_bytes(response: Any) -> bytes:
        if not isinstance(response, dict):
            response = {"Body": response}
        body = response.get("Body")
        if hasattr(body, "read"):
            return body.read()
        return bytes(body)

    def put(self, key: str, data: bytes, metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        object_key = self._object_key(key)
        record = {
            "key": object_key,
            "sha256": sha256_bytes(data),
            "size": len(data),
            "created_at": (metadata or {}).get("created_at") or now_iso(),
            "storage": "s3",
            "bucket": self.bucket,
        }
        record.update({str(k): str(v) for k, v in (metadata or {}).items()})
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=data,
                Metadata={str(k): str(v) for k, v in record.items()},
            )
        except Exception as exc:
            raise BackupStorageError(f"S3 上传失败：{object_key}：{exc}") from exc
        return record

    def get(self, key: str) -> bytes:
        object_key = self._object_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:
            raise BackupNotFoundError(f"S3 对象不存在或读取失败：{object_key}") from exc
        data = self._body_bytes(response)
        metadata = response.get("Metadata") or {} if isinstance(response, dict) else {}
        expected = metadata.get("sha256")
        if expected and sha256_bytes(data) != expected:
            raise BackupIntegrityError(f"S3 对象 SHA-256 校验失败：{object_key}")
        return data

    def stat(self, key: str) -> dict[str, Any]:
        object_key = self._object_key(key)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:
            raise BackupNotFoundError(f"S3 对象不存在：{object_key}") from exc
        metadata = dict(response.get("Metadata") or {})
        metadata.setdefault("key", object_key)
        metadata.setdefault("size", int(response.get("ContentLength", 0)))
        metadata.setdefault("storage", "s3")
        metadata.setdefault("bucket", self.bucket)
        return metadata

    def list(self, prefix: str = "") -> list[dict[str, Any]]:
        full_prefix = self._object_key(prefix) if prefix else (self.prefix + "/" if self.prefix else "")
        records: list[dict[str, Any]] = []
        token: Optional[str] = None
        while True:
            params: dict[str, Any] = {"Bucket": self.bucket, "Prefix": full_prefix, "MaxKeys": 1000}
            if token:
                params["ContinuationToken"] = token
            try:
                response = self.client.list_objects_v2(**params)
            except Exception as exc:
                raise BackupStorageError(f"S3 列表读取失败：{exc}") from exc
            for item in response.get("Contents", []):
                object_key = item["Key"]
                try:
                    record = self.stat(object_key)
                except BackupNotFoundError:
                    continue
                record.setdefault("last_modified", str(item.get("LastModified", "")))
                records.append(record)
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                break
        records.sort(key=lambda item: str(item.get("created_at") or item.get("last_modified") or item["key"]))
        return records

    def delete(self, key: str) -> bool:
        object_key = self._object_key(key)
        if not self.exists(object_key):
            return False
        try:
            self.client.delete_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:
            raise BackupStorageError(f"S3 删除失败：{object_key}：{exc}") from exc
        return True

    def location(self) -> str:
        endpoint = self.endpoint_url or "aws"
        return f"s3:{endpoint}/{self.bucket}/{self.prefix}".rstrip("/")


def _normalized_database_identity(target: Any) -> str:
    text = str(target)
    if text.startswith(("postgresql://", "postgres://")):
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port or 5432
        database = (parts.path or "").lstrip("/")
        user = parts.username or ""
        return f"postgresql://{user}@{host}:{port}/{database}"
    return "sqlite:" + str(Path(text).expanduser().resolve())


def fingerprint_database(target: Any) -> str:
    return sha256_bytes(_normalized_database_identity(target).encode("utf-8"))


def safe_database_label(target: Any) -> str:
    text = str(target)
    if text.startswith(("postgresql://", "postgres://")):
        parts = urlsplit(text)
        return f"postgresql://{parts.hostname or ''}:{parts.port or 5432}/{(parts.path or '').lstrip('/')}"
    return str(Path(text).expanduser())


def _database_is_empty(target: Any) -> bool:
    db = Database(target)
    if db.dialect == "sqlite":
        path = Path(db.target)
        if not path.exists() or path.stat().st_size == 0:
            return True
        conn = sqlite3.connect(str(path))
        try:
            tables = [row[0] for row in conn.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            ).fetchall()]
            for table in tables:
                count = conn.execute(f'select count(*) from "{table}"').fetchone()[0]
                if int(count):
                    return False
        finally:
            conn.close()
        return True

    import psycopg

    try:
        with psycopg.connect(db.target) as conn, conn.cursor() as cur:
            cur.execute(
                "select table_name from information_schema.tables "
                "where table_schema='public' and table_type='BASE TABLE'"
            )
            tables = [row[0] for row in cur.fetchall()]
            for table in tables:
                cur.execute(f'select count(*) from "{table}"')
                if int(cur.fetchone()[0]):
                    return False
    except Exception as exc:
        raise BackupConfigError(f"无法检查 PostgreSQL 恢复目标是否为空：{exc}") from exc
    return True


def _database_snapshot(target: Any, tenant_id: str, table_hashes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from ops_backup import _read_table, _table_columns, table_hash

    db = Database(target, tenant_id=tenant_id)
    checked: dict[str, Any] = {}
    with db.connection() as conn:
        for table, expected in table_hashes.items():
            columns = _table_columns(db, conn, table)
            if not columns:
                raise BackupIntegrityError(f"恢复目标缺少表：{table}")
            rows = _read_table(conn, table, columns)
            actual_hash = table_hash(rows)
            checked[table] = {
                "expected_rows": int(expected["row_count"]),
                "actual_rows": len(rows),
                "hash_match": actual_hash == expected["sha256"],
            }
    return {"ok": all(item["actual_rows"] == item["expected_rows"] and item["hash_match"] for item in checked.values()),
            "tables": checked}


class BackupManager:
    """加密备份包的生命周期管理。"""

    def __init__(
        self,
        storage: BackupStorage,
        *,
        encryption_key: Optional[bytes | str] = None,
        key_file: Optional[Path | str] = None,
        daily_keep: int = DEFAULT_DAILY_KEEP,
        monthly_keep: int = DEFAULT_MONTHLY_KEEP,
    ):
        if encryption_key is None:
            if key_file is None:
                raise BackupConfigError("必须提供 encryption_key 或 key_file")
            encryption_key = load_or_create_encryption_key(Path(key_file))
        self.key = derive_encryption_key(encryption_key)
        self.storage = storage
        self.daily_keep = max(1, int(daily_keep))
        self.monthly_keep = max(1, int(monthly_keep))

    def _package_key(self, category: str, now: datetime) -> str:
        if category not in ("daily", "monthly"):
            raise BackupConfigError("备份类别只能是 daily 或 monthly")
        suffix = f"{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:12]}.aiygbk"
        if category == "daily":
            return f"daily/{now:%Y/%m/%d}/{suffix}"
        return f"monthly/{now:%Y/%m}/{suffix}"

    @staticmethod
    def _canonical_json(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def create_backup(
        self,
        db_target: Any,
        *,
        tenant_id: str = "",
        store_id: str = "",
        evidence_dir: Optional[Path | str] = None,
        config_path: Optional[Path | str] = None,
        now: Optional[datetime] = None,
        create_monthly: bool = True,
    ) -> dict[str, Any]:
        moment = now or datetime.now()
        work = Path(tempfile.mkdtemp(prefix="aiyg_backup_package_"))
        try:
            raw_db_path = work / "database.json.gz"
            database_manifest = create_plain_backup(
                db_target,
                raw_db_path,
                compress=True,
                tenant_id=tenant_id,
                store_id=store_id,
            )
            database_verification = verify_plain_backup(raw_db_path)
            if not database_verification["ok"]:
                raise BackupIntegrityError("数据库逻辑备份自检失败：" + "；".join(database_verification["problems"]))
            database_bytes = raw_db_path.read_bytes()
            database = load_backup(raw_db_path)
            table_verification = {
                table: {"row_count": int(item["row_count"]), "sha256": item["sha256"]}
                for table, item in database["tables"].items()
            }

            evidence_files: list[dict[str, Any]] = []
            if evidence_dir is not None:
                evidence_root = Path(evidence_dir).expanduser().resolve()
                if evidence_root.exists():
                    for path in sorted(evidence_root.rglob("*")):
                        if not path.is_file():
                            continue
                        relative = path.relative_to(evidence_root).as_posix()
                        data = path.read_bytes()
                        evidence_files.append({
                            "path": relative,
                            "size": len(data),
                            "sha256": sha256_bytes(data),
                            "data_base64": base64.b64encode(data).decode("ascii"),
                        })

            config_info: dict[str, Any] = {"included": False}
            if config_path is not None:
                config_file = Path(config_path).expanduser().resolve()
                if config_file.is_file():
                    data = config_file.read_bytes()
                    config_info = {
                        "included": True,
                        "path": config_file.name,
                        "size": len(data),
                        "sha256": sha256_bytes(data),
                        "data_base64": base64.b64encode(data).decode("ascii"),
                    }

            manifest = {
                "format": BACKUP_FORMAT,
                "version": BACKUP_VERSION,
                "created_at": moment.isoformat(timespec="seconds"),
                "tenant_id": tenant_id,
                "store_id": store_id,
                "source_fingerprint": fingerprint_database(db_target),
                "source": safe_database_label(db_target),
                "database": {
                    "sha256": sha256_bytes(database_bytes),
                    "size": len(database_bytes),
                    "table_count": int(database_manifest["table_count"]),
                    "row_total": int(database_manifest["row_total"]),
                },
                "database_tables": table_verification,
                "evidence_count": len(evidence_files),
                "evidence": evidence_files,
                "config": config_info,
            }
            package = {
                "manifest": manifest,
                "database_backup_base64": base64.b64encode(database_bytes).decode("ascii"),
                "content_sha256": sha256_bytes(self._canonical_json(manifest)),
            }
            encrypted = encrypt_payload(self._canonical_json(package), self.key)
            daily_key = self._package_key("daily", moment)
            daily = self.storage.put(
                daily_key,
                encrypted,
                metadata={
                    "category": "daily",
                    "created_at": manifest["created_at"],
                    "source_fingerprint": manifest["source_fingerprint"],
                    "tenant_id": tenant_id,
                    "store_id": store_id,
                },
            )

            monthly = None
            month_prefix = f"monthly/{moment:%Y/%m}/"
            if create_monthly and not self.storage.list(month_prefix):
                monthly_key = self._package_key("monthly", moment)
                monthly = self.storage.put(
                    monthly_key,
                    encrypted,
                    metadata={
                        "category": "monthly",
                        "created_at": manifest["created_at"],
                        "source_fingerprint": manifest["source_fingerprint"],
                        "tenant_id": tenant_id,
                        "store_id": store_id,
                    },
                )

            pruned = {
                "daily": self.prune("daily", self.daily_keep),
                "monthly": self.prune("monthly", self.monthly_keep),
            }
            return {"ok": True, "daily": daily, "monthly": monthly, "manifest": manifest, "pruned": pruned}
        finally:
            shutil.rmtree(work, ignore_errors=True)  # noqa: dangerous


    def _load(self, key: str, expected_sha256: Optional[str] = None) -> tuple[bytes, dict[str, Any], dict[str, Any], dict[str, Any]]:
        try:
            record = self.storage.stat(key)
        except BackupNotFoundError:
            raise
        expected = expected_sha256 or record.get("sha256")
        if not expected:
            raise BackupIntegrityError("缺少备份对象 SHA-256，拒绝解密或恢复")
        body = self.storage.get(key)
        actual = sha256_bytes(body)
        if actual != str(expected):
            raise BackupIntegrityError(f"备份对象 SHA-256 校验失败：期望 {expected}，实际 {actual}")
        try:
            package = json.loads(decrypt_payload(body, self.key).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupIntegrityError(f"备份包无法解析：{exc}") from exc
        manifest = package.get("manifest") or {}
        if manifest.get("format") != BACKUP_FORMAT or int(manifest.get("version", 0)) != BACKUP_VERSION:
            raise BackupIntegrityError("备份包格式或版本不受支持")
        if package.get("content_sha256") != sha256_bytes(self._canonical_json(manifest)):
            raise BackupIntegrityError("备份清单内容哈希不匹配")
        return body, record, manifest, package

    def verify(self, key: str, expected_sha256: Optional[str] = None) -> dict[str, Any]:
        body, record, manifest, _ = self._load(key, expected_sha256)
        return {"ok": True, "object_key": record.get("key", key), "sha256": sha256_bytes(body),
                "size": len(body), "record": record, "manifest": manifest}

    def restore(
        self,
        key: str,
        *,
        target_db: Any,
        evidence_dir: Optional[Path | str] = None,
        config_path: Optional[Path | str] = None,
        expected_sha256: Optional[str] = None,
        tenant_id: str = "",
        store_id: str = "",
    ) -> dict[str, Any]:
        body, record, manifest, package = self._load(key, expected_sha256)
        if fingerprint_database(target_db) == manifest.get("source_fingerprint"):
            raise BackupProductionRestoreError("恢复目标与备份来源是同一个数据库，拒绝覆盖生产库")
        if not _database_is_empty(target_db):
            raise BackupProductionRestoreError("恢复目标非空，拒绝覆盖；请使用全新数据库执行恢复")

        work = Path(tempfile.mkdtemp(prefix="aiyg_backup_restore_"))
        try:
            raw_db_path = work / "database.json.gz"
            raw_db_path.write_bytes(base64.b64decode(package["database_backup_base64"]))
            verification = verify_plain_backup(raw_db_path)
            if not verification["ok"]:
                raise BackupIntegrityError("数据库逻辑备份校验失败，拒绝恢复：" + "；".join(verification["problems"]))
            restored = restore_plain_backup(
                target_db,
                raw_db_path,
                force=True,
                tenant_id=tenant_id,
                store_id=store_id,
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)  # noqa: dangerous

        restored_files = 0
        reused_files = 0
        evidence_report: list[dict[str, Any]] = []
        if evidence_dir is not None:
            root = Path(evidence_dir).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            for item in manifest.get("evidence", []):
                relative = _safe_relative_path(item["path"])
                destination = (root / Path(*relative.parts)).resolve()
                if root not in destination.parents:
                    raise BackupIntegrityError(f"备份包证据路径越界：{relative}")
                data = base64.b64decode(item["data_base64"])
                if sha256_bytes(data) != item["sha256"] or len(data) != int(item["size"]):
                    raise BackupIntegrityError(f"备份包证据内容校验失败：{relative}")
                if destination.exists():
                    if sha256_file(destination) != item["sha256"]:
                        raise BackupProductionRestoreError(f"目标证据文件已存在且内容不同：{relative}")
                    reused_files += 1
                else:
                    _atomic_write(destination, data)
                    restored_files += 1
                evidence_report.append({"path": relative.as_posix(), "sha256": item["sha256"], "size": len(data)})

        config_verified = False
        if config_path is not None and manifest.get("config", {}).get("included"):
            item = manifest["config"]
            data = base64.b64decode(item["data_base64"])
            if sha256_bytes(data) != item["sha256"] or len(data) != int(item["size"]):
                raise BackupIntegrityError("备份包配置文件校验失败")
            destination = Path(config_path).expanduser().resolve()
            if destination.exists():
                if sha256_file(destination) != item["sha256"]:
                    raise BackupProductionRestoreError(f"目标配置文件已存在且内容不同：{destination}")
            else:
                _atomic_write(destination, data)
            config_verified = True

        database_report = _database_snapshot(target_db, tenant_id, manifest.get("database_tables", {}))
        if not database_report["ok"]:
            raise BackupIntegrityError("恢复后的数据库与备份清单不一致")
        evidence_verified = all(sha256_bytes(base64.b64decode(item["data_base64"])) == item["sha256"]
                                for item in manifest.get("evidence", []))
        return {
            "ok": database_report["ok"] and evidence_verified,
            "object_key": record.get("key", key),
            "sha256": sha256_bytes(body),
            "manifest": manifest,
            "database": {"restored": restored, "verified": database_report["ok"], **database_report},
            "evidence": {"restored_files": restored_files, "reused_files": reused_files,
                         "files": evidence_report, "verified": evidence_verified},
            "config": {"verified": config_verified},
        }

    def drill(
        self,
        db_target: Any,
        *,
        tenant_id: str = "",
        store_id: str = "",
        evidence_dir: Optional[Path | str] = None,
        config_path: Optional[Path | str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        work = Path(tempfile.mkdtemp(prefix="aiyg_backup_drill_"))
        drill_db_name: Optional[str] = None
        try:
            storage = LocalBackupStorage(work / "storage")
            manager = BackupManager(
                storage,
                encryption_key=self.key,
                daily_keep=self.daily_keep,
                monthly_keep=self.monthly_keep,
            )
            backup = manager.create_backup(
                db_target,
                tenant_id=tenant_id,
                store_id=store_id,
                evidence_dir=evidence_dir,
                config_path=config_path,
                now=now,
            )
            source_db = Database(db_target)
            if source_db.dialect == "sqlite":
                target_db: Any = work / "restored.db"
            else:
                drill_db_name = "aiyg_bm_drill_" + uuid.uuid4().hex[:10]
                target_db = self._create_temporary_postgres_database(db_target, drill_db_name)
            restore = manager.restore(
                backup["daily"]["key"],
                target_db=target_db,
                evidence_dir=work / "evidence",
                config_path=work / "store_config.json",
                tenant_id=tenant_id,
                store_id=store_id,
            )
            evidence_verified = bool(restore["evidence"]["verified"])
            config_verified = bool(restore["config"]["verified"] or not backup["manifest"].get("config", {}).get("included"))
            return {
                "ok": bool(restore["ok"] and restore["database"]["verified"] and evidence_verified and config_verified),
                "backup": backup,
                "restore": restore,
                "database_verified": bool(restore["database"]["verified"]),
                "evidence_verified": evidence_verified,
                "config_verified": config_verified,
                "restored_files": int(restore["evidence"]["restored_files"]),
            }
        finally:
            if drill_db_name:
                try:
                    self._drop_temporary_postgres_database(db_target, drill_db_name)
                except Exception:
                    pass
            shutil.rmtree(work, ignore_errors=True)  # noqa: dangerous

    def prune(self, category: str, keep: int) -> int:
        if category not in ("daily", "monthly"):
            raise BackupConfigError("备份类别只能是 daily 或 monthly")
        records = self.storage.list(f"{category}/")
        records.sort(key=lambda item: str(item.get("created_at") or item["key"]), reverse=True)
        deleted = 0
        for record in records[max(0, int(keep)):]:
            if self.storage.delete(record["key"]):
                deleted += 1
        return deleted

    def list_backups(self, prefix: str = "") -> list[dict[str, Any]]:
        return self.storage.list(prefix)

    @staticmethod
    def _create_temporary_postgres_database(source_dsn: str, name: str) -> str:
        import psycopg

        with psycopg.connect(source_dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f'create database "{name}"')
        parts = urlsplit(source_dsn)
        return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))

    @staticmethod
    def _drop_temporary_postgres_database(source_dsn: str, name: str) -> None:
        import psycopg

        with psycopg.connect(source_dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f'drop database if exists "{name}" with (force)')


def _storage_from_args(args: argparse.Namespace) -> BackupStorage:
    backend = (args.storage or os.getenv("STORE_AGENT_BACKUP_STORAGE", "local")).lower()
    if backend == "local":
        root = args.local_root or os.getenv("STORE_AGENT_BACKUP_LOCAL_ROOT", "data/backups")
        return LocalBackupStorage(root)
    if backend == "s3":
        bucket = args.s3_bucket or os.getenv("S3_BUCKET")
        return S3BackupStorage(
            bucket or "",
            prefix=args.s3_prefix or os.getenv("S3_PREFIX", ""),
            endpoint_url=os.getenv("S3_ENDPOINT"),
            region=os.getenv("S3_REGION"),
            access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
            path_style=(os.getenv("S3_PATH_STYLE", "true").lower() not in ("0", "false", "no")),
        )
    raise BackupConfigError(f"未知备份存储：{backend}")


def _manager_from_args(args: argparse.Namespace) -> BackupManager:
    storage = _storage_from_args(args)
    secret = args.encryption_key or os.getenv("BACKUP_ENCRYPTION_KEY")
    if secret:
        return BackupManager(
            storage,
            encryption_key=secret,
            daily_keep=args.daily_keep,
            monthly_keep=args.monthly_keep,
        )
    key_file = args.encryption_key_file or os.getenv("BACKUP_ENCRYPTION_KEY_FILE", "data/backups/.backup-key")
    return BackupManager(
        storage,
        key_file=key_file,
        daily_keep=args.daily_keep,
        monthly_keep=args.monthly_keep,
    )


def _add_storage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--storage", choices=("local", "s3"), default=None)
    parser.add_argument("--local-root", default=None)
    parser.add_argument("--s3-bucket", default=None)
    parser.add_argument("--s3-prefix", default=None)
    parser.add_argument("--encryption-key", default=None)
    parser.add_argument("--encryption-key-file", default=None)
    parser.add_argument("--daily-keep", type=int, default=DEFAULT_DAILY_KEEP)
    parser.add_argument("--monthly-keep", type=int, default=DEFAULT_MONTHLY_KEEP)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="门店运营系统加密备份与恢复")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="创建加密日备份和当月月备份")
    backup.add_argument("--db", required=True)
    backup.add_argument("--tenant-id", default="")
    backup.add_argument("--store-id", default="")
    backup.add_argument("--evidence-dir", default=None)
    backup.add_argument("--config", default=None)
    _add_storage_args(backup)

    restore = sub.add_parser("restore", help="校验后恢复到全新数据库")
    restore.add_argument("--object-key", required=True)
    restore.add_argument("--target-db", required=True)
    restore.add_argument("--evidence-dir", default=None)
    restore.add_argument("--config-out", default=None)
    restore.add_argument("--tenant-id", default="")
    restore.add_argument("--store-id", default="")
    restore.add_argument("--expected-sha256", default=None)
    _add_storage_args(restore)

    drill = sub.add_parser("drill", help="执行完整加密恢复演练")
    drill.add_argument("--db", required=True)
    drill.add_argument("--tenant-id", default="")
    drill.add_argument("--store-id", default="")
    drill.add_argument("--evidence-dir", default=None)
    drill.add_argument("--config", default=None)
    _add_storage_args(drill)

    prune = sub.add_parser("prune", help="按保留策略清理旧备份")
    prune.add_argument("--category", choices=("daily", "monthly"), required=True)
    prune.add_argument("--keep", type=int, required=True)
    _add_storage_args(prune)

    listing = sub.add_parser("list", help="列出备份对象")
    listing.add_argument("--prefix", default="")
    _add_storage_args(listing)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            manager = _manager_from_args(args)
            result = manager.create_backup(
                args.db,
                tenant_id=args.tenant_id,
                store_id=args.store_id,
                evidence_dir=args.evidence_dir,
                config_path=args.config,
            )
            print(json.dumps(result, ensure_ascii=True, default=str))
            return 0
        if args.command == "restore":
            manager = _manager_from_args(args)
            result = manager.restore(
                args.object_key,
                target_db=args.target_db,
                evidence_dir=args.evidence_dir,
                config_path=args.config_out,
                expected_sha256=args.expected_sha256,
                tenant_id=args.tenant_id,
                store_id=args.store_id,
            )
            print(json.dumps(result, ensure_ascii=True, default=str))
            return 0
        if args.command == "drill":
            manager = _manager_from_args(args)
            result = manager.drill(
                args.db,
                tenant_id=args.tenant_id,
                store_id=args.store_id,
                evidence_dir=args.evidence_dir,
                config_path=args.config,
            )
            print(json.dumps(result, ensure_ascii=True, default=str))
            return 0 if result["ok"] else 2
        if args.command == "prune":
            manager = _manager_from_args(args)
            deleted = manager.prune(args.category, args.keep)
            print(json.dumps({"ok": True, "category": args.category, "deleted": deleted}, ensure_ascii=True))
            return 0
        if args.command == "list":
            manager = _manager_from_args(args)
            records = manager.list_backups(args.prefix)
            print(json.dumps({"ok": True, "objects": records}, ensure_ascii=True, default=str))
            return 0
    except (BackupError, OpsBackupError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
