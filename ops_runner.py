# -*- coding: utf-8 -*-
"""统一运维任务调度器。

设计边界：
* 调度器只负责锁、步骤顺序、重试、落库和失败告警；
* 真实业务动作由调用方以 ``Step`` 注入，因此测试和以后迁移到 Python
  调度脚本都不需要绑定 subprocess；
* SQLite 使用原子文件锁，PostgreSQL 使用 advisory lock；锁不可用时直接
  返回 skipped，重复触发不会再次执行任何步骤。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

from db import Database
from ops_store import OpsStore


DEFAULT_STEP_NAMES = ("backup", "import", "data-quality", "plan", "notify", "summary")
DEFAULT_STEPS = DEFAULT_STEP_NAMES


def now_iso() -> str:
    return datetime.now().isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Step:
    """一个可重试的运维步骤。"""

    name: str
    action: Callable[[], Any]
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("step.name 不能为空")
        if int(self.max_attempts) < 1:
            raise ValueError("step.max_attempts 必须大于等于 1")


@dataclass(frozen=True)
class StepFailure(Exception):
    """步骤返回的结构化失败。"""

    message: str

    def __str__(self) -> str:
        return self.message


def _normalise_steps(steps: Iterable[Any]) -> list[Step]:
    normalised: list[Step] = []
    for item in steps:
        if isinstance(item, Step):
            normalised.append(item)
        elif isinstance(item, tuple) and len(item) >= 2:
            normalised.append(Step(str(item[0]), item[1],
                                   int(item[2]) if len(item) > 2 else 3))
        else:
            raise ValueError(f"无法识别的步骤：{item!r}")
    return normalised


def _first_value(row: Any, key: str) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    if isinstance(row, (list, tuple)):
        return row[0]
    return row


def _advisory_key(namespace: str, job_name: str, store_id: str) -> int:
    raw = f"{namespace}:{job_name}:{store_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


class FileLock:
    """跨进程 SQLite 文件锁（O_CREAT|O_EXCL 原子创建）。"""

    def __init__(self, path: Any, *, stale_after_seconds: Optional[float] = None):
        self.path = Path(path)
        self.stale_after_seconds = stale_after_seconds

    def _try_remove_stale(self) -> bool:
        if self.stale_after_seconds is None:
            return False
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        if age <= float(self.stale_after_seconds):
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _acquire(self) -> Optional[str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}-{time.time_ns()}"
        for attempt in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if attempt == 0 and self._try_remove_stale():
                    continue
                return None
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            return token
        return None

    def _release(self, token: str) -> None:
        try:
            current = self.path.read_text(encoding="ascii")
        except FileNotFoundError:
            return
        except OSError:
            return
        if current != token:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    @contextmanager
    def hold(self) -> Iterator[bool]:
        token = self._acquire()
        if token is None:
            yield False
            return
        try:
            yield True
        finally:
            self._release(token)


class PostgresAdvisoryLock:
    """PostgreSQL 会话级 advisory lock；同一连接内执行完整任务。"""

    def __init__(self, db_target: Any, job_name: str = "daily_sync",
                 store_id: str = "", *, namespace: str = "store_ops"):
        target = getattr(db_target, "db", db_target)
        self.db = target if isinstance(target, Database) else Database(target)
        self.job_name = str(job_name)
        self.store_id = str(store_id or "")
        self.key = _advisory_key(namespace, self.job_name, self.store_id)

    @contextmanager
    def hold(self) -> Iterator[bool]:
        with self.db.connection() as conn:
            row = conn.execute("select pg_try_advisory_lock(?)", (self.key,)).fetchone()
            acquired = bool(_first_value(row, "pg_try_advisory_lock"))
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                conn.execute("select pg_advisory_unlock(?)", (self.key,))


class OutboxAlerter:
    """无第三方密钥时的默认告警出口：写 JSON 文件，不触碰网络。"""

    def __init__(self, outbox_dir: Any):
        self.outbox_dir = Path(outbox_dir)

    def send(self, event: dict[str, Any]) -> dict[str, Any]:
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        token = hashlib.sha256(json.dumps(event, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()[:8]
        path = self.outbox_dir / f"ops-alert-{stamp}-{token}.json"
        path.write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"sent": True, "channel": "outbox", "status": "outbox", "path": str(path)}


class WebhookAlerter:
    """可选的飞书/企微 webhook 出口；未配置时不实例化。"""

    def __init__(
        self,
        channel: str,
        webhook_url: str,
        *,
        timeout: int = 10,
        sender: Optional[Callable[[str, bytes, int], tuple[int, Any]]] = None,
    ):
        if channel not in {"feishu", "wecom"}:
            raise ValueError(f"不支持的告警通道：{channel}")
        self.channel = channel
        self.webhook_url = webhook_url
        self.timeout = timeout
        self._sender = sender or self._post

    @staticmethod
    def _post(url: str, payload: bytes, timeout: int) -> tuple[int, Any]:
        request = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return response.status, raw
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", errors="replace")

    def send(self, event: dict[str, Any]) -> dict[str, Any]:
        text = (
            f"[门店运维告警] {event.get('job_name', '-')} / {event.get('step_name', '-')}\n"
            f"状态：{event.get('status', 'failed')}\n"
            f"错误：{event.get('error') or '-'}"
        )
        if self.channel == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
        else:
            payload = {"msgtype": "markdown", "markdown": {"content": text}}
        status, body = self._sender(
            self.webhook_url,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            self.timeout,
        )
        return {"sent": 200 <= int(status) < 300, "channel": self.channel,
                "http_status": int(status), "response": str(body)[:500]}


def build_alerter(outbox_dir: Any = None, *, channel: Optional[str] = None) -> Any:
    """按环境变量选择告警出口；没有 webhook 时始终回退到 outbox。"""
    configured = (channel or os.getenv("STORE_AGENT_ALERT_CHANNEL", "")).strip().lower()
    feishu = os.getenv("FEISHU_WEBHOOK_URL") or os.getenv("STORE_AGENT_FEISHU_WEBHOOK_URL")
    wecom = os.getenv("WECOM_WEBHOOK_URL") or os.getenv("STORE_AGENT_WECOM_WEBHOOK_URL")
    if configured in {"feishu", "wecom"}:
        url = feishu if configured == "feishu" else wecom
        if url:
            return WebhookAlerter(configured, url)
    elif not configured:
        if feishu:
            return WebhookAlerter("feishu", feishu)
        if wecom:
            return WebhookAlerter("wecom", wecom)
    directory = outbox_dir or os.getenv("STORE_AGENT_OUTBOX_DIR") or Path("data") / "outbox"
    return OutboxAlerter(directory)


class OpsRunner:
    """按固定步骤列表执行一次运维任务，并将每次尝试写入 ``ops_runs``。"""

    def __init__(
        self,
        store: Any,
        *,
        steps: Optional[Sequence[Any]] = None,
        alerter: Any = None,
        lock_path: Any = None,
        job_name: str = "daily_sync",
        fail_fast: bool = False,
        retry_delay: float = 0.0,
        tenant_id: str = "",
        store_id: str = "",
        consume_retries: bool = True,
    ):
        if isinstance(store, OpsStore):
            self.store = store
        else:
            self.store = OpsStore(store, tenant_id=tenant_id, store_id=store_id)
        self.steps = _normalise_steps(steps or [])
        self.lock_path = Path(lock_path) if lock_path else None
        self.job_name = str(job_name)
        self.fail_fast = bool(fail_fast)
        self.retry_delay = max(0.0, float(retry_delay))
        self.consume_retries_on_run = bool(consume_retries)
        default_outbox = None
        if self.lock_path is None and self.store.db.dialect == "sqlite":
            default_outbox = Path(self.store.db.target).parent / "outbox"
        self.alerter = alerter if alerter is not None else build_alerter(default_outbox)

    def _lock(self, job_name: str):
        if self.store.db.dialect == "postgresql":
            return PostgresAdvisoryLock(
                self.store.db, job_name, self.store.store_id,
            ).hold()
        path = self.lock_path or (Path(self.store.db.target).parent / "ops_daily.lock")
        return FileLock(path).hold()

    @staticmethod
    def _details(value: Any) -> Any:
        if value is None:
            return {}
        try:
            json.dumps(value, ensure_ascii=True)
            return value
        except (TypeError, ValueError):
            return {"repr": repr(value)}

    @staticmethod
    def _interpret(value: Any) -> tuple[bool, Any, Optional[str]]:
        if isinstance(value, StepFailure):
            return False, {}, str(value)
        if value is False:
            return False, {}, "step returned False"
        if isinstance(value, dict):
            details = value
            if value.get("ok") is False or str(value.get("status", "")).lower() in {"failed", "error"}:
                return False, details, str(value.get("error") or value.get("message") or "step failed")
            return True, details, None
        return True, {"result": value}, None

    def _retry_alert(self, request: dict[str, Any], run_id: Optional[int], error: str) -> str:
        status = self._alert({
            "job_name": request["job_name"],
            "step_name": request["step_name"],
            "status": "dead_letter",
            "retry_request_id": request["id"],
            "source_run_id": request["source_run_id"],
            "attempt": request["attempt"],
            "error": error,
        })
        if run_id is not None:
            self.store.set_alert_status(int(run_id), status)
        return status

    def _execute_retry_request(
        self,
        request: dict[str, Any],
        step_map: dict[str, Step],
    ) -> dict[str, Any]:
        """执行一条已领取的重试请求，只允许调用本 runner 已配置的 Step。"""
        step = step_map.get(str(request["step_name"]))
        base_details = {
            "retry_request_id": request["id"],
            "source_run_id": request["source_run_id"],
            "requested_by": request["requested_by"],
            "step": request["step_name"],
        }
        if step is None:
            error = f"未配置原步骤，拒绝执行任意动作：{request['step_name']}"
            run = self.store.start_run(
                request["job_name"], request["step_name"], int(request["attempt"]),
                details=base_details,
            )
            self.store.finish_run(
                run["id"], status="failed", details=base_details,
                error=error, alert_status="pending",
            )
            dead = self.store.dead_letter_retry_request(request["id"], error)
            alert_status = self._retry_alert(dead, run["id"], error)
            return {
                "retry_request_id": dead["id"],
                "source_run_id": dead["source_run_id"],
                "step_name": dead["step_name"],
                "status": dead["status"],
                "attempt": dead["attempt"],
                "run_id": run["id"],
                "alert_status": alert_status,
                "error": error,
            }

        run = self.store.start_run(
            request["job_name"], step.name, int(request["attempt"]), details=base_details,
        )
        try:
            value = step.action()
            ok, details, error = self._interpret(value)
        except Exception as exc:
            ok = False
            details = {**base_details, "error_type": type(exc).__name__}
            error = f"{type(exc).__name__}: {exc}"
        else:
            details = self._details(details)
            if isinstance(details, dict):
                details = {**base_details, **details}

        if ok:
            self.store.finish_run(
                run["id"], status="success", details=details, alert_status="not_needed",
            )
            finished = self.store.finish_retry_request(request["id"], success=True)
            return {
                "retry_request_id": finished["id"],
                "source_run_id": finished["source_run_id"],
                "step_name": finished["step_name"],
                "status": finished["status"],
                "attempt": finished["attempt"],
                "run_id": run["id"],
                "error": None,
            }

        self.store.finish_run(
            run["id"], status="failed", details=details, error=error, alert_status="pending",
        )
        finished = self.store.finish_retry_request(
            request["id"], success=False, error=error,
        )
        alert_status = None
        if finished["status"] == "dead_letter":
            alert_status = self._retry_alert(finished, run["id"], error or "重试失败")
        return {
            "retry_request_id": finished["id"],
            "source_run_id": finished["source_run_id"],
            "step_name": finished["step_name"],
            "status": finished["status"],
            "attempt": finished["attempt"],
            "run_id": run["id"],
            "alert_status": alert_status,
            "error": error,
        }

    def consume_retries(
        self,
        job_name: Optional[str] = None,
        *,
        steps: Optional[Sequence[Any]] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """消费待执行重试请求；步骤来自 runner 配置，不接受命令参数。"""
        job = str(job_name or self.job_name)
        selected = _normalise_steps(steps) if steps is not None else list(self.steps)
        step_map = {step.name: step for step in selected}
        pending = self.store.list_retry_requests(
            job_name=job, status="pending", limit=limit, ascending=True,
        )
        results: list[dict[str, Any]] = []
        dead_letters: list[int] = []
        for item in pending:
            claimed = self.store.claim_retry_request(item["id"])
            if claimed is None:
                continue
            result = self._execute_retry_request(claimed, step_map)
            results.append(result)
            if result["status"] == "dead_letter":
                dead_letters.append(int(result["retry_request_id"]))
        return {
            "job_name": job,
            "retries": results,
            "dead_letters": dead_letters,
            "exit_code": 1 if dead_letters else 0,
            "error": "重试请求进入死信队列" if dead_letters else None,
        }

    def _alert(self, event: dict[str, Any]) -> str:
        if self.alerter is None:
            return "not_configured"
        try:
            result = self.alerter.send(event)
        except Exception as exc:  # 告警失败不能掩盖原业务失败
            return "failed"
        if isinstance(result, dict):
            if result.get("status"):
                return str(result["status"])
            if result.get("sent") is True or result.get("drafted") is True:
                return "sent"
            return "failed"
        return "sent"

    def run(
        self,
        job_name: Optional[str] = None,
        *,
        steps: Optional[Sequence[Any]] = None,
        fail_fast: Optional[bool] = None,
        consume_retries: Optional[bool] = None,
    ) -> dict[str, Any]:
        job = str(job_name or self.job_name)
        selected = _normalise_steps(steps) if steps is not None else list(self.steps)
        should_consume_retries = (
            self.consume_retries_on_run if consume_retries is None else bool(consume_retries)
        )
        started_at = now_iso()
        with self._lock(job) as acquired:
            if not acquired:
                return {
                    "job_name": job,
                    "status": "skipped",
                    "exit_code": 0,
                    "skipped": True,
                    "started_at": started_at,
                    "finished_at": now_iso(),
                    "steps": [],
                    "retries": [],
                    "retry_dead_letters": [],
                }

            retry_report = (
                self.consume_retries(job, steps=selected)
                if should_consume_retries else
                {"retries": [], "dead_letters": [], "exit_code": 0, "error": None}
            )
            report: dict[str, Any] = {
                "job_name": job,
                "status": "success",
                "exit_code": 0,
                "skipped": False,
                "started_at": started_at,
                "finished_at": None,
                "steps": [],
                "retries": list(retry_report.get("retries") or []),
                "retry_dead_letters": list(retry_report.get("dead_letters") or []),
            }
            overall_failed = bool(retry_report.get("exit_code"))
            first_error: Optional[str] = retry_report.get("error")
            stop_after_failure = self.fail_fast if fail_fast is None else bool(fail_fast)

            for step in selected:
                step_result: dict[str, Any] = {
                    "name": step.name,
                    "status": "failed",
                    "attempts": 0,
                    "run_ids": [],
                    "details": {},
                    "error": None,
                }
                exhausted = False
                for attempt in range(1, int(step.max_attempts) + 1):
                    record = self.store.start_run(
                        job, step.name, attempt,
                        details={"step": step.name},
                    )
                    step_result["attempts"] = attempt
                    step_result["run_ids"].append(record["id"])
                    try:
                        value = step.action()
                        ok, details, error = self._interpret(value)
                    except Exception as exc:
                        ok = False
                        details = {"error_type": type(exc).__name__}
                        error = f"{type(exc).__name__}: {exc}"

                    details = self._details(details)
                    if ok:
                        self.store.finish_run(
                            record["id"], status="success", details=details,
                            alert_status="not_needed",
                        )
                        step_result.update(status="success", details=details, error=None)
                        break

                    self.store.finish_run(
                        record["id"], status="failed", details=details,
                        error=error, alert_status="pending",
                    )
                    step_result.update(status="failed", details=details, error=error)
                    if attempt >= int(step.max_attempts):
                        exhausted = True
                        alert_status = self._alert({
                            "job_name": job,
                            "step_name": step.name,
                            "status": "failed",
                            "attempt": attempt,
                            "error": error,
                            "details": details,
                        })
                        self.store.set_alert_status(record["id"], alert_status)
                        step_result["alert_status"] = alert_status
                    elif self.retry_delay:
                        time.sleep(self.retry_delay)

                report["steps"].append(step_result)
                if exhausted:
                    overall_failed = True
                    if first_error is None:
                        first_error = step_result["error"]
                    if stop_after_failure:
                        break

            report["finished_at"] = now_iso()
            report["status"] = "failed" if overall_failed else "success"
            report["exit_code"] = 1 if overall_failed else 0
            if first_error:
                report["error"] = first_error
            return report


def run_daily_sync(store: Any, steps: Sequence[Any], **kwargs: Any) -> dict[str, Any]:
    """便捷入口：供脚本或测试直接执行一组每日同步步骤。"""
    return OpsRunner(store, steps=steps, job_name=kwargs.pop("job_name", "daily_sync"), **kwargs).run()


__all__ = [
    "DEFAULT_STEPS",
    "DEFAULT_STEP_NAMES",
    "FileLock",
    "OpsRunner",
    "OutboxAlerter",
    "PostgresAdvisoryLock",
    "Step",
    "StepFailure",
    "WebhookAlerter",
    "build_alerter",
    "run_daily_sync",
]
