# 生产化落地状态

- 日期：2026-10-22
- 分支：`dev/dev-20261020`
- Python：`python -m unittest discover -p "test_*.py"` → **751 passed**（第 3-10 期新增 149 个全绿；跨日脆弱用例已修复）
- 浏览器：`PLAYWRIGHT_CHANNEL=chrome npm run test:e2e` → **34 passed**（desktop + mobile）

## 已完成

- 收尾增强：知识库应用层词频向量（零依赖、可开关，`Migration v16`）；SSE `done` 事件回填 `citations`；沙箱受限工作区与四项文件上限（`read_file`/`list_files`）；`test_store_ops_data` 跨日用例相对日期化。全量 751 用例全绿、gate-check 四项通过。
- 第 10 期多员工/团队与渠道扩展：`digital_employee.py`（岗位 + 能力引用，校验第 5 期注册表）+ `team.py`（数字员工领导 + 人工兜底；成员两类；团队任务指派 + 认领，非法转换拒绝，不做竞标）；四张表进权威模型（`Migration v15`）与保留策略；五条接口 + `workforce:read/manage`；入站收口复用既有 `/api/inbound/messages/{provider}`。
- 第 9 期记忆 + 反馈进化：`memory.py`（三类记忆抽取/去重/隔离召回）+ `skill_evolution.py`（蒸馏与反思提案 + 人工确认）；两张表进权威模型（`Migration v14`）与保留策略；四条接口 + `memory:read/write`、`evolution:read/decide`。不做规则侧改动；提案只记录，未确认时行为不变。
- 第 8 期工具模板/实例 + 沙箱：`tool_manifest.py`（导出清空 secret）+ `tool_registry.py`（凭据只存哈希；网络/跨店/超时拒绝留痕）+ `tool_sandbox.py`（受控入口白名单 + 独立子进程 + 超时 + 结果上限；崩溃不影响主进程）；三张表进权威模型（`Migration v13`）与保留策略；五条接口 + `tool:read/manage/invoke`。沙箱是 14.4 红线第 6 条的唯一显式豁免处。
- 第 7 期SOP 状态机 + 技能图：`sop.py`（4 类节点 + 图校验 + 版本）+ `sop_runtime.py`（8 态状态机、非法转换拒绝、断点快照、人工/审批恢复、SOP 级与步骤级超时出口）；四张表进权威模型（`Migration v12`）与保留策略；五条接口 + `sop:read/manage/run`；审批复用既有通道；task 状态变更不直接改写 sop_run。
- 第 6 期知识库 RAG：`knowledge.py`（章节切片 + 结构感知检索 + 引用治理，引用可回查；无命中明确说明）；导入按内容 hash 幂等；五张表进权威模型（`Migration v11`）与保留策略；三条接口 + `knowledge:read/manage` 权限；`search_knowledge` 工具（15 → 16）与 `/api/chat` 的 `citations`。向量检索未做（环境无 `pgvector`）。
- 第 5 期能力注册与作用域底座：`capability_scope.py`（required ⊆ allowed）+ `capabilities.py`（15 个工具带 id/作用域/授权注册，越权拒绝并 `blocked` 留痕）；`capabilities`/`capability_bindings` 两表进权威模型（`Migration v10`）；`GET /api/capabilities`。注册表从 `ToolSpec` 派生，tools 数组与改造前逐字一致。
- 第 4 期巡店独立流程 + 申诉闭环：`inspection.py`（固定字段模板、版本与单一生效版本、巡店任务、缺项复核）+ `appeal.py`（一任务一次有效申诉、驳回必须写结论）；六条接口与 `inspection:*`/`appeals:*` 五个权限；**未结案申诉阻断奖惩审批（409）**；看板按 category 区分巡店任务；三张表进权威模型（`Migration v9`）与保留策略。
- 第 3 期（第 1-6 步）规则外置与版本化 + 归因回灌：`action_rules` 转正 + `rule_store.py`（版本/状态机/单一 approved）；`rule_seed.py` 导出 7 条内置规则（`rules-seed --approve` 生效）；`rule_eval.py` 表驱动执行器 + 与内置引擎逐字段一致性回归；`/api/rules` 四个接口与 `rules:read/write/decide` 权限；`generate_daily_plan(rules=...)` 注入，未就绪时回退内置并写 `plan_rule_fallback` 审计。第 5 步：`analysis.RULE_VERSION`/`DIAGNOSIS_RULE_VERSION` 读表版本 + 代码兜底；`task_feedback`/`task_outcomes` 收口进 `schema.sql` 与 `pg_sync`（`Migration v6`）。第 6 步：任务带 `rule_key`/`rule_version`（`Migration v7`）支持追溯；`rule_outcomes.py` + `rule_outcomes` 表（`Migration v8`）把任务结果按规则版本聚合成调参提案（只写提案、从不回写 `action_rules`，样本不足或驳回率 ≥50% 等判定确定可测）；`GET /api/rules/{rule_key}/outcomes` 与 `store_cli.py rules-outcomes`；`task_feedback`/`task_outcomes`/`rule_outcomes` 纳入保留策略（730 天，gate-check configured_tables 30→33）。
- 第 2 期竞品监控：数据底座（`Migration v4`）、九类数据集统一收件箱导入、匹配闸门与 `competitor:match` 权限；路线图见 `OPTIMIZATION_MASTER_PLAN.md`。
- 第 11 期对话问数：竞品四维对比（按商圈分组）、模型优先路由、5 个新问数工具、SSE 流式对话；已用 DeepSeek 真实联调（库存/时段/竞品/归因四类问法通过，高风险问句 0 秒拦截）。

- 第 1 期经营数据入口扩展：门店侧四类数据（开台/客流、时段、库存、商圈）已接入统一收件箱，库存预警自动分级并按日期重算；低峰时段与库存补货两条规则只出草稿；看板两张门店侧卡片默认关闭；新增 3 条接口与 `ops:import` 权限（`test_store_ops_data.py` 42 个用例）。

- 接口契约与遗漏覆盖：FastAPI 57 条 `/api/*` 路由已锁定；`/api/data-status`、`/api/history`、`/api/admin/retries` 已有直接回归；`/api/bootstrap` 新增向后兼容的 `capabilities` 字段。
- 标准库备用入口收敛：明确为 FastAPI 之外的核心排查入口；启用 MFA 时启动即拒绝，直接调用登录也不再因 `mfa_required` 抛出 `KeyError`。
- 生产集成预检：`.env.production.example` 已补齐真实回调、告警、S3、MFA 等变量；默认 `mock + outbox + local` 可通过，显式选择真实通道但缺密钥会拒绝启动，并接入 `store_cli.py gate-check`。
- 文档与页面同步：README 明确 FastAPI 完整入口/标准库核心备用边界，修正接口、schema 和测试基线；`agent.html` 去掉过期演示登录提示，并按 `/api/bootstrap.capabilities` 隐藏不可用入口。
- 生产部署补漏：新增 Compose `ops` 入口，统一 Base URL、证据/导入/备份/锁路径；生产预检拦截数据库口令、域名、邮箱、回环 Base URL 和缺失运行路径；`.env.production*` 变体已从 Git/Docker 上下文排除。

- 数据库配置真相源：所有入口（FastAPI、标准库服务、CLI、平台导入）首次从 JSON 导入，后续统一以 `store_configs` 为准；普通运行不会覆盖数据库配置；管理 API 使用 revision 乐观锁。
- 版本化迁移：`schema_migrations`、迁移锁、status/dry-run/apply/verify、恢复点清单和并发保护。
- 统一运维调度：`ops_runs`、SQLite/PostgreSQL 锁、最多 3 次重试、固定步骤顺序、真实重试队列与 dead letter、outbox/Webhook 告警适配。
- 加密备份：本地无密钥模式、AES-GCM、SHA-256、证据和配置打包、全新库恢复演练、30/12 保留、S3 假客户端契约。
- 浏览器安全：无 localStorage token、HttpOnly 会话 Cookie、CSRF 双提交、401 处理、共享 session-client。
- 登录生命周期：管理岗首次 MFA 绑定、reset-mfa 重绑定、临时口令强制修改、改密/停用撤销会话、7 天绝对有效期。
- 审批回调：mock/飞书/企微 adapter、签名、时间窗、nonce、幂等回执和固定通过/驳回决策。
- 任务与证据：待验收、图片证据、哈希校验、反馈和 D+7/D+28 结果归因。
- 第一阶段 AI 店长：签名入站消息、聊天账号预绑定、日/周双口径周期对比、确定性经营归因和数据依据。
- 云部署：Docker Compose、Caddy HTTPS、健康检查、迁移就绪检查、Prometheus 文本指标和结构化日志。
- 管理后台：成员角色/状态、强制改密状态、套餐成本、责任人、全部规则阈值、retention 和运维运行/重试队列。
- 第三方协议：飞书/企微真实签名、Encrypt Key/EncodingAESKey 解密、URL challenge、receiveid 校验和本地 fixture 契约。

## 任务与建议

- 任务卡片支持按需展开目标、生成依据、证据要求、审批状态、已提交证据和事件轨迹。
- `/api/chat` 对建议类问题返回结构化 `advice`；规则模式不依赖大模型 Key，配置模型后可继续增强表达。

## 生产运维入口

- 每日任务在 Compose 内执行：`docker compose --profile ops run --rm ops`。
- `STORE_AGENT_BASE_URL` 必须是公网 HTTPS 地址，供日报/告警链接使用。
- `STORE_AGENT_EVIDENCE_DIR`、`STORE_AGENT_IMPORT_INBOX`、`STORE_AGENT_BACKUP_LOCAL_ROOT`、`STORE_AGENT_OPS_LOCK` 是容器内运行路径。

## 真实第三方联调待密钥/域名

- S3：`S3_ENDPOINT / S3_REGION / S3_BUCKET / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY`
- 备份加密：`BACKUP_ENCRYPTION_KEY`
- 飞书：`FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_VERIFICATION_TOKEN / FEISHU_ENCRYPT_KEY`
- 企微：`WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_SECRET / WECOM_CALLBACK_TOKEN / WECOM_ENCODING_AES_KEY`
- 域名/TLS：`DOMAIN / ACME_EMAIL`
- MFA 生产密钥：`STORE_AGENT_MFA_KEY`
- LLM 仍可选：`STORE_AGENT_LLM_API_KEY`; 入站消息签名：`STORE_AGENT_INBOUND_SIGNING_SECRET`

## 无密钥默认模式

- `STORE_AGENT_BACKUP_STORAGE=local`：本地加密备份和恢复演练可完整执行。
- `STORE_AGENT_ALERT_CHANNEL=outbox`：告警写草稿，不发真实群。
- `STORE_AGENT_APPROVAL_CALLBACK_PROVIDER=mock`：本地签名回调全链路可测。
- 真实 S3 上传、真实飞书/企微卡片和真实 TLS 只在密钥/域名到位后验收，不能提前标记为已验证。

## 关键验证命令

```powershell
python -m unittest discover -p "test_*.py"
npm.cmd run check:js
$env:PLAYWRIGHT_CHANNEL='chrome'; npm.cmd run test:e2e
python store_cli.py gate-check
python platform_sync.py ops-status
```
