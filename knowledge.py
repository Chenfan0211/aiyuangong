#!/usr/bin/env python3
"""知识库 RAG（第 6 期）：结构感知检索 + 可回查引用。

边界（与总方案第 6 期一致）：
1. **结构 + 关键词打底**：按 Markdown 标题切章节，章节内切片；检索用标题/章节命中 + 关键词重合度打分。
   **向量检索为后续增强项**（本环境没有 `pgvector`，默认关闭、不引入外部向量库与 RAGFlow）。
2. **引用可回查**：每个引用都带 `base_id / document_id / chunk_id / heading_path`，可回到原文。
3. **无命中就明说**：返回 `has_match=False` 与 `"知识库没有相关内容"`，不编造、不硬凑引用。
4. 三条引用治理规则：**重编号**（按命中顺序从 1 编号）、**截断分档**（按长度分级截断）、**剥离页脚**。
5. 导入幂等：同一知识库内相同内容用 sha256 去重（沿用 `platform_data` 的批次哈希范式）。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid as uuid_module
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from auth import redact
from db import Database

CHUNK_MAX_CHARS = 600
SNIPPET_LEVELS = ((120, 120), (400, 240), (10 ** 9, 400))
BASE_STATUSES = ("active", "archived")
DOC_TYPES = ("text", "markdown")
NO_MATCH_REASON = "知识库没有相关内容"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FOOTER_RE = re.compile(
    r"^\s*(?:第\s*\d+\s*页(?:\s*/\s*共\s*\d+\s*页)?|[-—]{2,}\s*页脚\s*[-—]{2,}|"
    r"©|\(c\)|copyright|本文档仅供内部使用|机密|confidential)\b.*$",
    re.IGNORECASE,
)


class KnowledgeError(ValueError):
    """知识库的参数或状态错误。"""


def split_sections(content: str) -> list[dict[str, Any]]:
    """按 Markdown 标题切章节；没有标题时整篇作为一节。"""
    sections: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            sections.append({"heading_path": [title for _, title in stack], "text": text})

    for raw in str(content or "").splitlines():
        match = _HEADING_RE.match(raw.strip())
        if not match:
            buffer.append(raw)
            continue
        flush()
        buffer.clear()
        level, title = len(match.group(1)), match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    flush()
    if not sections and str(content or "").strip():
        sections.append({"heading_path": [], "text": str(content).strip()})
    return sections


def chunk_sections(sections: Iterable[dict[str, Any]],
                   max_chars: int = CHUNK_MAX_CHARS) -> list[dict[str, Any]]:
    """章节内按长度切片，保持章节路径可回查。"""
    chunks: list[dict[str, Any]] = []
    for section in sections:
        text = str(section.get("text") or "").strip()
        for start in range(0, len(text), max_chars):
            piece = text[start:start + max_chars].strip()
            if piece:
                chunks.append({"heading_path": list(section.get("heading_path") or []), "text": piece})
    return chunks


def _tokens(text: Any) -> set[str]:
    """中文按字符 + 双字组合，英文/数字按词（不引入分词依赖）。"""
    raw = str(text or "")
    words = {word.lower() for word in re.findall(r"[0-9A-Za-z]+", raw)}
    cjk = re.findall(r"[\u4e00-\u9fff]", raw)
    bigrams = {"".join(pair) for pair in zip(cjk, cjk[1:])}
    return words | set(cjk) | bigrams


def strip_footer(text: str) -> str:
    kept = [line for line in str(text or "").splitlines() if not _FOOTER_RE.match(line)]
    return "\n".join(kept).strip()


def trim_snippet(text: str, levels: tuple[tuple[int, int], ...] = SNIPPET_LEVELS) -> str:
    """截断分档：原文越长，允许的片段越短（长文只给更小的窗口）。"""
    body = strip_footer(redact(text))
    limit = levels[-1][1]
    for source_limit, snippet_limit in levels:
        if len(body) <= source_limit:
            limit = snippet_limit
            break
    return body if len(body) <= limit else body[:limit].rstrip() + "…"


class KnowledgeStore:
    """`knowledge_*` 五张表的读写与检索（SQLite / PostgreSQL 双引擎）。"""

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
                create table if not exists knowledge_bases (
                  tenant_id text not null default '',
                  base_id text not null,
                  store_id text not null,
                  base_key text not null,
                  name text not null,
                  description text not null default '',
                  status text not null default 'active' check (status in ('active','archived')),
                  created_at text not null,
                  primary key (tenant_id, base_id),
                  unique (tenant_id, store_id, base_key)
                );
                create table if not exists knowledge_documents (
                  tenant_id text not null default '',
                  document_id text not null,
                  base_id text not null,
                  store_id text not null,
                  title text not null,
                  source_name text not null default '',
                  doc_type text not null default 'text' check (doc_type in ('text','markdown')),
                  content_hash text not null,
                  char_count integer not null default 0,
                  chunk_count integer not null default 0,
                  created_at text not null,
                  primary key (tenant_id, document_id),
                  unique (tenant_id, base_id, content_hash)
                );
                create table if not exists knowledge_chunks (
                  tenant_id text not null default '',
                  chunk_id text not null,
                  document_id text not null,
                  base_id text not null,
                  store_id text not null,
                  heading_path text not null default '[]',
                  text text not null,
                  char_count integer not null default 0,
                  order_index integer not null default 0,
                  primary key (tenant_id, chunk_id)
                );
                create index if not exists idx_knowledge_chunks_base
                  on knowledge_chunks(tenant_id, base_id, order_index);
                create table if not exists knowledge_citations (
                  tenant_id text not null default '',
                  citation_id text not null,
                  store_id text not null,
                  query text not null,
                  document_id text not null,
                  chunk_id text not null,
                  score real not null default 0,
                  created_at text not null,
                  primary key (tenant_id, citation_id)
                );
                create table if not exists knowledge_ingest_jobs (
                  tenant_id text not null default '',
                  job_id text not null,
                  store_id text not null,
                  base_id text not null,
                  document_id text,
                  status text not null check (status in ('started','success','failed')),
                  chunk_count integer not null default 0,
                  error text,
                  created_at text not null,
                  finished_at text,
                  primary key (tenant_id, job_id)
                );
                """)

    def _require_text(self, value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise KnowledgeError(f"{field} 不能为空")
        return text

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ---------------- 知识库 ----------------

    def create_base(self, store_id: str, base_key: str, name: str,
                    description: str = "") -> dict[str, Any]:
        store_id = self._require_text(store_id, "store_id")
        base_key = self._require_text(base_key, "base_key")
        name = self._require_text(name, "name")
        with self._connection() as conn:
            row = conn.execute(
                "select * from knowledge_bases where tenant_id=? and store_id=? and base_key=?",
                (self.tenant_id, store_id, base_key)).fetchone()
            if row:
                return dict(row)
            base_id = f"kb-{uuid_module.uuid4().hex[:12]}"
            conn.execute(
                """insert into knowledge_bases
                   (tenant_id,base_id,store_id,base_key,name,description,status,created_at)
                   values (?,?,?,?,?,?,'active',?)""",
                (self.tenant_id, base_id, store_id, base_key, name, str(description or ""), self._now()))
        return self.get_base(base_id)

    def get_base(self, base_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from knowledge_bases where tenant_id=? and base_id=?",
                               (self.tenant_id, str(base_id))).fetchone()
        if not row:
            raise KnowledgeError(f"知识库不存在：{base_id}")
        return dict(row)

    def list_bases(self, store_id: str) -> list[dict[str, Any]]:
        store_id = self._require_text(store_id, "store_id")
        with self._connection() as conn:
            rows = conn.execute(
                "select * from knowledge_bases where tenant_id=? and store_id=? order by base_key",
                (self.tenant_id, store_id)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["document_count"] = self._scalar(
                "select count(*) as n from knowledge_documents where tenant_id=? and base_id=?",
                (self.tenant_id, item["base_id"]))
            items.append(item)
        return items

    def _scalar(self, sql: str, params: tuple) -> int:
        with self._connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    # ---------------- 文档导入（幂等） ----------------

    def ingest_document(self, store_id: str, base_id: str, title: str, content: str, *,
                        source_name: str = "", doc_type: str = "markdown") -> dict[str, Any]:
        store_id = self._require_text(store_id, "store_id")
        title = self._require_text(title, "title")
        body = str(content or "")
        if not body.strip():
            raise KnowledgeError("content 不能为空")
        if doc_type not in DOC_TYPES:
            raise KnowledgeError(f"doc_type 必须是 {'、'.join(DOC_TYPES)} 之一，收到：{doc_type!r}")
        base = self.get_base(base_id)
        if base["store_id"] != store_id:
            raise KnowledgeError("知识库不属于当前门店")
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._connection() as conn:
            existing = conn.execute(
                "select * from knowledge_documents where tenant_id=? and base_id=? and content_hash=?",
                (self.tenant_id, base_id, content_hash)).fetchone()
        if existing:
            record = dict(existing)
            record["duplicate"] = True
            return record

        job_id = f"kij-{uuid_module.uuid4().hex[:12]}"
        document_id = f"kdoc-{uuid_module.uuid4().hex[:12]}"
        with self._connection() as conn:
            conn.execute(
                """insert into knowledge_ingest_jobs
                   (tenant_id,job_id,store_id,base_id,document_id,status,chunk_count,created_at)
                   values (?,?,?,?,?,'started',0,?)""",
                (self.tenant_id, job_id, store_id, base_id, document_id, self._now()))
        try:
            chunks = chunk_sections(split_sections(body))
            if not chunks:
                raise KnowledgeError("内容里没有可入库的文本")
            with self._connection() as conn:
                conn.execute(
                    """insert into knowledge_documents
                       (tenant_id,document_id,base_id,store_id,title,source_name,doc_type,
                        content_hash,char_count,chunk_count,created_at)
                       values (?,?,?,?,?,?,?,?,?,?,?)""",
                    (self.tenant_id, document_id, base_id, store_id, title, str(source_name or ""),
                     doc_type, content_hash, len(body), len(chunks), self._now()))
                for order, chunk in enumerate(chunks):
                    conn.execute(
                        """insert into knowledge_chunks
                           (tenant_id,chunk_id,document_id,base_id,store_id,heading_path,text,
                            char_count,order_index)
                           values (?,?,?,?,?,?,?,?,?)""",
                        (self.tenant_id, f"kch-{uuid_module.uuid4().hex[:16]}", document_id, base_id,
                         store_id, json.dumps(chunk["heading_path"], ensure_ascii=False),
                         chunk["text"], len(chunk["text"]), order))
                conn.execute(
                    """update knowledge_ingest_jobs set status='success', chunk_count=?, finished_at=?
                       where tenant_id=? and job_id=?""",
                    (len(chunks), self._now(), self.tenant_id, job_id))
        except Exception as exc:                     # noqa: BLE001 - 失败要落账再抛出
            with self._connection() as conn:
                conn.execute(
                    """update knowledge_ingest_jobs set status='failed', error=?, finished_at=?
                       where tenant_id=? and job_id=?""",
                    (str(exc)[:300], self._now(), self.tenant_id, job_id))
            raise
        record = self.get_document(document_id)
        record["duplicate"] = False
        record["job_id"] = job_id
        return record

    def get_document(self, document_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("select * from knowledge_documents where tenant_id=? and document_id=?",
                               (self.tenant_id, str(document_id))).fetchone()
        if not row:
            raise KnowledgeError(f"知识文档不存在：{document_id}")
        return dict(row)

    def list_documents(self, base_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "select * from knowledge_documents where tenant_id=? and base_id=? order by created_at",
                (self.tenant_id, str(base_id))).fetchall()
        return [dict(row) for row in rows]

    # ---------------- 检索 ----------------

    def _candidates(self, store_id: str, base_id: Optional[str]) -> list[dict[str, Any]]:
        sql = ("select c.*, d.title as document_title from knowledge_chunks c "
               "join knowledge_documents d on d.document_id = c.document_id "
               "where c.tenant_id=? and c.store_id=?")
        params: list[Any] = [self.tenant_id, store_id]
        if base_id:
            sql += " and c.base_id=?"
            params.append(str(base_id))
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    @staticmethod
    def score_chunk(query_tokens: set[str], chunk: dict[str, Any]) -> float:
        """结构感知打分：关键词重合度 + 标题/章节命中加权。"""
        if not query_tokens:
            return 0.0
        body_tokens = _tokens(chunk.get("text"))
        heading = chunk.get("heading_path")
        if isinstance(heading, str):
            try:
                heading = json.loads(heading)
            except json.JSONDecodeError:
                heading = []
        heading_text = " ".join(heading or [])
        heading_tokens = _tokens(heading_text)
        overlap = len(query_tokens & (body_tokens | heading_tokens)) / len(query_tokens)
        bonus = 0.2 if query_tokens & heading_tokens else 0.0
        return round(min(1.0, overlap + bonus), 4)

    def search(self, store_id: str, query: str, *, base_id: Optional[str] = None,
               limit: int = 5, min_score: float = 0.15) -> dict[str, Any]:
        """检索：命中就返回可回查引用，没命中就明确说没有内容。"""
        store_id = self._require_text(store_id, "store_id")
        query = self._require_text(query, "query")
        query_tokens = _tokens(query)
        scored = []
        for chunk in self._candidates(store_id, base_id):
            score = self.score_chunk(query_tokens, chunk)
            if score >= float(min_score):
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]["chunk_id"]))
        if not scored:
            return {"store_id": store_id, "query": query, "has_match": False,
                    "reason": NO_MATCH_REASON, "count": 0, "citations": []}
        return {"store_id": store_id, "query": query, "has_match": True, "count": len(scored[:limit]),
                "citations": self.govern_citations(scored[:limit])}

    # ---------------- 引用治理 ----------------

    @staticmethod
    def govern_citations(hits: Iterable[tuple[float, dict[str, Any]]]) -> list[dict[str, Any]]:
        """重编号 + 截断分档 + 剥离页脚，产出可回查的引用列表。"""
        citations = []
        for index, (score, chunk) in enumerate(hits, start=1):
            heading = chunk.get("heading_path")
            if isinstance(heading, str):
                try:
                    heading = json.loads(heading)
                except json.JSONDecodeError:
                    heading = []
            citations.append({
                "index": index,
                "score": score,
                "base_id": chunk.get("base_id"),
                "document_id": chunk.get("document_id"),
                "chunk_id": chunk.get("chunk_id"),
                "document_title": chunk.get("document_title"),
                "heading_path": list(heading or []),
                "snippet": trim_snippet(chunk.get("text") or ""),
            })
        return citations

    def record_citations(self, store_id: str, query: str,
                         citations: Iterable[dict[str, Any]]) -> int:
        """把一次检索的引用落库（便于回查"当时引用了哪一段"）。"""
        store_id = self._require_text(store_id, "store_id")
        rows = list(citations or [])
        if not rows:
            return 0
        moment = self._now()
        with self._connection() as conn:
            for citation in rows:
                conn.execute(
                    """insert into knowledge_citations
                       (tenant_id,citation_id,store_id,query,document_id,chunk_id,score,created_at)
                       values (?,?,?,?,?,?,?,?)""",
                    (self.tenant_id, f"kct-{uuid_module.uuid4().hex[:16]}", store_id, str(query or ""),
                     str(citation.get("document_id") or ""), str(citation.get("chunk_id") or ""),
                     float(citation.get("score") or 0), moment))
        return len(rows)