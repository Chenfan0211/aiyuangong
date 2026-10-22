#!/usr/bin/env python3
"""平台数据导入、数据状态与摘要命令行。

`import-dir` 按表头自动识别两类来源、共七种数据集，可混放在同一个收件箱：

  平台侧（写平台指标库 --db）：
    1. 平台日指标：date,platform,store_id + 9 个指标
    2. 套餐明细：  date,platform,store_id,package_id,package_name + 4 个指标
    3. 评价明细：  review_id,platform,store_id,rating,created_at,status,category,replied_at

  门店侧（写门店侧数据库 --store-db）：
    4. 开台与客流：date,store_id,tables_opened,table_capacity,covers,revenue
    5. 时段分布：  date,store_id,slot,covers,orders,revenue
    6. 库存与物料：date,store_id,item_id,name,unit,quantity,safety_stock
    7. 商圈：      store_id,area_id,area_name,area_type,note

  竞品（写门店侧库 --store-db，第 2 期）：
    8. 竞品门店：  store_id,competitor_id,competitor_name,area_id（可选 note）
    9. 竞品采样：  date,store_id,competitor_id,package_name,price
                  + 必填 sampled_by,source_method,source_note（合规留痕）
                  + 可选 activity,activity_note

日常用法（把平台后台导出的 CSV 放进 imports/inbox/ 后执行一条命令）：

    python platform_sync.py import-dir

`import-dir` 按文件内容哈希去重：重复导入同一文件时 `rows_written = 0`（状态 skipped），
单个文件失败不会中断其它文件；只要有文件失败，进程退出码为 1（便于计划任务告警）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from platform_data import DataValidationError, PlatformMetricStore
from competitor import CompetitorStore
from store_ops_data import StoreOpsDataStore, import_inbox
from registry import RegistryStore
from snapshot_builder import load_config

ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="门店平台经营数据同步")
    parser.add_argument("--db", default=os.getenv("STORE_AGENT_METRICS_DB", str(ROOT / "data" / "platform_metrics.db")),
                        help="平台指标库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--config", type=Path, default=Path(os.getenv("STORE_AGENT_CONFIG", ROOT / "store_config.json")))
    parser.add_argument("--store-db", default=os.getenv("STORE_AGENT_STORE_DB", str(ROOT / "data" / "store_ops.db")),
                        help="门店配置库：SQLite 路径或 postgresql:// DSN")
    parser.add_argument("--store-id", default=None, help="门店编码；默认使用数据库配置，空库时回退 JSON")

    # 子命令内也接受 --store-id；default=SUPPRESS 保证不会覆盖全局上已给的值
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store-id", default=argparse.SUPPRESS, help="门店编码；默认使用数据库配置，空库时回退 JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    import_dir = sub.add_parser("import-dir", parents=[common], help="扫描目录批量导入（推荐）")
    import_dir.add_argument("--inbox", type=Path, default=ROOT / "imports" / "inbox", help="待导入目录")

    import_csv = sub.add_parser("import-csv", parents=[common], help="导入平台官方日指标报表 CSV")
    import_csv.add_argument("path", type=Path)

    import_packages = sub.add_parser("import-packages", parents=[common], help="导入套餐/商品维度明细 CSV")
    import_packages.add_argument("path", type=Path)

    import_reviews = sub.add_parser("import-reviews", parents=[common], help="导入单条评价明细 CSV")
    import_reviews.add_argument("path", type=Path)

    import_traffic = sub.add_parser("import-traffic", parents=[common], help="导入门店开台与客流 CSV")
    import_traffic.add_argument("path", type=Path)
    import_timeslot = sub.add_parser("import-timeslot", parents=[common], help="导入门店时段分布 CSV")
    import_timeslot.add_argument("path", type=Path)
    import_inventory = sub.add_parser("import-inventory", parents=[common], help="导入门店库存与物料 CSV")
    import_inventory.add_argument("path", type=Path)
    import_trade_area = sub.add_parser("import-trade-area", parents=[common], help="导入门店商圈 CSV")
    import_trade_area.add_argument("path", type=Path)

    import_competitor_store = sub.add_parser("import-competitor-store", parents=[common],
                                             help="导入竞品门店 CSV（area_id 必须在本店商圈清单内）")
    import_competitor_store.add_argument("path", type=Path)
    import_competitor_offer = sub.add_parser("import-competitor-offer", parents=[common],
                                             help="导入竞品采样明细 CSV（需带采样人/采集方式/来源说明）")
    import_competitor_offer.add_argument("path", type=Path)

    sub.add_parser("ops-status", parents=[common], help="查看门店侧数据新鲜度")
    ops_summary = sub.add_parser("ops-summary", parents=[common], help="查看门店侧数据摘要")
    ops_summary.add_argument("--days", type=int, default=7)

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
    file_config = load_config(args.config)
    lookup_config = dict(file_config)
    if args.store_id:
        lookup_config["store_id"] = args.store_id

    tenant_id = str(lookup_config.get("tenant_id") or "")
    registry = RegistryStore(args.store_db, tenant_id=tenant_id)
    config_source = "数据库(database)" if registry.store_config(
        tenant_id, str(lookup_config.get("store_id") or "")) is not None else "JSON 首次导入(json-first-import)"
    print(f"配置来源：{config_source}", file=sys.stderr)
    config = registry.effective_config(lookup_config)
    tenant_id = str(config.get("tenant_id") or "")
    store_id = str(config.get("store_id") or "")

    store = PlatformMetricStore(args.db, tenant_id=tenant_id)
    ops = StoreOpsDataStore(args.store_db, tenant_id=tenant_id)
    competitor = CompetitorStore(args.store_db, tenant_id=tenant_id, ops_store=ops)
    failed = 0
    try:
        if args.command == "import-dir":
            payload = import_inbox(args.inbox, store_id, store, ops, competitor)
            failed = int(payload["failed_count"])
        elif args.command == "import-traffic":
            payload = asdict(ops.import_traffic_csv(args.path, store_id))
        elif args.command == "import-timeslot":
            payload = asdict(ops.import_timeslot_csv(args.path, store_id))
        elif args.command == "import-inventory":
            payload = asdict(ops.import_inventory_csv(args.path, store_id))
        elif args.command == "import-trade-area":
            payload = asdict(ops.import_trade_area_csv(args.path, store_id))
        elif args.command == "import-competitor-store":
            payload = asdict(competitor.import_competitor_store_csv(args.path, store_id))
        elif args.command == "import-competitor-offer":
            payload = asdict(competitor.import_competitor_offer_csv(args.path, store_id))
        elif args.command == "ops-status":
            payload = ops.status(store_id)
        elif args.command == "ops-summary":
            payload = asdict(ops.summary(store_id, args.days))
        elif args.command == "import-csv":
            payload = asdict(store.import_csv(args.path, store_id))
        elif args.command == "import-packages":
            payload = asdict(store.import_packages_csv(args.path, store_id))
        elif args.command == "import-reviews":
            payload = asdict(store.import_reviews_csv(args.path, store_id))
        elif args.command == "data-quality":
            payload = store.data_quality(
                store_id,
                days=args.days,
                fresh_days=args.fresh_days,
                jump_alert_percent=args.jump_alert_percent,
                batch_limit=args.batches,
            )
        elif args.command == "status":
            payload = store.status(store_id)
        else:
            payload = store.period_summary(store_id, args.days)
    except (DataValidationError, ValueError, OSError) as exc:
        raise SystemExit(f"导入失败：{exc}") from exc

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if failed:
        raise SystemExit(f"有 {failed} 个文件导入失败，详见上面 files 列表中的 error 字段")


if __name__ == "__main__":
    main()
