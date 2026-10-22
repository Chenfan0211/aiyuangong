#!/usr/bin/env python3
"""长期记忆（第 9 期）：三类记忆的抽取、去重与召回。

边界（与总方案第 9 期一致）：
1. **三类记忆**：`profile`（画像）/ `preference`（偏好）/ `fact`（事实）。
2. **按租户 / 门店 / 用户 / 角色隔离**：召回只在本门店 + 本用户可见范围内进行。
3. **去重**：同 (租户, 门店, 用户, 类型, 内容哈希) 只保留一条，重复抽取只刷新 `updated_at` 与出现次数。
4. **不做规则侧改动**：规则调参已在第 3 期完成，本模块只沉淀记忆。

抽取是**规则式**的（可解释、可测试、无模型依赖）；未命中的句子不会产生记忆。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from db import Database

MEMORY_KINDS = ("profile", "preference", "fact")

_EXTRA_PATTERNS: tuple[tuple[str, str], ...] = (
    ("preference", r"(?:我|本人)?(?:比较)?(?:喜欢|偏好|习惯|不喜欢|不要|不想)(?P<value>[^。；;\n]{1,40})"),
    ("profile", r"(?:我是|本人是|我的角色是|我负责|我在)(?P<value>[^。；;\n]{1,40})"),
    ("fact", r"(?:门店|我们店)(?:的)?(?P<value>[^。；;\n]{1,60})"),
)


class MemoryError(ValueError):
    """记忆的参数或状态错误。"""


def extract_memories(text: Any) -> list[dict[str, str]]:
    """规则式抽取：返回 `[{kind, value}]`，同一句里同类型只取第一条。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    found: dict[str, str] = {}
    for kind, pattern in _EXTRA_PATTERNS:
        if kind in found:
            continue
        match = re.search(pattern, raw)
        if not match:
            continue
        value = match.group("value").strip(" ，,。；;")
        if value:
            found[kind] = f"{match.group(0).split(value)[0].strip()}{value}"
    return [{"kind": kind, "value": value} for kind, value in found.items()]


def normalize(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "")).lower()
    return text.strip("，,。；;：:")


class MemoryStore:
    """`memory_records` 的读写（SQLite / PostgreSQL 双引擎）。"""

    def __init__(self, db_target: Any, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)
        self.tenant_id = tenant_id
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
                create table if not exists memory_records (
                  tenant_id text not null default '',
                  memory_id text not null,
                  store_id text not null,
                  user_id text not null default '',
                  role text not null default '',
                  kind text not null check (kind in ('profile','preference','fact')),
                  value text not null,
                  content_hash text not null,
                  hits integer not null default 1,
                  source text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  primary key (tenant_id, memory_id),
                  unique (tenant_id, store_id, user_id, kind, content_hash)
                );
                create index if not exists idx_memory_scope
                  on memory_records(tenant_id, store_id, user_id, kind);
                """)

    def _require_text(self, value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise MemoryError(f"{field} 不能为空")
        return text

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _hash(kind: str, value: str) -> str:
        return hashlib.sha256(f"{kind}:{normalize(value)}".encode("utf-8")).hexdigest()[:32]

    def remember(self, store_id: str, user_id: str, kind: str, value: str, *,
                 role: str = "", source: str = "") -> dict[str, Any]:
        """写入一条记忆；重复（同内容）只累加命中次数，不新增记录。"""
        store_id = self._require_text(store_id, "store_id")
        user_id = str(user_id or "")
        if kind not in MEMORY_KINDS:
            raise MemoryError(f"kind 必须是 {'、'.join(MEMORY_KINDS)} 之一，收到：{kind!r}")
        value = self._require_text(value, "value")
        digest = self._hash(kind, value)
        moment = self._now()
        with self._connection() as conn:
            existing = conn.execute(
                "select * from memory_records where tenant_id=? and store_id=? and user_id=? "
                "and kind=? and content_hash=?",
                (self.tenant_id, store_id, user_id, kind, digest)).fetchone()
            if existing:
                conn.execute(
                    "update memory_records set hits=hits+1, updated_at=?, role=?, value=? "
                    "where tenant_id=? and memory_id=?",
                    (moment, str(role or existing["role"]), value, self.tenant_id,
                     existing["memory_id"]))
                memory_id = existing["memory_id"]
                return_after_commit = True
            else:
                memory_id = f"mem-{uuid.uuid4().hex[:12]}"
                return_after_commit = False
                conn.execute(
                    """insert into memory_records (tenant_id,memory_id,store_id,user_id,role,kind,value,
                       content_hash,hits,source,created_at,updated_at)
                       values (?,?,?,?,?,?,?,?,1,?,?,?)""",
                    (self.tenant_id, memory_id, store_id, user_id, str(role or ""), kind, value,
                     digest, str(source or ""), moment, moment))
        # 事务提交后再读回，避免读到未提交前的旧值
        return self.get(memory_id)

    def absorb(self, store_id: str, user_id: str, text: str, *, role: str = "",
               source: str = "") -> list[dict[str, Any]]:
        """从一段文本抽取并保存记忆（未命中则返回空列表）。"""
        return [self.remember(store_id, user_id, item["kind"], item["value"], role=role, source=source)
                for item in extract_memories(text)]

    def get(self, memory_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from memory_records where tenant_id=? and memory_id=?",
                               (self.tenant_id, str(memory_id))).fetchone()
        if not row:
            raise MemoryError(f"记忆不存在：{memory_id}")
        return dict(row)

    def recall(self, store_id: str, user_id: Optional[str] = None, *,
               kind: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        """召回：按租户 + 门店（可选再按用户与类型）隔离。"""
        store_id = self._require_text(store_id, "store_id")
        if kind is not None and kind not in MEMORY_KINDS:
            raise MemoryError(f"kind 必须是 {'、'.join(MEMORY_KINDS)} 之一，收到：{kind!r}")
        sql = "select * from memory_records where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, store_id]
        if user_id:
            sql += " and user_id=?"
            params.append(str(user_id))
        if kind:
            sql += " and kind=?"
            params.append(kind)
        sql += " order by updated_at desc, memory_id limit ?"
        params.append(int(limit))
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def forget(self, memory_id: str) -> dict[str, Any]:
        record = self.get(memory_id)
        with self._connection() as conn:
            conn.execute("delete from memory_records where tenant_id=? and memory_id=?",
                         (self.tenant_id, str(memory_id)))
        return record