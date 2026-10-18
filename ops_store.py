# -*- coding: utf-8 -*-
"""统一运维任务运行记录存储。

`ops_runs` 由本模块负责建表和读写，避免为了一个运维辅助表修改权威
`schema.sql`。表同时兼容 SQLite 与 PostgreSQL；所有查询都强制带
`tenant_id + store_id`，避免运维页面在多租户部署中误读其它门店记录。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from db import Database


OPS_RUNS_DDL_SQLITE = """
create table if not exists ops_runs (
  id integer primary key autoincrement,
  tenant_id text not null default '',
  store_id text not null default '',
  job_name text not null,
  step_name text not null,
  started_at text not null,
  finished_at text,
  status text not null,
  attempt integer not null default 1,
  details_json text,
  error text,
  alert_status text not null default 'pending',
  created_at text not null
);
create index if not exists idx_ops_runs_scope_job
  on ops_runs(tenant_id, store_id, job_name, started_at);
create index if not exists idx_ops_runs_step
  on ops_runs(job_name, step_name, attempt);
"""

OPS_RUNS_DDL_POSTGRES = """
create table if not exists ops_runs (
  id bigserial primary key,
  tenant_id text not null default '',
  store_id text not null default '',
  job_name text not null,
  step_name text not null,
  started_at text not null,
  finished_at text,
  status text not null,
  attempt integer not null default 1,
  details_json text,
  error text,
  alert_status text not null default 'pending',
  created_at text not null
);
create index if not exists idx_ops_runs_scope_job
  on ops_runs(tenant_id, store_id, job_name, started_at);
create index if not exists idx_ops_runs_step
  on ops_runs(job_name, step_name, attempt);
"""

OPS_RETRY_REQUESTS_DDL_SQLITE = """
create table if not exists ops_retry_requests (
  id integer primary key autoincrement,
  tenant_id text not null default '',
  store_id text not null default '',
  source_run_id integer not null,
  job_name text not null,
  step_name text not null,
  requested_by text not null,
  status text not null default 'pending',
  attempt integer not null default 0,
  max_attempts integer not null default 3,
  last_error text,
  created_at text not null,
  updated_at text not null,
  started_at text,
  finished_at text,
  dead_letter_at text
);
create index if not exists idx_ops_retry_scope
  on ops_retry_requests(tenant_id, store_id, job_name, status, id);
create index if not exists idx_ops_retry_source
  on ops_retry_requests(tenant_id, store_id, source_run_id);
"""

OPS_RETRY_REQUESTS_DDL_POSTGRES = """
create table if not exists ops_retry_requests (
  id bigserial primary key,
  tenant_id text not null default '',
  store_id text not null default '',
  source_run_id integer not null,
  job_name text not null,
  step_name text not null,
  requested_by text not null,
  status text not null default 'pending',
  attempt integer not null default 0,
  max_attempts integer not null default 3,
  last_error text,
  created_at text not null,
  updated_at text not null,
  started_at text,
  finished_at text,
  dead_letter_at text
);
create index if not exists idx_ops_retry_scope
  on ops_retry_requests(tenant_id, store_id, job_name, status, id);
create index if not exists idx_ops_retry_source
  on ops_retry_requests(tenant_id, store_id, source_run_id);
"""


def now_iso() -> str:
    """运维记录统一使用无时区偏移的门店本地 ISO 时间。"""
    return datetime.now().isoformat(timespec="microseconds")


class OpsStoreError(ValueError):
    """运维记录参数或状态错误。"""


class OpsStore:
    """`ops_runs` 的最小仓储接口。"""

    def __init__(
        self,
        db_target: Any,
        tenant_id: str = "",
        store_id: str = "",
        *,
        auto_init: bool = True,
    ):
        self.db = Database(db_target, tenant_id=tenant_id or "")
        self.tenant_id = tenant_id or ""
        self.store_id = store_id or ""
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        if auto_init:
            self._init_schema()

    def _connection(self):
        return self.db.connection()

    def _init_schema(self) -> None:
        if self.db.dialect == "sqlite":
            ddl = OPS_RUNS_DDL_SQLITE + "\n" + OPS_RETRY_REQUESTS_DDL_SQLITE
        else:
            ddl = OPS_RUNS_DDL_POSTGRES + "\n" + OPS_RETRY_REQUESTS_DDL_POSTGRES
        with self._connection() as conn:
            conn.executescript(ddl)

    @staticmethod
    def _decode(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["attempt"] = int(item.get("attempt") or 1)
        item["details"] = json.loads(item.pop("details_json") or "{}")
        return item

    def _one(self, conn: Any, run_id: int) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "select * from ops_runs where id=? and tenant_id=? and store_id=?",
            (int(run_id), self.tenant_id, self.store_id),
        ).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode_retry(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["attempt"] = int(item.get("attempt") or 0)
        item["max_attempts"] = int(item.get("max_attempts") or 3)
        item["source_run_id"] = int(item["source_run_id"])
        return item

    def _one_retry(self, conn: Any, retry_id: int) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "select * from ops_retry_requests where id=? and tenant_id=? and store_id=?",
            (int(retry_id), self.tenant_id, self.store_id),
        ).fetchone()
        return self._decode_retry(row) if row else None

    @staticmethod
    def _json(value: Any) -> str:
        if value is None:
            return "{}"
        if isinstance(value, str):
            try:
                json.loads(value)
                return value
            except json.JSONDecodeError:
                pass
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))

    def start_run(
        self,
        job_name: str,
        step_name: str,
        attempt: int = 1,
        *,
        details: Any = None,
    ) -> dict[str, Any]:
        """写入一次步骤尝试，状态先置为 running。"""
        if not str(job_name).strip():
            raise OpsStoreError("job_name 不能为空")
        if not str(step_name).strip():
            raise OpsStoreError("step_name 不能为空")
        if int(attempt) < 1:
            raise OpsStoreError("attempt 必须大于等于 1")

        moment = now_iso()
        with self._connection() as conn:
            run_id = self.db.insert_returning_id(
                conn,
                """insert into ops_runs(
                     tenant_id,store_id,job_name,step_name,started_at,finished_at,
                     status,attempt,details_json,error,alert_status,created_at
                   ) values(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.tenant_id,
                    self.store_id,
                    str(job_name),
                    str(step_name),
                    moment,
                    None,
                    "running",
                    int(attempt),
                    self._json(details),
                    None,
                    "pending",
                    moment,
                ),
            )
            record = self._one(conn, run_id)
        if record is None:  # pragma: no cover - 同一连接写后读应必然存在
            raise OpsStoreError("写入 ops_runs 后无法回读记录")
        return record

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        details: Any = None,
        error: Optional[str] = None,
        alert_status: Optional[str] = None,
    ) -> dict[str, Any]:
        """结束一次步骤尝试，并返回完整记录。"""
        if status not in {"running", "success", "failed", "skipped"}:
            raise OpsStoreError(f"不支持的运行状态：{status}")
        if alert_status is None:
            alert_status = "not_needed" if status in {"success", "skipped"} else "pending"

        with self._connection() as conn:
            current = self._one(conn, run_id)
            if current is None:
                raise OpsStoreError(f"找不到当前作用域的 ops_runs 记录：{run_id}")
            detail_json = current["details"] if details is None else self._json(details)
            finished_at = now_iso() if status != "running" else current["finished_at"]
            conn.execute(
                """update ops_runs
                   set finished_at=?, status=?, details_json=?, error=?, alert_status=?
                   where id=? and tenant_id=? and store_id=?""",
                (finished_at, status, self._json(detail_json), error, alert_status,
                 int(run_id), self.tenant_id, self.store_id),
            )
            record = self._one(conn, run_id)
        if record is None:  # pragma: no cover
            raise OpsStoreError("更新 ops_runs 后无法回读记录")
        return record

    def set_alert_status(self, run_id: int, status: str) -> dict[str, Any]:
        """仅更新告警状态，供 alerter 返回后回写。"""
        with self._connection() as conn:
            current = self._one(conn, run_id)
            if current is None:
                raise OpsStoreError(f"找不到当前作用域的 ops_runs 记录：{run_id}")
            conn.execute(
                "update ops_runs set alert_status=? where id=? and tenant_id=? and store_id=?",
                (str(status), int(run_id), self.tenant_id, self.store_id),
            )
            record = self._one(conn, run_id)
        if record is None:  # pragma: no cover
            raise OpsStoreError("更新告警状态后无法回读记录")
        return record

    def get_run(self, run_id: int) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            return self._one(conn, run_id)

    def list_runs(
        self,
        *,
        job_name: Optional[str] = None,
        step_name: Optional[str] = None,
        limit: int = 200,
        ascending: bool = True,
    ) -> list[dict[str, Any]]:
        """按当前租户/门店列出运行尝试。"""
        sql = "select * from ops_runs where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, self.store_id]
        if job_name is not None:
            sql += " and job_name=?"
            params.append(str(job_name))
        if step_name is not None:
            sql += " and step_name=?"
            params.append(str(step_name))
        direction = "asc" if ascending else "desc"
        sql += f" order by id {direction} limit ?"
        params.append(max(1, min(int(limit), 5000)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode(row) for row in rows]

    # ---------------- 重试队列 ----------------

    def request_retry(
        self,
        source_run_id: int,
        *,
        requested_by: str,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """创建一次真实重试请求。

        调用方只能引用已存在的失败/跳过运行记录；任务、步骤和参数由原
        ``ops_runs`` 记录决定，不能通过 API 注入任意命令或步骤。
        """
        requester = str(requested_by or "").strip()
        if not requester:
            raise OpsStoreError("requested_by 不能为空")
        attempts = int(max_attempts)
        if attempts < 1 or attempts > 10:
            raise OpsStoreError("max_attempts 必须在 1 到 10 之间")

        with self._connection() as conn:
            source = self._one(conn, int(source_run_id))
            if source is None:
                raise OpsStoreError(f"找不到源运行记录：{source_run_id}")
            source_id = int(source_run_id)
            if source["step_name"] == "retry-request":
                details = source.get("details") or {}
                source_id = int(details.get("source_run_id") or 0)
                requester = str(details.get("requested_by") or requester).strip()
                source = self._one(conn, source_id)
                if source is None:
                    raise OpsStoreError(f"找不到占位重试记录引用的源运行：{source_id}")
            if source["status"] not in {"failed", "skipped"}:
                raise OpsStoreError("只有失败或跳过的运行记录可以重试")
            if source["step_name"] == "retry-request":
                raise OpsStoreError("重试占位记录不能再次作为重试来源")

            existing = conn.execute(
                """select * from ops_retry_requests
                   where tenant_id=? and store_id=? and source_run_id=?
                     and status in ('pending','running')
                   order by id asc limit 1""",
                (self.tenant_id, self.store_id, source_id),
            ).fetchone()
            if existing:
                return self._decode_retry(existing)

            moment = now_iso()
            retry_id = self.db.insert_returning_id(
                conn,
                """insert into ops_retry_requests(
                     tenant_id,store_id,source_run_id,job_name,step_name,requested_by,
                     status,attempt,max_attempts,last_error,created_at,updated_at,
                     started_at,finished_at,dead_letter_at
                   ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.tenant_id,
                    self.store_id,
                    source_id,
                    str(source["job_name"]),
                    str(source["step_name"]),
                    requester,
                    "pending",
                    0,
                    attempts,
                    None,
                    moment,
                    moment,
                    None,
                    None,
                    None,
                ),
            )
            record = self._one_retry(conn, retry_id)
        if record is None:  # pragma: no cover
            raise OpsStoreError("写入 ops_retry_requests 后无法回读记录")
        return record

    def get_retry_request(self, retry_id: int) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            return self._one_retry(conn, int(retry_id))

    def list_retry_requests(
        self,
        *,
        status: Optional[str | Sequence[str]] = None,
        job_name: Optional[str] = None,
        limit: int = 200,
        ascending: bool = True,
    ) -> list[dict[str, Any]]:
        """按当前作用域查询重试队列，供管理 API/页面只读展示。"""
        sql = "select * from ops_retry_requests where tenant_id=? and store_id=?"
        params: list[Any] = [self.tenant_id, self.store_id]
        if job_name is not None:
            sql += " and job_name=?"
            params.append(str(job_name))
        if status is not None:
            statuses = [str(status)] if isinstance(status, str) else [str(item) for item in status]
            if statuses:
                marks = ",".join("?" for _ in statuses)
                sql += f" and status in ({marks})"
                params.extend(statuses)
        direction = "asc" if ascending else "desc"
        sql += f" order by id {direction} limit ?"
        params.append(max(1, min(int(limit), 5000)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode_retry(row) for row in rows]

    def claim_retry_request(self, retry_id: int) -> Optional[dict[str, Any]]:
        """原子领取一个待执行重试；并发消费时最多一个消费者成功。"""
        with self._connection() as conn:
            current = self._one_retry(conn, int(retry_id))
            if current is None or current["status"] != "pending":
                return None
            attempt = int(current["attempt"]) + 1
            moment = now_iso()
            if attempt > int(current["max_attempts"]):
                conn.execute(
                    """update ops_retry_requests
                       set status='dead_letter', updated_at=?, finished_at=?,
                           dead_letter_at=?, last_error=?
                       where id=? and tenant_id=? and store_id=? and status='pending'""",
                    (moment, moment, moment, "超过最大重试次数",
                     int(retry_id), self.tenant_id, self.store_id),
                )
                return self._one_retry(conn, int(retry_id))
            cursor = conn.execute(
                """update ops_retry_requests
                   set status='running', attempt=?, updated_at=?, started_at=?
                   where id=? and tenant_id=? and store_id=? and status='pending'""",
                (attempt, moment, moment, int(retry_id), self.tenant_id, self.store_id),
            )
            if cursor.rowcount != 1:
                return None
            return self._one_retry(conn, int(retry_id))

    def finish_retry_request(
        self,
        retry_id: int,
        *,
        success: bool,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        """完成一次真实重试；失败未超限回到 pending，超限进入 dead_letter。"""
        with self._connection() as conn:
            current = self._one_retry(conn, int(retry_id))
            if current is None:
                raise OpsStoreError(f"找不到重试请求：{retry_id}")
            if current["status"] == "succeeded":
                return current
            if current["status"] != "running":
                raise OpsStoreError("只有 running 状态的重试请求可以完成")
            moment = now_iso()
            if success:
                conn.execute(
                    """update ops_retry_requests
                       set status='succeeded', updated_at=?, finished_at=?,
                           dead_letter_at=NULL, last_error=NULL
                       where id=? and tenant_id=? and store_id=?""",
                    (moment, moment, int(retry_id), self.tenant_id, self.store_id),
                )
            elif int(current["attempt"]) >= int(current["max_attempts"]):
                conn.execute(
                    """update ops_retry_requests
                       set status='dead_letter', updated_at=?, finished_at=?,
                           dead_letter_at=?, last_error=?
                       where id=? and tenant_id=? and store_id=?""",
                    (moment, moment, moment, error, int(retry_id), self.tenant_id, self.store_id),
                )
            else:
                conn.execute(
                    """update ops_retry_requests
                       set status='pending', updated_at=?, finished_at=NULL,
                           dead_letter_at=NULL, last_error=?
                       where id=? and tenant_id=? and store_id=?""",
                    (moment, error, int(retry_id), self.tenant_id, self.store_id),
                )
            record = self._one_retry(conn, int(retry_id))
        if record is None:  # pragma: no cover
            raise OpsStoreError("更新重试请求后无法回读记录")
        return record

    def dead_letter_retry_request(self, retry_id: int, error: str) -> dict[str, Any]:
        """不可执行的重试直接进入死信，例如调度器未配置原步骤。"""
        with self._connection() as conn:
            current = self._one_retry(conn, int(retry_id))
            if current is None:
                raise OpsStoreError(f"找不到重试请求：{retry_id}")
            if current["status"] == "dead_letter":
                return current
            if current["status"] not in {"pending", "running"}:
                raise OpsStoreError("只有未完成的重试请求可以进入死信")
            moment = now_iso()
            conn.execute(
                """update ops_retry_requests
                   set status='dead_letter', updated_at=?, finished_at=?,
                       dead_letter_at=?, last_error=?
                   where id=? and tenant_id=? and store_id=?""",
                (moment, moment, moment, str(error), int(retry_id), self.tenant_id, self.store_id),
            )
            record = self._one_retry(conn, int(retry_id))
        if record is None:  # pragma: no cover
            raise OpsStoreError("更新死信后无法回读记录")
        return record

    # 语法糖：让调用方不必关心 start/finish 两次调用。
    def record_run(
        self,
        job_name: str,
        step_name: str,
        *,
        status: str = "success",
        attempt: int = 1,
        details: Any = None,
        error: Optional[str] = None,
        alert_status: Optional[str] = None,
    ) -> dict[str, Any]:
        record = self.start_run(job_name, step_name, attempt, details=details)
        return self.finish_run(
            record["id"], status=status, details=details, error=error,
            alert_status=alert_status,
        )


__all__ = [
    "OPS_RETRY_REQUESTS_DDL_POSTGRES",
    "OPS_RETRY_REQUESTS_DDL_SQLITE",
    "OPS_RUNS_DDL_POSTGRES",
    "OPS_RUNS_DDL_SQLITE",
    "OpsStore",
    "OpsStoreError",
    "now_iso",
]
