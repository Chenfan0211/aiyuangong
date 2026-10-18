#!/usr/bin/env python3
"""平台数据导入、数据状态与摘要命令行。

支持三类数据集，由 `import-dir` 按表头自动识别：

  1. 平台日指标：date,platform,store_id + 9 个指标
  2. 套餐明细：  date,platform,store_id,package_id,package_name + 4 个指标
  3. 评价明细：  review_id,platform,store_id,rating,created_at,status,category,replied_at

日常用法（把平台后台导出的 CSV 放进 imports/inbox/ 后执行一条命令）：

    python platform_sync.py import-dir

`import-dir` 按文件内容哈希去重：重复导入同一文件时 `rows_written = 0`（状态 skipped），
单个文件失败不会中断其它文件；只要有文件失败，进程退出码为 1（便于计划任务告警）。
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from platform_data import DataValidationError, PlatformMetricStore
from snapshot_builder import load_config

ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="门店平台经营数据同步")
    parser.add_argument("--db", default=os.getenv("STORE_AGENT_METRICS_DB", str(ROOT / "data" / "platform_metrics.db")),
                        help="平台指标库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--config", type=Path, default=Path(os.getenv("STORE_AGENT_CONFIG", ROOT / "store_config.json")))
    parser.add_argument("--store-id", default="WH-LION-001", help="门店编码，默认 WH-LION-001")

    # 子命令内也接受 --store-id；default=SUPPRESS 保证不会覆盖全局上已给的值
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store-id", default=argparse.SUPPRESS, help="门店编码，默认 WH-LION-001")

    sub = parser.add_subparsers(dest="command", required=True)

    import_dir = sub.add_parser("import-dir", parents=[common], help="扫描目录批量导入（推荐）")
    import_dir.add_argument("--inbox", type=Path, default=ROOT / "imports" / "inbox", help="待导入目录")

    import_csv = sub.add_parser("import-csv", parents=[common], help="导入平台官方日指标报表 CSV")
    import_csv.add_argument("path", type=Path)

    import_packages = sub.add_parser("import-packages", parents=[common], help="导入套餐/商品维度明细 CSV")
    import_packages.add_argument("path", type=Path)

    import_reviews = sub.add_parser("import-reviews", parents=[common], help="导入单条评价明细 CSV")
    import_reviews.add_argument("path", type=Path)

    data_quality = sub.add_parser("data-quality", parents=[common], help="数据质量体检：缺日/跳变/批次/新鲜度")
    data_quality.add_argument("--days", type=int, default=30, help="体检窗口天数，默认 30")
    data_quality.add_argument("--fresh-days", type=int, default=2, help="超过多少天视为不新鲜，默认 2")
    data_quality.add_argument("--jump-alert-percent", type=float, default=30.0, help="日环比告警阈值（%%），默认 30")
    data_quality.add_argument("--batches", type=int, default=20, help="批次审计条数，默认 20")

    sub.add_parser("status", parents=[common], help="查看平台数据新鲜度")

    summary = sub.add_parser("summary", parents=[common], help="查看统一指标摘要")
    summary.add_argument("--days", type=int, default=7)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    # 目标可以是 SQLite 路径，也可以是 postgresql:// DSN；PG 下用配置里的 tenant 做归属标注
    tenant_id = ""
    if isinstance(args.db, str) and args.db.startswith(("postgresql://", "postgres://")):
        try:
            tenant_id = str(load_config(args.config).get("tenant_id") or "")
        except Exception:                                  # 配置缺失不阻塞，仅不做归属标注
            tenant_id = ""
    store = PlatformMetricStore(args.db, tenant_id=tenant_id)
    failed = 0
    try:
        if args.command == "import-dir":
            payload = store.import_directory(args.inbox, args.store_id)
            failed = int(payload["failed_count"])
        elif args.command == "import-csv":
            payload = asdict(store.import_csv(args.path, args.store_id))
        elif args.command == "import-packages":
            payload = asdict(store.import_packages_csv(args.path, args.store_id))
        elif args.command == "import-reviews":
            payload = asdict(store.import_reviews_csv(args.path, args.store_id))
        elif args.command == "data-quality":
            payload = store.data_quality(
                args.store_id,
                days=args.days,
                fresh_days=args.fresh_days,
                jump_alert_percent=args.jump_alert_percent,
                batch_limit=args.batches,
            )
        elif args.command == "status":
            payload = store.status(args.store_id)
        else:
            payload = store.period_summary(args.store_id, args.days)
    except (DataValidationError, ValueError, OSError) as exc:
        raise SystemExit(f"导入失败：{exc}") from exc

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if failed:
        raise SystemExit(f"有 {failed} 个文件导入失败，详见上面 files 列表中的 error 字段")


if __name__ == "__main__":
    main()
