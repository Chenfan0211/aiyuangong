# 云主机部署

目标环境：Ubuntu 24.04、Docker Engine、Docker Compose v2、一个域名和 DNS A/AAAA 记录。

1. 将代码放到服务器目录，复制环境模板：`cp .env.production.example .env.production`。
2. 修改域名、邮箱、PostgreSQL 强口令、`STORE_AGENT_BASE_URL`（必须是对应 HTTPS 公网地址）、至少一个告警 Webhook，并把 `STORE_AGENT_MFA_KEY` 换成随机强密钥；不要把 `.env.production` 提交到仓库。
3. 如需真实审批回调，把 `STORE_AGENT_APPROVAL_CALLBACK_PROVIDER` 改为 `feishu` 或 `wecom`，补齐对应回调密钥；默认 `mock` 不需要第三方密钥。
4. 启动前加载环境文件并运行：`set -a; . ./.env.production; set +a; python store_cli.py gate-check`。默认 `outbox + mock + local` 必须通过；选择真实通道但缺密钥会失败。
5. 启动：`docker compose up -d --build`。
6. 检查：`docker compose ps`、`curl -fsS https://$DOMAIN/livez`、`curl -fsS https://$DOMAIN/readyz`。
7. 首次建表：`docker compose exec app python pg_sync.py --dsn "$STORE_AGENT_STORE_DB" --config "$STORE_AGENT_CONFIG" init`。

升级顺序固定为：备份 → `pg_sync.py migrate --status` → `migrate --dry-run` → `migrate --apply` → `migrate --verify` → 启动应用。`/readyz` 在存在待迁移版本时返回 503。
8. 首次导入配置和账号：按 README 使用 `pg_sync.py` 与 `store_cli.py`，生产环境禁止使用 demo 模式。
9. 每日任务使用主机 cron/systemd timer 调用 `docker compose --profile ops run --rm ops`。`ops` 一次性服务复用 app 的 PostgreSQL DSN、`.env.production` 和 evidence/imports/backup 卷；不要在生产宿主机直接运行 `scripts/daily_sync.sh`，该脚本仅用于本地无容器开发。容器内的 `OpsRunner` 锁负责防重入。

RPO/RTO：每日加密备份，RPO ≤ 24 小时；恢复演练目标 RTO ≤ 2 小时。备份必须放到服务器之外的 S3 兼容存储。
