# 生产化落地状态

- 日期：2026-10-18
- 分支：`production-hardening`
- 验证：`python -m unittest discover -p "test_*.py"` → 316 passed；所有静态 JS 通过 `node --check`。

## 已完成

- Windows CLI JSON 输出改为 ASCII 安全，修复 cp936 下 `¥` 导致的进程失败。
- 修复治理页/看板页存储型 XSS；内联 JS/CSS 外置；两套服务统一严格 CSP、frame/nosniff/referrer 响应头。
- FastAPI 禁止 `demo + 非回环地址`；生产环境关闭 `/docs`、`/redoc`、`/openapi.json`。
- 新增 TOTP MFA、恢复码、两步登录；浏览器登录同时下发 HttpOnly/Secure/SameSite=Strict Cookie。
- 图片证据使用随机文件名、magic-byte 校验、5MB 上限、tenant/store/task 作用域读取和哈希校验。
- 数据缺失或评价周期过期时按规则跳过，不再补零或强制剩余 1 天；生成补数任务。
- PostgreSQL 备份/恢复/演练支持 tenant 上下文，恢复前强制校验；保留期改为租户作用域、先归档后删除。
- 新增 Dockerfile、Compose、Caddy HTTPS、生产依赖锁定、健康检查、Prometheus 文本指标、请求 ID/结构化访问日志。
- 新增 owner 设置与成员页面、配置原子写入、成员创建、口令/MFA 重置。
- 新增任务待验收队列、图片证据上传、任务反馈与 D+7/D+28 结果归因模块。

## 仍需上线前完成

- 将生产配置从 `store_config.json` 完全迁移到 `store_configs` 表；当前管理页原子更新 JSON，服务同步更新内存配置。
- 接入 S3 兼容对象存储的每日加密备份、30 日/12 月保留和异地恢复演练；Compose 包未包含外部备份凭据。
- 为 PostgreSQL 引入正式的 `schema_migrations` 迁移锁/回滚流程；当前仍以幂等 schema.sql + 人工备份为主。
- 将每日任务结果写入 `ops_runs` 并在飞书/企微中闭环失败告警。
- 浏览器端统一改为 Cookie/CSRF 会话，彻底移除 localStorage token 兼容路径；CLI 继续保留 Bearer。
- 增加真实浏览器 E2E（桌面/移动、MFA、任务验收、图片上传、管理后台）。
- 飞书/企微交互式审批卡片仍需公网回调、验签和重放防护。

## 云主机部署入口

`deploy/README.md` 与根目录 `compose.yaml` 提供 Ubuntu 24.04 + Docker Compose + Caddy + PostgreSQL 16 部署说明。RPO 目标 ≤ 24 小时，RTO 目标 ≤ 2 小时；正式上线前必须完成一次服务器外恢复演练。
