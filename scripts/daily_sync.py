#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日运维调度入口：备份 → 导入 → 质量 → 派单 → 四类通知 → 状态摘要。

脚本本身不实现业务逻辑，只把现有命令构造成可重试的 ``Step``，再由
``OpsRunner`` 统一负责锁、重试、ops_runs 落库和失败告警。这样 Windows
计划任务与 Linux cron 调用的是同一条执行链。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops_runner import DEFAULT_STEP_NAMES, Step, StepFailure, build_alerter  # noqa: E402
from ops_runner import OpsRunner  # noqa: E402

NOTIFY_MESSAGE_KINDS = ("daily", "overdue", "approvals", "quality")


def _env_path(name: str, fallback: Path) -> Path:
    return Path(os.getenv(name) or fallback)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="门店运营每日统一运维调度")
    parser.add_argument("--python", default=sys.executable, help="执行子命令的 Python 解释器")
    parser.add_argument("--config", type=Path,
                        default=_env_path("STORE_AGENT_CONFIG", ROOT / "store_config.json"))
    parser.add_argument("--metrics-db", default=os.getenv(
        "STORE_AGENT_METRICS_DB", str(ROOT / "data" / "platform_metrics.db")))
    parser.add_argument("--store-db", default=os.getenv(
        "STORE_AGENT_STORE_DB", str(ROOT / "data" / "store_ops.db")))
    parser.add_argument("--store-id", default=os.getenv("STORE_AGENT_STORE_ID"))
    parser.add_argument("--tenant-id", default=os.getenv("STORE_AGENT_TENANT_ID"))
    parser.add_argument("--inbox", type=Path, default=_env_path(
        "STORE_AGENT_IMPORT_INBOX", ROOT / "imports" / "inbox"))
    parser.add_argument("--backup-dir", type=Path, default=_env_path(
        "STORE_AGENT_BACKUP_DIR", ROOT / "data" / "backup"))
    parser.add_argument("--outbox-dir", type=Path, default=_env_path(
        "STORE_AGENT_OUTBOX_DIR", ROOT / "data" / "outbox"))
    parser.add_argument("--lock-file", type=Path, default=_env_path(
        "STORE_AGENT_OPS_LOCK", ROOT / "data" / "ops" / "daily_sync.lock"))
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--days", type=int, default=30, help="质量体检与摘要窗口天数")
    parser.add_argument("--base-url", default=os.getenv(
        "STORE_AGENT_BASE_URL", "http://127.0.0.1:8766"))
    parser.add_argument("--alert-channel", default=os.getenv("STORE_AGENT_ALERT_CHANNEL") or None,
                        choices=("outbox", "feishu", "wecom"))
    parser.add_argument("--allow-degraded", action="store_true",
                        help="缺数时仍生成计划；必须由 owner 显式调用并写审计")
    parser.add_argument("--include-financial", action="store_true",
                        help="通知中包含 GMV/退款字段；默认不发送给群聊")
    parser.add_argument("--fail-fast", action="store_true", help="某一步耗尽重试后停止后续步骤")
    parser.add_argument("--dry-run", action="store_true", help="只打印固定步骤顺序，不执行")
    parser.add_argument("--job-name", default="daily_sync")
    return parser


def _config_ids(path: Path) -> tuple[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    return str(raw.get("tenant_id") or ""), str(raw.get("store_id") or "")


def _command(args: Sequence[Any], *, step_name: str) -> dict[str, Any]:
    command = [str(item) for item in args]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    details = {
        "step": step_name,
        "command": command,
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-4000:],
    }
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()[-1000:]
        raise StepFailure(f"{step_name} 命令失败（exit={completed.returncode}）：{message}")
    return details


def build_backup_commands(args: argparse.Namespace) -> list[list[str]]:
    """构造加密备份命令及 daily/monthly 保留清理命令。"""
    python = str(getattr(args, "python", sys.executable))
    config = Path(getattr(args, "config"))
    store_db = str(getattr(args, "store_db"))
    tenant_id = str(getattr(args, "tenant_id", "") or "")
    store_id = str(getattr(args, "store_id", "") or "")
    evidence_dir = _env_path("STORE_AGENT_EVIDENCE_DIR", ROOT / "evidence")
    backup_dir = Path(getattr(args, "backup_dir"))
    backup_manager = ROOT / "backup_manager.py"

    backup = [
        python, str(backup_manager), "backup",
        "--db", store_db,
        "--tenant-id", tenant_id,
        "--store-id", store_id,
        "--evidence-dir", str(evidence_dir),
        "--config", str(config),
        "--local-root", str(backup_dir),
        "--daily-keep", "30",
        "--monthly-keep", "12",
    ]
    prune_daily = [
        python, str(backup_manager), "prune",
        "--category", "daily", "--keep", "30",
        "--local-root", str(backup_dir),
    ]
    prune_monthly = [
        python, str(backup_manager), "prune",
        "--category", "monthly", "--keep", "12",
        "--local-root", str(backup_dir),
    ]
    return [backup, prune_daily, prune_monthly]


def _run_many(commands: Sequence[tuple[str, Sequence[Any]]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, command in commands:
        try:
            results.append(_command(command, step_name=name))
        except StepFailure as exc:
            errors.append(str(exc))
    if errors:
        raise StepFailure("；".join(errors))
    return {"commands": results}


def build_default_steps(args: argparse.Namespace) -> list[Step]:
    """构造固定顺序的每日步骤；调用方可以注入替身测试 runner。"""
    python = str(getattr(args, "python", sys.executable))
    config = Path(getattr(args, "config"))
    metrics_db = str(getattr(args, "metrics_db"))
    store_db = str(getattr(args, "store_db"))
    tenant_id, config_store_id = _config_ids(config)
    tenant_id = str(getattr(args, "tenant_id", "") or tenant_id)
    store_id = str(getattr(args, "store_id") or config_store_id or "")
    evidence_dir = _env_path("STORE_AGENT_EVIDENCE_DIR", ROOT / "evidence")
    backup_manager = ROOT / "backup_manager.py"
    inbox = Path(getattr(args, "inbox"))
    backup_dir = Path(getattr(args, "backup_dir"))
    days = int(getattr(args, "days", 30))
    allow_degraded = bool(getattr(args, "allow_degraded", False))
    include_financial = bool(getattr(args, "include_financial", False))
    base_url = str(getattr(args, "base_url", "http://127.0.0.1:8766"))

    store_cli = ROOT / "store_cli.py"
    platform_sync = ROOT / "platform_sync.py"
    store_common = [python, store_cli, "--config", config, "--metrics-db", metrics_db,
                    "--store-db", store_db]

    def backup_action() -> dict[str, Any]:
        backup_dir.mkdir(parents=True, exist_ok=True)
        commands = build_backup_commands(args)
        return _run_many([
            ("backup", commands[0]),
            ("backup:prune-daily", commands[1]),
            ("backup:prune-monthly", commands[2]),
        ])

    def import_action() -> dict[str, Any]:
        return _command(
            [python, platform_sync, "--db", metrics_db, "--config", config,
             "--store-id", store_id, "import-dir", "--inbox", inbox],
            step_name="import",
        )

    def quality_action() -> dict[str, Any]:
        return _command(
            [python, platform_sync, "--db", metrics_db, "--config", config,
             "--store-id", store_id, "data-quality", "--days", str(days)],
            step_name="data-quality",
        )

    def plan_action() -> dict[str, Any]:
        command = [*store_common, "plan", "--date", "today", "--by", "ops:daily_sync"]
        if allow_degraded:
            command.append("--allow-degraded")
        return _command(command, step_name="plan")

    def notify_action() -> dict[str, Any]:
        commands: list[tuple[str, Sequence[Any]]] = []
        for kind in NOTIFY_MESSAGE_KINDS:
            command: list[Any] = [
                *store_common, "notify", "--kind", kind, "--base-url", base_url,
            ]
            if not include_financial:
                command.append("--no-financial")
            commands.append((f"notify:{kind}", command))
        return _run_many(commands)

    def summary_action() -> dict[str, Any]:
        return _run_many([
            ("status", [python, platform_sync, "--db", metrics_db, "--config", config,
                        "--store-id", store_id, "status"]),
            ("summary", [python, platform_sync, "--db", metrics_db, "--config", config,
                         "--store-id", store_id, "summary", "--days", str(days)]),
        ])

    return [
        Step("backup", backup_action),
        Step("import", import_action),
        Step("data-quality", quality_action),
        Step("plan", plan_action),
        Step("notify", notify_action),
        Step("summary", summary_action),
    ]


def _write_log(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_tenant, config_store = _config_ids(Path(args.config))
    args.tenant_id = str(args.tenant_id or config_tenant)
    args.store_id = str(args.store_id or config_store or "WH-LION-001")

    if args.dry_run:
        print(json.dumps({
            "job_name": args.job_name,
            "steps": list(DEFAULT_STEP_NAMES),
            "store_id": args.store_id,
            "tenant_id": args.tenant_id,
        }, ensure_ascii=True, indent=2))
        return 0

    steps = build_default_steps(args)
    alerter = build_alerter(args.outbox_dir, channel=args.alert_channel)
    runner = OpsRunner(
        args.store_db,
        tenant_id=args.tenant_id,
        store_id=args.store_id,
        steps=steps,
        alerter=alerter,
        lock_path=args.lock_file,
        job_name=args.job_name,
        fail_fast=args.fail_fast,
    )
    report = runner.run()
    log_file = args.log_file or (ROOT / "data" / "logs" /
                                 f"daily_sync_{date.today():%Y%m%d}.log")
    _write_log(Path(log_file), report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return int(report.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
