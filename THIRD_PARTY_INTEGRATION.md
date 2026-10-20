# 第三方密钥与无密钥默认模式

## 当前默认值

| 能力 | 默认值 | 无密钥时行为 |
|---|---|---|
| 备份 | `STORE_AGENT_BACKUP_STORAGE=local` | 写本地 AES-GCM 加密备份 |
| 告警 | `STORE_AGENT_ALERT_CHANNEL=outbox` | 写 outbox 草稿 |
| 审批回调 | `STORE_AGENT_APPROVAL_CALLBACK_PROVIDER=mock` | 本地 HMAC 回调可测；显式切换真实 provider 时缺密钥会拒绝启动 |
| LLM | 未配置 Key | 规则模式，不调用外部模型 |
| 入站消息 | `STORE_AGENT_INBOUND_SIGNING_SECRET` 未配置 | `/api/inbound/messages/{provider}` 返回 503；网页对话不受影响 |

## 回调入口

- 本地契约与联调（审批）：`/api/webhooks/approvals/mock`
- 飞书：`/api/webhooks/approvals/feishu`（GET 处理 challenge，POST 处理签名回调）
- 企业微信：`/api/webhooks/approvals/wecom`（GET 处理 URL 验证，POST 处理加密回调）
- 通用入站消息：`/api/inbound/messages/{provider}`（HMAC 签名、时间窗、nonce 防重放；绑定后进入 Agent）

启动前会执行统一集成配置预检：默认 `mock + outbox + local` 不需要任何第三方密钥；生产环境还要求真实数据库口令、域名、HTTPS Base URL 和容器运行路径。显式选择真实回调、真实告警或 S3 备份时，缺少必需变量会直接拒绝启动。可用 `python store_cli.py gate-check` 复核。

## 后续只需填写的配置

```text
POSTGRES_PASSWORD=
STORE_AGENT_BASE_URL=https://ops.example.com
STORE_AGENT_EVIDENCE_DIR=/app/evidence
STORE_AGENT_IMPORT_INBOX=/app/imports/inbox
STORE_AGENT_BACKUP_LOCAL_ROOT=/app/data/backups
STORE_AGENT_OPS_LOCK=/app/data/ops/daily_sync.lock
BACKUP_ENCRYPTION_KEY=
S3_ENDPOINT=
S3_REGION=
S3_BUCKET=
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_PATH_STYLE=true

FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_VERIFICATION_TOKEN=
FEISHU_ENCRYPT_KEY=

WECOM_CORP_ID=
WECOM_AGENT_ID=
WECOM_SECRET=
WECOM_CALLBACK_TOKEN=
WECOM_ENCODING_AES_KEY=

DOMAIN=
ACME_EMAIL=
STORE_AGENT_MFA_KEY=
STORE_AGENT_INBOUND_SIGNING_SECRET=
```

代码、接口、厂商解密协议、统一配置预检和本地 fixture 契约测试已经完成；真实第三方联调属于密钥和域名到位后的验收阶段。

飞书/企微回调已支持真实签名、Encrypt Key / EncodingAESKey 解密、URL challenge 和 receiveid 校验；填入密钥后可替换 fixture 做真实回调验收。
