#!/usr/bin/env python3
"""门店注册表（多租户的唯一真相源，二期批次 8c）。

`stores` / `store_configs` 两个表在批次 1 就画进了数据模型，但一直由 `store_config.json` 代替；
本模块把它们真正写起来，并提供**跨租户守卫**：

* `register(config)`       —— 启动时把门店与配置写入注册表（幂等 upsert）；
* `list_stores(tenant)`    —— 列出某租户下的门店（多店部署的基础）；
* `assert_store_scope()`   —— 同一 `store_id` 不允许出现在两个租户下。

为什么需要守卫：运行时所有业务查询都按 `store_id` 过滤（PostgreSQL 侧另有行级安全兜底），
如果两个租户复用了同一个 `store_id`，SQLite 侧就会出现跨租户可见。启动即拒绝这种配置。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from db import Database

STORE_DDL = """
create table if not exists stores (
  tenant_id text not null default '',
  store_id text not null,
  name text not null,
  timezone text not null default 'Asia/Shanghai',
  status text not null default 'pilot',
  created_at text,
  primary key (tenant_id, store_id)
);
create table if not exists store_configs (
  tenant_id text not null default '',
  store_id text not null,
  config_json text not null,
  updated_at text,
  primary key (tenant_id, store_id)
);
"""


class RegistryError(ValueError):
    """注册表冲突（例如同一 store_id 被两个租户占用）。"""


class RegistryStore:
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
            conn.executescript(STORE_DDL)

    # ---------------- 写入 ----------------

    def register(self, config: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(config.get("tenant_id") or "")
        store_id = str(config.get("store_id") or "")
        if not tenant_id or not store_id:
            raise RegistryError("门店配置缺少 tenant_id 或 store_id")
        self.assert_store_scope(tenant_id, store_id)
        moment = datetime.now().isoformat(timespec="seconds")
        with self._connection() as conn:
            conn.execute(
                """insert into stores(tenant_id,store_id,name,timezone,status,created_at)
                   values(?,?,?,?,?,?)
                   on conflict(tenant_id,store_id) do update set
                     name=excluded.name, timezone=excluded.timezone""",
                (tenant_id, store_id, str(config.get("name") or store_id),
                 str(config.get("timezone") or "Asia/Shanghai"), "pilot", moment),
            )
            conn.execute(
                """insert into store_configs(tenant_id,store_id,config_json,updated_at)
                   values(?,?,?,?)
                   on conflict(tenant_id,store_id) do update set
                     config_json=excluded.config_json, updated_at=excluded.updated_at""",
                (tenant_id, store_id, json.dumps(config, ensure_ascii=False), moment),
            )
        return {"tenant_id": tenant_id, "store_id": store_id,
                "name": str(config.get("name") or store_id), "registered_at": moment}

    def assert_store_scope(self, tenant_id: str, store_id: str) -> None:
        """同一 store_id 不允许被其它租户占用（SQLite 侧门店作用域=租户隔离的前提）。"""
        with self._connection() as conn:
            rows = conn.execute(
                "select tenant_id from stores where store_id=? and tenant_id<>?",
                (store_id, tenant_id or ""),
            ).fetchall()
        if rows:
            owners = "、".join(sorted({row["tenant_id"] for row in rows}))
            raise RegistryError(
                f"门店 {store_id} 已注册在租户 {owners} 下；本部署不允许跨租户复用同一 store_id"
                "（SQLite 侧没有行级安全，靠此守卫保证门店作用域等价于租户隔离）"
            )

    # ---------------- 读取 ----------------

    def list_stores(self, tenant_id: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "select tenant_id,store_id,name,timezone,status from stores"
        params: list[Any] = []
        if tenant_id is not None:
            sql += " where tenant_id=?"
            params.append(tenant_id)
        sql += " order by tenant_id, store_id"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def store_config(self, tenant_id: str, store_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                "select config_json from store_configs where tenant_id=? and store_id=?",
                (tenant_id, store_id),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["config_json"])
