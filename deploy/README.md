# 云主机部署

目标环境：Ubuntu 24.04、Docker Engine、Docker Compose v2、一个域名和 DNS A/AAAA 记录。

1. 将代码放到服务器目录，复制环境模板：`cp .env.production.example .env.production`。
2. 修改域名、邮箱、PostgreSQL 强口令和至少一个告警 Webhook；不要把 `.env.production` 提交到仓库。
3. 启动：`docker compose up -d --build`。
4. 检查：`docker compose ps`、`curl -fsS https://$DOMAIN/livez`、`curl -fsS https://$DOMAIN/readyz`。
5. 首次建表：`docker compose exec app python pg_sync.py --dsn "$STORE_AGENT_STORE_DB" --config "$STORE_AGENT_CONFIG" init`。

升级顺序固定为：备份 → `pg_sync.py migrate --status` → `migrate --dry-run` → `migrate --apply` → `migrate --verify` → 启动应用。`/readyz` 在存在待迁移版本时返回 503。
6. 首次导入配置和账号：按 README 使用 `pg_sync.py` 与 `store_cli.py`，生产环境禁止使用 demo 模式。
7. 每日任务使用主机 cron/systemd timer 调用 `scripts/daily_sync.sh`，脚本自带互斥锁（flock）和失败退出码。

RPO/RTO：每日加密备份，RPO ≤ 24 小时；恢复演练目标 RTO ≤ 2 小时。备份必须放到服务器之外的 S3 兼容存储。
