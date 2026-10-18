# 生产化落地状态

- 日期：2026-10-19
- 分支：`production-hardening`
- Python：`python -m unittest discover -p "test_*.py"` → **418 passed**
- 浏览器：`PLAYWRIGHT_CHANNEL=chrome npm run test:e2e` → **22 passed**（desktop + mobile）

## 已完成

- 数据库配置真相源：所有入口（FastAPI、标准库服务、CLI、平台导入）首次从 JSON 导入，后续统一以 `store_configs` 为准；普通运行不会覆盖数据库配置；管理 API 使用 revision 乐观锁。
- 版本化迁移：`schema_migrations`、迁移锁、status/dry-run/apply/verify、恢复点清单和并发保护。
- 统一运维调度：`ops_runs`、SQLite/PostgreSQL 锁、最多 3 次重试、固定步骤顺序、真实重试队列与 dead letter、outbox/Webhook 告警适配。
- 加密备份：本地无密钥模式、AES-GCM、SHA-256、证据和配置打包、全新库恢复演练、30/12 保留、S3 假客户端契约。
- 浏览器安全：无 localStorage token、HttpOnly 会话 Cookie、CSRF 双提交、401 处理、共享 session-client。
- 登录生命周期：管理岗首次 MFA 绑定、reset-mfa 重绑定、临时口令强制修改、改密/停用撤销会话、7 天绝对有效期。
- 审批回调：mock/飞书/企微 adapter、签名、时间窗、nonce、幂等回执和固定通过/驳回决策。
- 任务与证据：待验收、图片证据、哈希校验、反馈和 D+7/D+28 结果归因。
- 云部署：Docker Compose、Caddy HTTPS、健康检查、迁移就绪检查、Prometheus 文本指标和结构化日志。
- 管理后台：成员角色/状态、强制改密状态、套餐成本、责任人、全部规则阈值、retention 和运维运行/重试队列。
- 第三方协议：飞书/企微真实签名、Encrypt Key/EncodingAESKey 解密、URL challenge、receiveid 校验和本地 fixture 契约。

## 真实第三方联调待密钥/域名

- S3：`S3_ENDPOINT / S3_REGION / S3_BUCKET / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY`
- 备份加密：`BACKUP_ENCRYPTION_KEY`
- 飞书：`FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_VERIFICATION_TOKEN / FEISHU_ENCRYPT_KEY`
- 企微：`WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_SECRET / WECOM_CALLBACK_TOKEN / WECOM_ENCODING_AES_KEY`
- 域名/TLS：`DOMAIN / ACME_EMAIL`
- MFA 生产密钥：`STORE_AGENT_MFA_KEY`
- LLM 仍可选：`STORE_AGENT_LLM_API_KEY`

## 无密钥默认模式

- `STORE_AGENT_BACKUP_STORAGE=local`：本地加密备份和恢复演练可完整执行。
- `STORE_AGENT_ALERT_CHANNEL=outbox`：告警写草稿，不发真实群。
- `STORE_AGENT_APPROVAL_CALLBACK_PROVIDER=mock`：本地签名回调全链路可测。
- 真实 S3 上传、真实飞书/企微卡片和真实 TLS 只在密钥/域名到位后验收，不能提前标记为已验证。

## 关键验证命令

```powershell
python -m unittest discover -p "test_*.py"
node --check static/session-client.js
npm.cmd install
$env:PLAYWRIGHT_CHANNEL='chrome'; npm.cmd run test:e2e
```
