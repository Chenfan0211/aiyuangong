# -*- coding: utf-8 -*-
"""图片证据存储模块。

文件字节保存在可配置根目录下，元数据通过 ``db.Database`` 写入 SQLite 或
PostgreSQL。文件名由服务端随机 UUID 生成，原始文件名只作为元数据保存，
因此调用方提供的文件名不能影响落盘路径。
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Database


MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
DEFAULT_EVIDENCE_ROOT = Path(
    os.getenv("EVIDENCE_ROOT_DIR", str(Path(__file__).resolve().parent / "data" / "evidence"))
)

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MIME_ALIASES = {"image/jpg": "image/jpeg"}
_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_SQLITE_SCHEMA = """
create table if not exists evidence_files (
  tenant_id text not null default '',
  store_id text not null,
  task_id text not null,
  evidence_id text not null,
  original_name text not null,
  stored_path text not null,
  mime_type text not null,
  size_bytes integer not null,
  sha256 text not null,
  uploaded_by text not null,
  created_at text not null,
  primary key (evidence_id)
);
create index if not exists idx_evidence_files_scope
  on evidence_files(tenant_id, store_id, task_id, created_at);
"""

_POSTGRES_SCHEMA = """
create table if not exists evidence_files (
  tenant_id text not null default '',
  store_id text not null,
  task_id text not null,
  evidence_id text not null,
  original_name text not null,
  stored_path text not null,
  mime_type text not null,
  size_bytes integer not null,
  sha256 text not null,
  uploaded_by text not null,
  created_at timestamptz not null,
  primary key (evidence_id)
);
create index if not exists idx_evidence_files_scope
  on evidence_files(tenant_id, store_id, task_id, created_at);
"""


class EvidenceError(Exception):
    """证据存储相关错误基类。"""


class EvidenceValidationError(EvidenceError, ValueError):
    """证据参数不合法。"""


class EvidenceMimeError(EvidenceValidationError):
    """图片内容或声明的 MIME 类型不受支持。"""


class EvidenceSizeError(EvidenceValidationError):
    """图片超过大小限制。"""


class EvidencePathError(EvidenceValidationError):
    """文件名或存储路径存在穿越风险。"""


class EvidenceNotFoundError(EvidenceError, LookupError):
    """证据不存在，或不在调用方的作用域内。"""


class EvidenceIntegrityError(EvidenceError):
    """落盘内容与元数据不一致。"""


class EvidenceStorageError(EvidenceError):
    """文件系统或数据库写入失败。"""


class EvidenceStore:
    """按 tenant/store/task 作用域管理图片证据。"""

    def __init__(
        self,
        db_target: Any,
        root_dir: Optional[Path | str] = None,
        tenant_id: str = "",
    ):
        if isinstance(db_target, Database):
            self.db = db_target
            if tenant_id and not self.db.tenant_id:
                self.db.tenant_id = tenant_id
        else:
            self.db = Database(db_target, tenant_id=tenant_id)

        self.root_dir = Path(root_dir if root_dir is not None else DEFAULT_EVIDENCE_ROOT).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        schema = _POSTGRES_SCHEMA if self.db.dialect == "postgresql" else _SQLITE_SCHEMA
        with self.db.connection() as conn:
            conn.executescript(schema)

    def save(
        self,
        task_id: str,
        original_name: str,
        data: Any,
        uploaded_by: str,
        *,
        store_id: str,
        tenant_id: Optional[str] = None,
        mime_type: Optional[str] = None,
        evidence_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """保存图片并返回元数据；任何校验失败都不会留下文件。"""
        tenant = self._tenant(tenant_id)
        task = self._require_text(task_id, "task_id")
        store = self._require_text(store_id, "store_id")
        uploader = self._require_text(uploaded_by, "uploaded_by")
        name = self._validate_original_name(original_name)
        raw = self._coerce_bytes(data)

        if len(raw) > MAX_EVIDENCE_BYTES:
            raise EvidenceSizeError(
                f"图片超过最大限制 {MAX_EVIDENCE_BYTES} 字节（当前 {len(raw)} 字节）"
            )

        detected_mime = self._detect_mime(raw)
        if detected_mime is None:
            raise EvidenceMimeError("只支持 JPEG/PNG/WebP 图片（magic bytes 校验失败）")
        declared_mime = self._normalize_mime(mime_type)
        if declared_mime is not None and declared_mime != detected_mime:
            raise EvidenceMimeError(
                f"声明的 MIME 类型 {declared_mime} 与实际图片类型 {detected_mime} 不一致"
            )

        evidence = evidence_id if evidence_id is not None else uuid.uuid4().hex
        evidence = self._validate_evidence_id(evidence)
        stored_name = f"{evidence}{MIME_EXTENSIONS[detected_mime]}"
        path = self._resolve_stored_path(stored_name)

        try:
            with path.open("xb") as handle:
                handle.write(raw)
        except FileExistsError as exc:
            raise EvidenceStorageError(f"证据文件已存在：{evidence}") from exc
        except OSError as exc:
            raise EvidenceStorageError(f"证据文件写入失败：{exc}") from exc

        digest = hashlib.sha256(raw).hexdigest()
        created_at = datetime.now().isoformat(timespec="seconds")
        try:
            with self.db.connection() as conn:
                conn.execute(
                    """insert into evidence_files
                       (tenant_id, store_id, task_id, evidence_id, original_name,
                        stored_path, mime_type, size_bytes, sha256, uploaded_by, created_at)
                       values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        tenant,
                        store,
                        task,
                        evidence,
                        name,
                        stored_name,
                        detected_mime,
                        len(raw),
                        digest,
                        uploader,
                        created_at,
                    ),
                )
        except Exception as exc:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise EvidenceStorageError(f"证据元数据写入失败：{exc}") from exc

        return self.metadata(
            evidence,
            tenant_id=tenant,
            store_id=store,
            task_id=task,
        )

    def read(
        self,
        evidence_id: str,
        *,
        store_id: str,
        task_id: str,
        tenant_id: Optional[str] = None,
    ) -> bytes:
        """按 evidence_id 和完整作用域读取图片字节。"""
        row = self.metadata(
            evidence_id,
            tenant_id=tenant_id,
            store_id=store_id,
            task_id=task_id,
        )
        path = self._resolve_stored_path(row["stored_path"])
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise EvidenceNotFoundError(f"证据文件不存在：{evidence_id}") from exc
        except OSError as exc:
            raise EvidenceStorageError(f"证据文件读取失败：{exc}") from exc

        if len(data) != int(row["size_bytes"]) or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise EvidenceIntegrityError(f"证据文件校验失败：{evidence_id}")
        return data

    def metadata(
        self,
        evidence_id: str,
        *,
        store_id: str,
        task_id: str,
        tenant_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """按完整作用域读取单个证据元数据，越域时按不存在处理。"""
        evidence = self._validate_evidence_id(evidence_id)
        tenant = self._tenant(tenant_id)
        store = self._require_text(store_id, "store_id")
        task = self._require_text(task_id, "task_id")
        with self.db.connection() as conn:
            row = conn.execute(
                """select * from evidence_files
                   where evidence_id=? and tenant_id=? and store_id=? and task_id=?""",
                (evidence, tenant, store, task),
            ).fetchone()
        if not row:
            raise EvidenceNotFoundError(f"证据不存在或无权访问：{evidence}")
        return dict(row)

    def list_evidence(
        self,
        *,
        store_id: str,
        task_id: str,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列出某个 tenant/store/task 作用域下的证据元数据。"""
        tenant = self._tenant(tenant_id)
        store = self._require_text(store_id, "store_id")
        task = self._require_text(task_id, "task_id")
        try:
            safe_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError) as exc:
            raise EvidenceValidationError("limit 必须是整数") from exc
        with self.db.connection() as conn:
            rows = conn.execute(
                """select * from evidence_files
                   where tenant_id=? and store_id=? and task_id=?
                   order by created_at, evidence_id limit ?""",
                (tenant, store, task, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _tenant(self, tenant_id: Optional[str]) -> str:
        value = self.db.tenant_id if tenant_id is None else tenant_id
        if not isinstance(value, str):
            raise EvidenceValidationError("tenant_id 必须是字符串")
        return value

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise EvidenceValidationError(f"{field} 不能为空")
        if "\x00" in value:
            raise EvidenceValidationError(f"{field} 不能包含空字符")
        return value

    @staticmethod
    def _validate_original_name(name: Any) -> str:
        if not isinstance(name, str) or not name.strip():
            raise EvidencePathError("original_name 不能为空")
        if "\x00" in name:
            raise EvidencePathError("original_name 不能包含空字符")
        if name in (".", "..") or any(ch in name for ch in ("/", "\\", ":")):
            raise EvidencePathError(f"original_name 不能包含路径：{name}")
        if Path(name).name != name:
            raise EvidencePathError(f"original_name 不能包含路径：{name}")
        return name

    @staticmethod
    def _validate_evidence_id(evidence_id: Any) -> str:
        if not isinstance(evidence_id, str) or not _EVIDENCE_ID_RE.fullmatch(evidence_id):
            raise EvidencePathError("evidence_id 只能包含字母、数字、下划线和连字符")
        return evidence_id

    @staticmethod
    def _coerce_bytes(data: Any) -> bytes:
        if isinstance(data, bytes):
            return data
        if isinstance(data, (bytearray, memoryview)):
            return bytes(data)
        reader = getattr(data, "read", None)
        if callable(reader):
            try:
                value = reader(MAX_EVIDENCE_BYTES + 1)
            except Exception as exc:
                raise EvidenceValidationError(f"读取图片数据失败：{exc}") from exc
            if isinstance(value, (bytes, bytearray, memoryview)):
                return bytes(value)
        raise EvidenceValidationError("data 必须是 bytes、bytearray、memoryview 或二进制流")

    @staticmethod
    def _detect_mime(data: bytes) -> Optional[str]:
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return None

    @staticmethod
    def _normalize_mime(mime_type: Optional[str]) -> Optional[str]:
        if mime_type is None:
            return None
        if not isinstance(mime_type, str):
            raise EvidenceMimeError("mime_type 必须是字符串")
        value = mime_type.split(";", 1)[0].strip().lower()
        value = MIME_ALIASES.get(value, value)
        if value not in MIME_EXTENSIONS:
            raise EvidenceMimeError("只支持 image/jpeg、image/png、image/webp")
        return value

    def _resolve_stored_path(self, stored_path: Any) -> Path:
        if not isinstance(stored_path, str) or not stored_path:
            raise EvidencePathError("stored_path 不能为空")
        if stored_path in (".", "..") or "\x00" in stored_path:
            raise EvidencePathError(f"stored_path 非法：{stored_path}")
        if any(ch in stored_path for ch in ("/", "\\", ":")):
            raise EvidencePathError(f"stored_path 不能包含路径：{stored_path}")
        candidate = (self.root_dir / stored_path).resolve()
        try:
            candidate.relative_to(self.root_dir)
        except ValueError as exc:
            raise EvidencePathError(f"stored_path 越出证据根目录：{stored_path}") from exc
        if candidate.parent != self.root_dir:
            raise EvidencePathError(f"stored_path 必须位于证据根目录直属层级：{stored_path}")
        return candidate

    # 兼容更明确的方法名，便于调用方阅读。
    save_evidence = save
    read_evidence = read
    get_evidence = metadata
    query_evidence = list_evidence


__all__ = [
    "MAX_EVIDENCE_BYTES",
    "DEFAULT_EVIDENCE_ROOT",
    "EvidenceError",
    "EvidenceValidationError",
    "EvidenceMimeError",
    "EvidenceSizeError",
    "EvidencePathError",
    "EvidenceNotFoundError",
    "EvidenceIntegrityError",
    "EvidenceStorageError",
    "EvidenceStore",
]