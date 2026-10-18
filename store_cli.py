#!/usr/bin/env python3
"""门店运营命令行：组装快照、生成并落库计划、查询与推进任务。

    python store_cli.py snapshot --date today      # 打印"由数据+配置"组装出的快照（排查用）
    python store_cli.py plan --date today          # 生成当日计划并落库（可重复执行，不重复派单）
    python store_cli.py tasks --status open        # 查看任务
    python store_cli.py evidence <task_id> --type 截图 --ref shot-001 --by 员工小李
    python store_cli.py set-status <task_id> --to accepted --by 员工小李
    python store_cli.py overdue                    # 把已超期任务标记为 overdue
    python store_cli.py approvals --status pending    # 审批队列
    python store_cli.py decide <approval_id> --to approved --by 张三
    python store_cli.py dashboard --days 7            # 经营看板数据（JSON）
    python store_cli.py weekly / monthly              # 周报 / 月报文本
    python store_cli.py notify --kind daily            # 推送日报（默认写 data/outbox 草稿）
    python store_cli.py notify --kind approvals --force # 强制重发待审批提醒
    python store_cli.py notify-history                  # 查看推送记录
    python store_cli.py backup / restore --in <file> / drill   # 备份、恢复、恢复演练
    python store_cli.py retention-plan                # 保留期清理计划（只统计）
    python store_cli.py purge --apply --export data/archive   # 先归档再删除
    python store_cli.py gate-check --dsn "$DSN"        # P0 闸门自检
    python store_cli.py audit --limit 30              # 审计流水（--tool-runs 只看工具留痕）
    python store_cli.py auth-init --username manager01 --display-name 张三 --role manager

门店与配置来源：store_config.json（store_id 以它为准，可用 --store-id 覆盖）。
账号运维：`auth-init` 建账号、`auth-passwd` 重置口令、`auth-list` 看成员、`auth-audit` 查登录与权限审计。
数据分库：平台数据 data/platform_metrics.db（可由导出文件重建）；任务/证据 data/store_ops.db（需单独备份）。
"""

from __future__ import annotations

import argparse
import getpass
import os
from datetime import datetime
import json
import sys
from pathlib import Path

from auth import ROLES, AuthStore
from governance import ApprovalStore, AuditCenter, ToolRunLog, apply_decision
from reporting import StoreReporter
from ops_backup import create_backup, drill, restore_backup, verify_backup
from notify import (CHANNELS, DEFAULT_BASE_URL, KINDS, Message, NotificationLedger, Notifier,
                    build_approval_digest, build_daily_digest, build_overdue_digest,
                    build_quality_alert, render_payload)
from registry import RegistryStore
from retention import plan_purge, policy_from_config, purge as purge_retention
from security import gate_report

from platform_data import PlatformMetricStore
from prototype import generate_daily_plan
from snapshot_builder import SnapshotError, build_snapshot, load_config
from task_store import TaskStore, TaskStoreError

ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="门店运营命令行（快照 / 计划 / 任务）")
    parser.add_argument("--config", type=Path, default=ROOT / "store_config.json", help="门店配置文件")
    parser.add_argument("--metrics-db", default=str(ROOT / "data" / "platform_metrics.db"),
                        help="平台指标库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--store-db", default=str(ROOT / "data" / "store_ops.db"),
                        help="任务/证据/审计/身份库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--store-id", default=None, help="默认取 store_config.json 中的 store_id")
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="打印组装出的经营快照")
    snapshot.add_argument("--date", default="today", help="today / yesterday / YYYY-MM-DD")

    plan = sub.add_parser("plan", help="生成当日计划并落库（幂等）")
    plan.add_argument("--date", default="today", help="today / yesterday / YYYY-MM-DD")

    tasks = sub.add_parser("tasks", help="查询任务")
    tasks.add_argument("--status", default=None, help="open 或具体状态：proposed/ready/accepted/done/verified/rejected/overdue/canceled")
    tasks.add_argument("--assignee", default=None, help="按负责人过滤")
    tasks.add_argument("--limit", type=int, default=50)

    evidence = sub.add_parser("evidence", help="为任务提交证据")
    evidence.add_argument("task_id")
    evidence.add_argument("--type", required=True, help="证据类型，例如：脱敏订单号 / 平台回复截图 / 页面快照")
    evidence.add_argument("--ref", required=True, help="证据引用（脱敏单号、文件路径或对象存储 key）")
    evidence.add_argument("--by", required=True, help="提交人")
    evidence.add_argument("--note", default=None)

    status = sub.add_parser("set-status", help="推进任务状态")
    status.add_argument("task_id")
    status.add_argument("--to", required=True)
    status.add_argument("--by", required=True)
    status.add_argument("--reason", default="")

    sub.add_parser("overdue", help="把超过截止时间的任务标记为 overdue")

    retention = sub.add_parser("retention-plan", help="查看保留期清理计划（只统计，不删除）")
    retention.add_argument("--now", default=None, help="按指定日期计算（YYYY-MM-DD），便于演练")

    purge_cmd = sub.add_parser("purge", help="执行保留期清理（默认只输出计划，--apply 才真删）")
    purge_cmd.add_argument("--apply", action="store_true", help="确认执行删除")
    purge_cmd.add_argument("--export", type=Path, default=None, help="删除前把待删行归档到该目录")
    purge_cmd.add_argument("--now", default=None, help="按指定日期计算（YYYY-MM-DD）")

    backup = sub.add_parser("backup", help="备份运行时数据（JSON.gz，含每表内容哈希）")
    backup.add_argument("--out", type=Path, default=None, help="输出路径，默认 data/backup/backup-<时间>.json.gz")
    backup.add_argument("--plain", action="store_true", help="不压缩")

    restore = sub.add_parser("restore", help="从备份恢复（默认拒绝覆盖非空库）")
    restore.add_argument("--in", dest="input", type=Path, required=True)
    restore.add_argument("--force", action="store_true", help="确认覆盖现有数据")
    restore.add_argument("--target", default=None, help="恢复目标（默认与 --store-db 相同）")

    drill_cmd = sub.add_parser("drill", help="备份恢复演练：备份→恢复到全新库→逐表比对哈希")
    drill_cmd.add_argument("--workdir", type=Path, default=None, help="演练目录，默认 data/drill")

    gate = sub.add_parser("gate-check", help="P0 闸门自检（危险能力/明文密钥/PG RLS 与应用角色）")
    gate.add_argument("--dsn", default=None, help="给出 PostgreSQL DSN 时额外检查 RLS 与连接角色")

    notify = sub.add_parser("notify", help="推送日报/逾期/待审批/数据质量（默认只写草稿到 data/outbox）")
    notify.add_argument("--kind", required=True, choices=list(KINDS))
    notify.add_argument("--channel", default=None, choices=list(CHANNELS),
                        help="默认：有 webhook 环境变量则发飞书/企微，否则写 outbox 草稿")
    notify.add_argument("--date", default=None,
                        help="日报覆盖的日期；默认昨天（早上推送昨日经营）")
    notify.add_argument("--force", action="store_true", help="忽略发送记录强制重发")
    notify.add_argument("--base-url", default=DEFAULT_BASE_URL)
    notify.add_argument("--no-financial", action="store_true", help="消息里不含 GMV/退款")

    notify_history = sub.add_parser("notify-history", help="查看推送记录")
    notify_history.add_argument("--limit", type=int, default=30)

    dashboard = sub.add_parser("dashboard", help="输出经营看板数据（JSON）")
    dashboard.add_argument("--days", type=int, default=7)
    dashboard.add_argument("--no-financial", action="store_true", help="不输出 GMV/退款字段")

    weekly = sub.add_parser("weekly", help="输出周报文本")
    weekly.add_argument("--date", default="today")

    monthly = sub.add_parser("monthly", help="输出月报文本")
    monthly.add_argument("--date", default="today")

    approvals = sub.add_parser("approvals", help="查看审批队列")
    approvals.add_argument("--status", default=None, help="pending / approved / rejected")

    decide = sub.add_parser("decide", help="审批决策（通过后任务转为可执行）")
    decide.add_argument("approval_id")
    decide.add_argument("--to", required=True, choices=["approved", "rejected"])
    decide.add_argument("--by", required=True, help="决策人显示名")
    decide.add_argument("--reason", default="")

    request = sub.add_parser("request", help="提交一条审批申请（临时动作，不挂任务）")
    request.add_argument("--action-type", required=True, help="例如 pricing:change")
    request.add_argument("--by", required=True)
    request.add_argument("--payload-json", default="{}")
    request.add_argument("--reason", default="")

    audit = sub.add_parser("audit", help="查看审计流水（事件 + 工具调用留痕）")
    audit.add_argument("--limit", type=int, default=30)
    audit.add_argument("--event-type", default=None)
    audit.add_argument("--actor-id", default=None)
    audit.add_argument("--days", type=int, default=7)
    audit.add_argument("--tool-runs", action="store_true", help="只列工具调用留痕")

    auth_init = sub.add_parser("auth-init", help="创建账号并授予门店角色（口令不会被记录）")
    auth_init.add_argument("--username", required=True)
    auth_init.add_argument("--display-name", required=True, help="真实姓名或对外显示名")
    auth_init.add_argument("--role", required=True, choices=list(ROLES))
    auth_init.add_argument("--password-stdin", action="store_true",
                           help="从标准输入读取口令（供脚本/自动化使用；交互式请勿加此参数）")

    sub.add_parser("auth-list", help="列出门店成员及其角色")

    auth_passwd = sub.add_parser("auth-passwd", help="重置成员口令（会撤销该用户全部会话）")
    auth_passwd.add_argument("--username", required=True)
    auth_passwd.add_argument("--password-stdin", action="store_true",
                             help="从标准输入读取口令（供脚本/自动化使用）")

    auth_audit = sub.add_parser("auth-audit", help="查看身份与权限相关审计事件")
    auth_audit.add_argument("--limit", type=int, default=30)
    auth_audit.add_argument("--event-type", default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
        store_id = args.store_id or str(config.get("store_id") or "")
        tenant_id = str(config.get("tenant_id") or "")
        # 注册门店并做跨租户守卫（同一 store_id 不允许出现在两个租户下）
        RegistryStore(args.store_db, tenant_id=tenant_id).register(config)
        metrics = PlatformMetricStore(args.metrics_db, tenant_id=tenant_id)
        task_store = TaskStore(args.store_db, tenant_id=tenant_id)
        approvals = ApprovalStore(args.store_db, tenant_id=tenant_id)

        if args.command == "snapshot":
            payload = build_snapshot(config, metrics, args.date, task_store=task_store)
        elif args.command == "plan":
            snapshot = build_snapshot(config, metrics, args.date, task_store=task_store)
            marked = task_store.mark_overdue(store_id)
            plan = generate_daily_plan(snapshot)
            saved = task_store.save_plan(plan, store_id)
            approval_sync = approvals.sync_from_plan(plan, store_id)
            payload = {
                "store_id": store_id,
                "date": plan["date"],
                "overdue_marked": marked,
                "approvals": approval_sync,
                "created": saved["created"],
                "updated": saved["updated"],
                "summary": plan["summary"],
                "guardrails": plan["guardrails"],
                "snapshot_notes": snapshot["_meta"]["notes"],
                "snapshot_counts": snapshot["_meta"]["counts"],
                "tasks": [
                    {
                        "task_id": item["task_id"],
                        "category": item["category"],
                        "title": item["title"],
                        "assignee": item["assignee"],
                        "priority": item["priority"],
                        "requires_approval": item["requires_approval"],
                        "due_at": item["due_at"],
                    }
                    for item in plan["tasks"]
                ],
            }
        elif args.command == "tasks":
            payload = {
                "store_id": store_id,
                "filter": {"status": args.status or "all", "assignee": args.assignee},
                "count_by_status": task_store.count_by_status(store_id),
                "tasks": task_store.list_tasks(store_id, args.status, args.assignee, args.limit),
            }
        elif args.command == "evidence":
            payload = task_store.add_evidence(args.task_id, args.type, args.ref, args.by, args.note)
        elif args.command == "approvals":
            payload = {
                "store_id": store_id,
                "status_filter": args.status or "all",
                "count_by_status": approvals.count_by_status(store_id),
                "approvals": approvals.list(store_id, status=args.status, limit=100),
            }
        elif args.command == "decide":
            result = apply_decision(approvals, task_store, args.approval_id, args.to, args.by, args.reason)
            AuthStore(args.store_db, tenant_id=tenant_id).record_audit(
                event_type="approval_decided", actor_type="user", actor_id=args.by,
                tenant_id=tenant_id, store_id=store_id, object_type="approval",
                object_id=args.approval_id, before={"status": "pending"},
                after={"status": result["approval"]["status"], "reason": args.reason,
                       "task_status": (result["task"] or {}).get("status")},
            )
            payload = result
        elif args.command == "request":
            payload = approvals.request(
                store_id=store_id, action_type=args.action_type,
                payload=json.loads(args.payload_json), requested_by=args.by, reason=args.reason,
            )
            AuthStore(args.store_db, tenant_id=tenant_id).record_audit(
                event_type="approval_requested", actor_type="user", actor_id=args.by,
                tenant_id=tenant_id, store_id=store_id, object_type="approval",
                object_id=payload["approval_id"],
                after={"action_type": args.action_type, "skipped": payload.get("skipped")},
            )
        elif args.command == "audit":
            # AuthStore 会在 SQLite 侧建出 audit_events（PG 侧由 schema.sql 统一保证）
            AuthStore(args.store_db, tenant_id=tenant_id)
            center = AuditCenter(args.store_db, tenant_id=tenant_id)
            if args.tool_runs:
                payload = {
                    "store_id": store_id,
                    "count_by_tool": ToolRunLog(args.store_db, tenant_id=tenant_id).count_by_tool(store_id),
                    "runs": ToolRunLog(args.store_db, tenant_id=tenant_id).list(store_id, limit=args.limit),
                }
            else:
                payload = {
                    "store_id": store_id,
                    "summary": center.summary(store_id, days=args.days),
                    "event_types": center.event_types(store_id),
                    "events": center.list_events(store_id, limit=args.limit,
                                                 event_type=args.event_type, actor_id=args.actor_id),
                }
        elif args.command == "auth-init":
            auth = AuthStore(args.store_db, tenant_id=tenant_id)
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\n")
            else:
                password = getpass.getpass("请输入口令（至少 10 位，不会回显）：")
                if password != getpass.getpass("请再次输入以确认："):
                    raise ValueError("两次输入的口令不一致")
            created = auth.create_user(
                args.username, password, args.display_name, args.role,
                str(config.get("tenant_id") or ""), store_id,
            )
            created["note"] = "口令已用 scrypt + 每用户盐加密存储；请尽快交付本人并提醒修改"
            payload = created
        elif args.command == "auth-passwd":
            auth = AuthStore(args.store_db, tenant_id=tenant_id)
            tenant_id = str(config.get("tenant_id") or "")
            members = [row for row in auth.list_users(tenant_id, store_id) if row["username"] == args.username]
            if not members:
                raise ValueError(f"门店 {store_id} 下没有成员：{args.username}")
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\n")
            else:
                password = getpass.getpass("请输入新口令（至少 10 位，不会回显）：")
                if password != getpass.getpass("请再次输入以确认："):
                    raise ValueError("两次输入的口令不一致")
            auth.set_password(members[0]["user_id"], password, actor_id="auth-passwd")
            payload = {"username": args.username, "user_id": members[0]["user_id"],
                       "note": "口令已更新，该用户所有登录会话已失效"}
        elif args.command == "auth-list":
            auth = AuthStore(args.store_db, tenant_id=tenant_id)
            payload = {
                "store_id": store_id,
                "users": auth.list_users(str(config.get("tenant_id") or ""), store_id),
            }
        elif args.command == "auth-audit":
            auth = AuthStore(args.store_db, tenant_id=tenant_id)
            payload = {
                "store_id": store_id,
                "events": auth.list_audit(
                    tenant_id=str(config.get("tenant_id") or ""), store_id=store_id,
                    limit=args.limit, event_type=args.event_type,
                ),
            }
        elif args.command in ("dashboard", "weekly", "monthly"):
            reporter = StoreReporter(config, metrics, task_store, approvals, config_path=args.config)
            if args.command == "dashboard":
                payload = reporter.dashboard(days=args.days, include_financial=not args.no_financial)
            elif args.command == "weekly":
                payload = reporter.weekly_report(args.date, include_financial=True)
            else:
                payload = reporter.monthly_report(args.date, include_financial=True)
        elif args.command in ("notify", "notify-history"):
            ledger = NotificationLedger(args.store_db, tenant_id=tenant_id)
            if args.command == "notify-history":
                payload = {"store_id": store_id, "history": ledger.list(store_id, limit=args.limit)}
            else:
                reporter = StoreReporter(config, metrics, task_store, approvals, config_path=args.config)
                include_financial = not args.no_financial
                if args.kind == "daily":
                    message = build_daily_digest(reporter, args.date, base_url=args.base_url,
                                                 include_financial=include_financial)
                elif args.kind == "overdue":
                    message = build_overdue_digest(task_store, store_id, base_url=args.base_url)
                elif args.kind == "approvals":
                    message = build_approval_digest(approvals, store_id, base_url=args.base_url)
                else:
                    message = build_quality_alert(metrics, store_id, config, base_url=args.base_url)

                channel = args.channel
                webhook = None
                if channel is None:
                    feishu = os.getenv("FEISHU_WEBHOOK_URL")
                    wecom = os.getenv("WECOM_WEBHOOK_URL")
                    if feishu:
                        channel, webhook = "feishu", feishu
                    elif wecom:
                        channel, webhook = "wecom", wecom
                    else:
                        channel = "outbox"
                elif channel == "feishu":
                    webhook = os.getenv("FEISHU_WEBHOOK_URL")
                elif channel == "wecom":
                    webhook = os.getenv("WECOM_WEBHOOK_URL")

                notifier = Notifier(channel, webhook_url=webhook,
                                    outbox_dir=Path(args.store_db).parent / "outbox" if channel == "outbox" else None,
                                    ledger=ledger, store_id=store_id)
                result = notifier.send(message, force=args.force)
                result["message"] = {"kind": message.kind, "window_key": message.window_key,
                                     "title": message.title, "text": message.text, "empty": message.empty}
                result["channel"] = channel
                payload = result
        elif args.command in ("backup", "restore", "drill", "gate-check"):
            if args.command == "backup":
                out = args.out or Path(args.store_db).parent / "backup" / f"backup-{datetime.now():%Y%m%d-%H%M%S}.json"
                payload = create_backup(args.store_db, out, compress=not args.plain)
                payload["verified"] = verify_backup(Path(payload["path"]))["ok"]
            elif args.command == "restore":
                payload = restore_backup(args.target or args.store_db, args.input, force=args.force)
            elif args.command == "drill":
                payload = drill(args.store_db, args.workdir or Path(args.store_db).parent / "drill")
            else:
                payload = gate_report(Path(__file__).resolve().parent, dsn=args.dsn)
        elif args.command in ("retention-plan", "purge"):
            policy = policy_from_config(config)
            moment = datetime.fromisoformat(args.now) if args.now else None
            if args.command == "retention-plan":
                plans = plan_purge(args.store_db, policy, now=moment)
                payload = {"store_id": store_id, "policy_days": policy,
                           "planned_total": sum(plan.rows for plan in plans),
                           "tables": [plan.as_dict() for plan in plans]}
            else:
                payload = purge_retention(args.store_db, policy, apply=args.apply, export_dir=args.export,
                                          now=moment, tenant_id=tenant_id, store_id=store_id)
        elif args.command == "overdue":
            payload = {
                "store_id": store_id,
                "marked": task_store.mark_overdue(store_id),
                "overdue": task_store.overdue_tasks(store_id),
            }
        else:
            payload = task_store.change_status(args.task_id, args.to, args.by, args.reason)
    except (SnapshotError, TaskStoreError, ValueError, OSError) as exc:
        raise SystemExit(f"执行失败：{exc}")

    print(json.dumps(payload, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
