# 门店运营系统（武汉狮子座店试点）

一个面向**单店**的门店运营 Agent：把平台经营数据统一入库，按透明规则生成可派发、可验收、可审计的动作，并通过对话与日报把结论交给店长和店员。

- 门店：武汉狮子座店（`store_id = WH-LION-001`）
- 形态：单店生产化加固版本，支持 FastAPI + PostgreSQL、云主机 HTTPS、管理岗 TOTP、图片证据和 owner 管理后台
- 技术栈：Python 3.12 + FastAPI + PostgreSQL 16 + Docker Compose + Caddy
- 当前验证基线：**316 个自动化用例全部通过**
- 生产化落地状态与未闭环边界见 `PRODUCTION_READINESS.md`

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | **3.10+**（开发与验证环境为 3.12.10） |
| 第三方依赖 | 一期入口 **无**（仅标准库）；FastAPI 入口与 PG 落地见 `requirements.txt` |
| 操作系统 | Windows 已验证；macOS / Linux 同样适用 |

---

## 2. 快速开始

### 2.1 启动对话 Agent

```bash
cd 门店运营系统

# Windows（PowerShell / CMD）
python agent_server.py

# macOS / Linux
python3 agent_server.py
```

首次使用建议走一遍完整流程：

```bash
python store_cli.py auth-init --username manager01 --display-name 张三 --role manager  # 0. 建账号（一次性）
python platform_sync.py --store-id WH-LION-001 import-dir   # 1. 导入平台报表
python store_cli.py plan --date today                       # 2. 生成当日任务
python agent_server.py                                      # 3. 打开界面
```

浏览器打开 <http://127.0.0.1:8766/>（对话界面，含今日三件事与日报按钮）或 <http://127.0.0.1:8766/index.html>（完整试点方案）。

可选参数：

```bash
python agent_server.py --port 8766 --db data/agent_demo.db --metrics-db data/platform_metrics.db
```

### 2.2 冒烟验证

```bash
# 首页与身份引导
curl "http://127.0.0.1:8766/api/bootstrap?user_id=manager-001"

# 一条消息（未导入数据时应明确回答"缺数"，而不是编数字）
curl -X POST http://127.0.0.1:8766/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"manager-001\",\"session_id\":\"demo-1\",\"message\":\"分析最近7天美团和抖音数据\"}"
```

### 2.3 运行测试

```bash
cd 门店运营系统
python -m unittest discover -p "test_*.py"     # Windows
python3 -m unittest discover -p "test_*.py"    # macOS / Linux
```

当前基线：**316 个用例全部通过**（含 FastAPI、PostgreSQL 迁移/等价性/RLS、两套传输层的治理与看板接口、推送与大模型工具调用均用本地桩实测 HTTP）。

---

## 3. 平台数据导入流程

数据来源为**平台后台人工导出**的官方报表：人工只做"导出 + 放进 inbox"两件事，其余（导入、校验、去重、汇总）由系统完成。

### 3.1 日常三步

```bash
# 1. 从平台后台导出报表，按模板存成 CSV（三类文件可混放，按表头自动识别）
# 2. 放进待导入目录：imports/inbox/
# 3. 一条命令全部导入（重复文件自动跳过，单个文件失败不影响其它文件）
python platform_sync.py --store-id WH-LION-001 import-dir

# 随后体检 + 看新鲜度 + 看近 7 天统一口径摘要
python platform_sync.py --store-id WH-LION-001 data-quality --days 30
python platform_sync.py --store-id WH-LION-001 status
python platform_sync.py --store-id WH-LION-001 summary --days 7
```

`import-dir` 输出 `file_count / success_count / skipped_count / failed_count` 以及每个文件的明细（数据集类型、读入行数、写入行数、日期范围、失败原因）。只要有文件失败，命令退出码为 **1**，便于计划任务告警。

### 3.2 三类数据集（按表头自动识别）

| 数据集 | 模板 | 必需字段 |
|---|---|---|
| 平台日指标 | `imports/平台数据导入模板.csv` | `date,platform,store_id` + `impressions,detail_views,paid_orders,verified_orders,gmv,refunds,review_count,negative_review_count,avg_rating` |
| 套餐/商品明细 | `imports/套餐明细导入模板.csv` | `date,platform,store_id,package_id,package_name,detail_views,paid_orders,verified_orders,gmv` |
| 评价明细 | `imports/评价明细导入模板.csv` | `review_id,platform,store_id,rating,created_at`（可选 `status,category,replied_at`） |

约束与语义：

- 一个文件只能包含一个平台；门店编码必须与 `--store-id` 一致。
- 同一文件按 SHA-256 批次哈希去重：重复导入 `rows_written = 0`，状态为 `skipped`。
- **评价明细**是"差评 SLA 任务"的数据前提（需要单条评价的评分与时间）；**套餐明细**是"套餐转化规则"的数据前提。
- 评价 `status` 只接受 `open` / `replied` / `resolved`；`rating` 为 0–5。

### 3.3 数据质量体检

```bash
python platform_sync.py --store-id WH-LION-001 data-quality --days 30
# 可选参数：--fresh-days 2 --jump-alert-percent 30 --batches 20
```

输出四部分：

| 部分 | 含义 |
|---|---|
| `freshness` | 每个平台的最新数据日期与落后天数：`age_days <= fresh_days` 为 `fresh`，否则 `stale`；完全没数据为 `not_connected` |
| `coverage` | **缺日检测**：逐平台给出 `missing_dates` / `expected_days` / `actual_days`。窗口终点取**全店最新数据日期**，各平台从**该平台首条数据日期**开始检查——平台上线前的日期不会误报，中间断档与末尾停更会被检出 |
| `anomalies` | **日环比跳变**：相邻两条数据的变化率超过 `jump_alert_percent`（默认 ±30%）即告警；只检查曝光/访问/订单/核销/GMV/退款，回避评价条数与评分这类小样本噪声；基准值为 0 或缺失时跳过 |
| `batches` | **批次审计**：最近 N 个导入批次的数据集类型、平台、读写行数、SHA-256 前 12 位、日期范围与导入时间 |

`summary.issues` 是给人看的问题清单（缺哪天、哪个平台停更、有几处跳变）。体检结果**不改变退出码**（只有命令本身出错才非 0），因此它在计划任务里只做提示，不会因为业务波动反复告警。

### 3.4 排查单个文件

```bash
python platform_sync.py --store-id WH-LION-001 import-csv      /path/to/meituan.csv
python platform_sync.py --store-id WH-LION-001 import-packages /path/to/packages.csv
python platform_sync.py --store-id WH-LION-001 import-reviews  /path/to/reviews.csv
```

### 3.5 每日自动执行（可选）

```powershell
# 手动跑一次：导入 + 新鲜度 + 近 7 天摘要，全过程追加写入 data/logs/daily_sync_YYYYMMDD.log
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\daily_sync.ps1

# 注册为每天 08:30 的计划任务（一次性，管理员 PowerShell；脚本不会自动注册）
$scriptPath = 'E:\项目\aiyuangong\门店运营系统\scripts\daily_sync.ps1'
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $scriptPath)
$trigger = New-ScheduledTaskTrigger -Daily -At 08:30
Register-ScheduledTask -TaskName '门店运营系统-每日同步' -Action $action -Trigger $trigger
```

导入失败时会给出具体行号与原因（字段缺失、门店不匹配、重复记录、数值非法、平台不支持、时间格式错误、文件非 UTF-8）。

> **红线**：不使用爬虫、不绕过平台限制、不抓取顾客隐私数据；没有接口就用官方导出。

## 4. 快照、计划与任务

规则引擎的输入不再是手填的 `sample_input.json`，而是由 `snapshot_builder.py` 从**数据库 + `store_config.json`** 组装；除成本、目标、责任人这类平台拿不到的字段外，快照里的经营数字全部自动来自已落库数据。

```bash
# 查看组装出的快照（排查"为什么生成了这条任务"）
python store_cli.py snapshot --date today

# 生成当日计划并落库（可重复执行：同一天同一对象不重复派单）
python store_cli.py plan --date today

# 查任务（open = proposed / ready / accepted / overdue）
python store_cli.py tasks --status open
python store_cli.py tasks --assignee 员工小李
```

任务状态机：`proposed`（待审批）→ `ready` → `accepted` → `done` → `verified`；`rejected` 可返工重开，`canceled` 终止；超期由扫描标记为 `overdue`。

```bash
python store_cli.py set-status <task_id> --to accepted --by 员工小李
python store_cli.py evidence   <task_id> --type 脱敏订单号 --ref MT-20260902-0007 --by 员工小李
python store_cli.py set-status <task_id> --to done     --by 员工小李
python store_cli.py set-status <task_id> --to verified --by 武汉狮子座店店长

# 把超期任务标记为 overdue（plan 执行时会自动先扫一次）
python store_cli.py overdue
```

三条硬约束：

1. **没有证据不能算完成**：`done` / `verified` 之前必须至少提交一条证据，对应"仅回复'做完了'不算完成"的门店 SOP。
2. **待审批任务不算逾期**：`proposed` 状态在审批前无法执行，逾期扫描只覆盖 `ready` / `accepted` / `done`，责任在审批人。
3. **重新生成计划不覆盖人工进度**：只刷新标题、优先级、截止时间等描述性字段，已推进的状态与已交证据保持不变。

`store_config.json` 是**唯一需要人工维护**的文件：售价与成本（缺成本时该套餐自动跳过，不生成调价建议）、月度评价目标、周期起止、责任人、规则阈值。导入新的报表或评价后重新执行 `plan`，任务清单会随之变化——不需要改任何 JSON。

数据分为两个库：`data/platform_metrics.db`（平台数据，可由导出文件重建）与 `data/store_ops.db`（任务、证据、事件，不可重建，需单独备份）。

## 5. Agent 与接口

对话界面 <http://127.0.0.1:8766/>（`agent.html`）：顶部显示各平台数据新鲜度与数据质量，中部是"今日三件事"卡片（可直接接受任务、提交证据并完成），右上角一键生成今日日报。

`store_agent.py` 的意图路由是**显式规则表**（`INTENT_RULES`），关键词是数据而不是 if/elif 分支，便于审查与加词；时间范围、平台、指标按槽位解析（如"近30天美团曝光"→ days=30 / meituan / impressions）。支持意图：今日任务、任务依据、任务完成情况、执行 SOP、评价与差评、套餐诊断、平台指标、数据齐不齐、哪天缺数、今日日报；未命中且配置了大模型时走兜底。

### 5.1 接口

| 方法 | 路径 | 权限 |
|---|---|---|
| POST | `/api/auth/login` | 公开（用户名 + 口令） |
| POST | `/api/auth/logout` | 已登录 |
| GET | `/api/auth/me` | 已登录 |
| GET | `/api/bootstrap` | 已登录 |
| GET | `/api/data-status` | metrics:read |
| GET | `/api/metrics/summary` | metrics:read（财务字段需 metrics:read:financial） |
| GET | `/api/data-quality` | quality:read |
| GET | `/api/reports/daily` | report:read |
| GET | `/api/tasks/today` | tasks:read:store 看全店；tasks:read:own 只看本人 |
| GET | `/api/tasks/{task_id}` | 同上；越权 403 |
| GET | `/api/history` | 会话所属用户 |
| POST | `/api/chat` | agent:chat（身份按会话绑定） |
| POST | `/api/tasks/{task_id}/evidence` | 任务负责人或店长 |
| POST | `/api/tasks/{task_id}/status` | `accepted`/`done` 由负责人；`verified`/`rejected` 仅店长 |

### 5.2 身份与权限

- 所有接口先解析 `Authorization: Bearer <token>`，再按权限点校验；无令牌 401、越权 403。
- 写操作（提交证据、推进状态）的 actor 取**服务端会话身份**，不信任前端传的任何身份字段。
- 会话数据分两处：对话内容在 `data/agent_demo.db`，身份绑定与审计在 `data/store_ops.db`。
- 账号、口令策略与权限矩阵见**第 6 节**。

## 6. 登录与权限

### 6.1 创建账号（一次性）

```bash
# 交互式：口令不回显，至少 10 位，不会写入任何文件或日志
python store_cli.py auth-init --username manager01 --display-name 张三 --role manager

# 查看门店成员 / 查看登录与权限审计
python store_cli.py auth-list
python store_cli.py auth-audit --limit 50
```

角色：`owner`（店主）/ `area_manager`（区域负责人）/ `manager`（店长）/ `operator`（线上运营）/ `employee`（店员）。

### 6.2 登录与令牌

网页版直接打开 <http://127.0.0.1:8766/> 即是登录页。命令行方式：

```bash
curl -X POST http://127.0.0.1:8766/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"manager01","password":"<口令>"}'
# → {"token":"...","expires_at":"...","user":{...},"permissions":[...]}

curl http://127.0.0.1:8766/api/auth/me     -H "Authorization: Bearer <token>"
curl -X POST http://127.0.0.1:8766/api/auth/logout -H "Authorization: Bearer <token>"
```

安全要点：

- 口令用 **scrypt + 每用户随机盐** 哈希，数据库里没有明文；连续失败 5 次锁定 15 分钟。
- 令牌 32 字节随机、数据库**只存 SHA-256**；默认 12 小时 TTL、使用中滑动续期、可登出撤销；改口令会撤销该用户全部会话。
- 同一 `session_id` 绑定到某个账号后不能再换人（换账号调 `/api/chat` 返回 403）。
- 所有数据接口只认 `Authorization: Bearer`；**session 模式下 `?user_id=` 不参与鉴权**，带店长的 user_id 也不会提权。

### 6.3 权限矩阵

| 权限 | owner | area_manager | manager | operator | employee |
|---|:--:|:--:|:--:|:--:|:--:|
| `agent:chat` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `metrics:read`（曝光/访问/订单/核销/转化率） | ✓ | ✓ | ✓ | ✓ | ✓ |
| `metrics:read:financial`（GMV/退款/退款率） | ✓ | ✓ | ✓ | ✓ | — |
| `quality:read`（数据质量体检） | ✓ | ✓ | ✓ | ✓ | — |
| `report:read`（日报） | ✓ | ✓ | ✓ | — | — |
| `tasks:read:store` | ✓ | ✓ | ✓ | ✓ | — |
| `tasks:read:own` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `tasks:accept` / `tasks:evidence:submit` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `tasks:verify`（验收/驳回） | ✓ | ✓ | ✓ | — | — |
| `approvals:decide` | ✓ | ✓ | ✓ | — | — |
| `pricing:draft`（调价草稿） | ✓ | — | ✓ | ✓ | — |
| `audit:read` | ✓ | ✓ | — | — | — |
| `config:write` / `store:manage` | ✓ | — | — | — | — |

关键点：**没有财务权限的角色拿到的是被裁剪的数据结构**——响应里根本不存在 `gmv` / `refunds` / `refund_rate` 字段，不是靠文案隐藏。

### 6.4 租户与门店作用域

- 令牌绑定 `(tenant_id, store_id, role)`；所有数据访问都使用会话里的作用域，前端传什么都不影响。
- 跨门店任务即使知道 `task_id` 也返回 403（已有自动化测试覆盖）。
- 当前运行时库是**单租户部署**（门店隔离已强制）；租户级隔离会在批次 8 迁到 PostgreSQL 后逐表显式落地。

### 6.5 demo 模式（仅本机演示）

```bash
python agent_server.py --auth-mode demo   # 会打印醒目警告，允许用 ?user_id= 伪身份
```

默认是 `session`（必须登录）。`demo` 只用于本机演示与自动化测试，**不得对外**。

## 7. FastAPI 与 PostgreSQL（二期批次 8）

### 7.1 两个可用的服务入口

| 入口 | 依赖 | 适用 |
|---|---|---|
| `agent_server.py` | **仅标准库** | 一期环境、无网络机器、故障排查备用 |
| `app_fastapi.py` | FastAPI + uvicorn | 推荐：自带 OpenAPI 文档与请求校验 |

```bash
# FastAPI（推荐）：默认 session 认证，接口文档 http://127.0.0.1:8766/docs
python -m uvicorn app_fastapi:create_app --factory --host 127.0.0.1 --port 8766

# 或直接用内置启动器（等价，可传 --config/--auth-mode 等参数）
python app_fastapi.py --port 8766

# 标准库版（零依赖）
python agent_server.py
```

两个入口**行为等价**：同一套 `auth.py` / `agent_tools.py` / `task_store.py` 逻辑，同样的接口路径、
同样的错误码（无令牌 401 / 权限不足 403 / 任务状态冲突 409 / 口令锁定 423），因此 `agent.html` 两边通用。
等价性由 `test_fastapi_api.py`（11 个用例）与 `test_auth_api.py`（9 个用例）分别覆盖。

### 7.2 PostgreSQL 落地

`schema.sql` 已重写为**权威 PostgreSQL 16+ 数据模型**，分两段：

- **A. 已落地（16 张表）**：`stores`、`store_configs`、`platform_metrics`、`package_metrics`、`reviews`、
  `metric_import_batches`、`action_tasks`、`task_evidence`、`task_events`、`users`、`memberships`、
  `auth_sessions`、`login_attempts`、`audit_events`、`agent_sessions`、`agent_messages`；
- **B. 规划中（7 张表）**：连接器、指标长表、审批中心等，**不会被 `init` 创建**。

```bash
# 建表（幂等，可重复执行）+ 从 SQLite 迁移 + 对账，一条命令
python pg_sync.py --dsn "postgresql://postgres@127.0.0.1:5432/postgres" all

# 分步执行
python pg_sync.py --dsn "..." init     # 只建表
python pg_sync.py --dsn "..." etl      # 只迁移（按主键 upsert；追加型表按租户清空重灌）
python pg_sync.py --dsn "..." verify   # 只对账
python pg_sync.py --dsn "..." --only platform_metrics etl   # 只处理某张表
```

**已用真实 PostgreSQL 16.2 验证**（`test_pg_sync.py`，7 个用例）：

- 27 条 DDL 幂等执行通过，16 张已落地表全部创建；
- 14 张运行表全量迁移，**行数逐表一致**；
- **7 项业务口径对账一致**：GMV（831.50）、支付订单、差评数、平台行数、待处理差评、未关闭任务、已验收任务；
- 类型正确落地：`target`/`evidence_required` 为 jsonb（读回即 dict/list）、`approval_required` 为 boolean、
  `due_at` 为带时区的 timestamptz、金额为 `numeric(12,2)`；
- 迁移幂等：重复执行不产生重复行；
- **租户隔离落地**：16 张表全部带 `tenant_id`，除 `audit_events` 外均为 `NOT NULL`
  （该例外是刻意的：未知用户名的失败登录无法归属租户，已在 `schema.sql` 注明）。

### 7.3 运行时已支持 PostgreSQL（批次 8b）

三个命令行工具与服务端都接受 **SQLite 路径或 `postgresql://` DSN**，由 `db.py` 的方言层自动适配：

```bash
DSN="postgresql://user:pass@host:5432/store"

# 导入 / 体检 / 派单直接写 PostgreSQL
python platform_sync.py --db "$DSN" import-dir
python store_cli.py --metrics-db "$DSN" --store-db "$DSN" plan --date today
python store_cli.py --metrics-db "$DSN" --store-db "$DSN" tasks --status open

# 服务端跑在 PostgreSQL 上（FastAPI 或标准库版都可以）
python app_fastapi.py --metrics-db "$DSN" --store-db "$DSN" --chat-db "$DSN"
python agent_server.py --metrics-db "$DSN" --store-db "$DSN" --db "$DSN"
```

适配层（`db.py`）负责抹平六处引擎差异：

| 差异 | 处理 |
|---|---|
| 占位符 `?` / `:name` | 自动翻译为 `%s` / `%(name)s`（跳过字符串字面量） |
| 回读类型不同（datetime / date / Decimal / dict） | 统一规范为本地 ISO 时间字符串 / float / JSON 文本 |
| 自增主键 | SQLite 用 `lastrowid`，PostgreSQL 用 `insert ... returning id` |
| 建表 | SQLite 用各 store 内联 DDL；PostgreSQL 直接执行 `schema.sql` 的已落地段 |
| 补列迁移 | `add column`（SQLite）/ `add column if not exists`（PostgreSQL） |
| 时区 | 连接即把会话时区设为门店偏移；回读统一成门店本地"墙上时钟" |

**等价性证据**（`test_pg_runtime.py`）：同一份输入分别在 SQLite 与 PostgreSQL 上跑完整业务场景，
逐项比对数据质量、指标摘要、任务清单、派单幂等、任务状态机与事件轨迹、身份/权限/会话/审计脱敏、
以及**日报全文**——全部一致；并核对 PG 各表行数与 `jsonb` / `boolean` / `timestamptz` 类型。

**DSN 的注入方式**：通过命令行参数传入。生产环境请由进程管理器 / 密钥管理器注入参数，
不要把连接串写进代码、前端或仓库。

### 7.5 多租户隔离（批次 8c）

三层防线，缺一不可：

| 层 | 机制 | 证据 |
|---|---|---|
| 写入归属 | 方言层给每条 INSERT 自动补 `tenant_id`（含 `on conflict` 目标），两个引擎行为一致 | 测试断言各表行的 `tenant_id` 全部等于配置租户 |
| 门店注册表 | `registry.py` 把 `stores` / `store_configs` 真正写起来，并做**跨租户守卫**：同一 `store_id` 不允许出现在两个租户下 | 启动即拒绝跨租户复用；`list_stores()` 支持多店 |
| 数据库级 | PostgreSQL 对 17 张租户表开启**行级安全（RLS）**，策略要求 `tenant_id = current_setting('app.tenant_id')`；连接层每次连接设置该变量 | 用非所有者角色实测：**未设租户时看不到任何行**，设为租户 A 只能看到 A 的行 |

说明：表所有者（迁移/运维连接，如 `pg_sync.py`）默认绕过 RLS，用于全量迁移与对账。
**要让 RLS 真正生效，服务必须以非所有者角色连接**（一次性创建）：

```sql
create role store_app login password '<强口令>';
grant usage on schema public to store_app;
grant select, insert, update, delete on all tables in schema public to store_app;
grant usage, select on all sequences in schema public to store_app;
-- 之后用这个角色跑服务：--store-db "postgresql://store_app:<强口令>@host:5432/db"
```
用所有者角色连服务时 RLS 会被绕过（仅建议用于本机演示与迁移）。

SQLite 侧没有 RLS，靠"跨租户守卫 + 门店唯一"保证门店作用域等价于租户隔离（已在文档与测试中注明）。

### 7.4 本地起一个 PostgreSQL（开发/测试用）

```python
import pgserver
server = pgserver.get_server("D:/pgdata")     # 自带 PostgreSQL 16 二进制
print(server.get_uri())                       # postgresql://postgres:@127.0.0.1:<port>/postgres
# ... 跑 pg_sync.py ...
server.cleanup()
```

`test_pg_sync.py` 就是这么做的：**没有本地 PostgreSQL 时自动跳过**，不会阻塞其它测试。

## 8. 审批与审计中心

高风险动作（改价、赔付、公开回复、警告、奖惩、发布）不再只是一句"需要审批"，而是**可追溯的审批单**。

### 8.1 界面

<http://127.0.0.1:8766/governance>（对话页左侧也有入口）：

- **审批队列**：待审批卡片含动作类型、申请人、任务、载荷快照；「通过」按钮把任务转为**可执行**，「驳回」必须填写原因并把任务转为**已驳回**；
- **审计流水**：谁在何时看了什么、改了什么，含登录成功/失败、锁定、越权尝试（`permission_denied`）、日报生成、任务证据与状态变更、审批申请与决策；
- **工具调用留痕**：Agent 每次读取数据的记录（工具名、操作者、会话、状态、脱敏后的入参与出参）。

### 8.2 命令行

```bash
python store_cli.py approvals --status pending          # 审批队列 + 状态统计
python store_cli.py decide <approval_id> --to approved --by 张三 --reason 毛利可接受
python store_cli.py request --action-type pricing:change --by 线上运营 --payload-json '{"package_id":"MT-1632214010"}'
python store_cli.py audit --limit 30                    # 审计流水（--tool-runs 只看工具留痕）
```

`store_cli.py plan`（以及每日计划任务）在派单时会**自动为每个"需审批"任务开审批单**，重复执行不会重复开单。

### 8.3 接口

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/approvals` | `approvals:decide` |
| POST | `/api/approvals` | `approvals:request` |
| POST | `/api/approvals/{approval_id}/decide` | `approvals:decide` |
| GET | `/api/audit` | `audit:read` |
| GET | `/api/tool-runs` | `audit:read` |

`approvals:request` 是批次 9 新增权限，五个角色都有（一线可申请）；`approvals:decide` 仍限店主/区域负责人/店长。

### 8.4 三条硬约定

1. **幂等**：同一门店 + 同一任务 + 同一动作只会有一张待审批单；被驳回后可以重新申请（生成新单，历史保留）。
2. **驳回必须写原因**，且审批决策会写审计（`approval_requested` / `approval_decided`）。
3. **留痕是旁路**：工具调用写入失败绝不阻断业务请求（`log_tool_call` 吞掉异常），入参出参一律脱敏（手机号、身份证、password/token/密钥字段）。

## 9. 经营看板与周/月报

<http://127.0.0.1:8766/dashboard>（对话页与治理页都有入口）：所有数字都来自已落库的平台数据，
不使用演示值；看板与报表会按权限裁剪财务字段（无 `metrics:read:financial` 时**响应里不存在** GMV/退款字段）。

看板包含：KPI + 环比、每日曝光趋势、曝光→访问→支付→核销漏斗、套餐表现（转化率与毛利率）、
数据新鲜度与缺日/跳变、任务与审批统计、自动汇总的「需要关注」清单。

```bash
python store_cli.py dashboard --days 7            # 看板数据（JSON）
python store_cli.py dashboard --no-financial      # 不带 GMV/退款
python store_cli.py weekly                        # 周报文本（近 7 天 + 与上周对比）
python store_cli.py monthly                       # 月报文本（自然月 + 与上月对比）
```

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/dashboard?days=7` | `metrics:read`（财务字段需 `metrics:read:financial`） |
| GET | `/api/reports/weekly?date=today` | `report:read` |
| GET | `/api/reports/monthly?date=today` | `report:read` |

周报/月报结构一致：结论摘要（含环比与数据完整性）→ 分平台明细 → 每日表现（最好/最弱一天）→ 任务与审批 →
数据质量 → 下一步建议（缺数补齐、套餐转化、逾期任务、待审批单）。月报额外与**上一个自然月**对比。

## 10. 消息推送（飞书/企微）

把"打开系统看"变成"送到手边"：日报、逾期提醒、待审批、数据质量四类消息。

### 10.1 三种通道

| 通道 | 行为 | 用途 |
|---|---|---|
| `outbox`（默认） | 把消息写成 `data/outbox/*.json` 草稿，**不碰网络** | 本机演示、留档、人工确认后再发 |
| `feishu` | 真实 POST 到飞书自定义机器人 webhook，载荷 `{"msg_type":"text","content":{"text":...}}` | 飞书群 |
| `wecom` | 真实 POST 到企业微信机器人 webhook，载荷 `{"msgtype":"markdown","markdown":{"content":...}}` | 企微群 |

配置（环境变量，不写进代码/仓库）：

```bash
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
```

给了环境变量后，`notify` 会**自动选对应通道真发**；没给就落到 outbox 草稿（默认只出草稿）。

### 10.2 用法

```bash
python store_cli.py notify --kind daily        # 日报（默认覆盖昨天，早上推送昨日经营）
python store_cli.py notify --kind overdue      # 逾期任务提醒
python store_cli.py notify --kind approvals    # 待审批提醒
python store_cli.py notify --kind quality      # 数据质量提醒
python store_cli.py notify --kind daily --channel outbox --force   # 强制重出草稿
python store_cli.py notify-history             # 查看推送记录
```

`scripts/daily_sync.ps1` 已接一步：导入 → 体检 → 派单 → **推送日报** → 状态与摘要（通知步骤失败也会让脚本退出码非 0）。

### 10.3 三条硬约定

1. **默认只出草稿**：不配置 webhook 就绝不会外发；要真发必须显式给环境变量或 `--channel`。
2. **不重复打扰**：`notifications` 表按「门店 + 类型 + 窗口 + 通道」记录发送结果，同一窗口默认只成功推一次；失败可自动重试，`--force` 可强制重发。
3. **空消息不推**：没有逾期、没有待审批、数据也完整时消息标记为空并跳过（避免"每天一条什么都没发生"）。**反例是有意的**：空库会推一条"还没有平台数据"的提醒——那是需要行动的状态。

### 10.4 尚未做的部分

**交互式审批卡片**（在飞书/企微里直接点"通过/驳回"）需要公网回调地址、签名校验与重放防护，属于下一批；当前推送侧只发"待审批 N 张"并附审批入口链接。

## 11. 目录说明

```
门店运营系统\
├─ agent_server.py          本地演示 HTTP 服务（8 个 GET + 3 个 POST，仅 127.0.0.1）
├─ store_agent.py           对话与权限内核（意图规则表、槽位解析、角色可见性、高风险拦截）
├─ prototype.py             规则引擎：由经营快照生成动作草稿（纯函数，不产生副作用）
├─ platform_data.py         SQLite 统一指标库（校验、幂等批次、新鲜度、周期对比）
├─ platform_sync.py         数据命令行：import-dir / import-csv / import-packages / import-reviews / data-quality / status / summary
├─ store_cli.py             运营命令行：snapshot/plan/tasks/evidence/set-status/overdue/approvals/decide/request/audit/dashboard/weekly/monthly
├─ snapshot_builder.py      快照组装：数据库 + store_config.json → 规则引擎输入
├─ task_store.py            任务与证据库：状态机、指纹去重、逾期扫描、会话绑定
├─ store_config.json        唯一人工维护的配置（成本/目标/周期/责任人/阈值）
├─ db.py                    SQLite 连接工具（提交/回滚后必定关闭）
├─ auth.py                  身份/会话/RBAC/审计/脱敏（框架无关的安全核心）
├─ app_fastapi.py           FastAPI 入口（带 OpenAPI 文档，与 agent_server 行为等价）
├─ pg_sync.py               PostgreSQL 落地：建表 / SQLite→PG 迁移 / 对账
├─ agent_tools.py           只读工具集：指标/数据质量/任务/日报 + 角色可见性
├─ governance.py            审批流 / 工具调用留痕 / 审计查询
├─ reporting.py             经营看板数据 + 周报/月报生成
├─ registry.py              门店注册表（多租户唯一真相源 + 跨租户守卫）
├─ notify.py                消息推送：日报/逾期/待审批/质量 + 双通道载荷 + 发送账本
├─ llm_agent.py             大模型工具调用：八个只读工具 + 权限过滤 + 编排与留痕
├─ ops_backup.py            逻辑备份 / 恢复 / 恢复演练（两引擎通用 + 内容哈希对账）
├─ security.py              Webhook 验签与重放防护 / 凭据轮换 / 闸门自检
├─ retention.py             数据保留与删除策略（默认只计划、先归档再删、写审计）
├─ dashboard.html           经营看板界面
├─ governance.html          审批与审计中心界面
├─ scripts\daily_sync.ps1   每日同步：导入 → 体检 → 派单 → 状态与摘要
├─ schema.sql               PostgreSQL 生产数据模型（二期迁移依据，当前代码未使用）
├─ agent.html               对话界面
├─ index.html               试点方案文档（十一章，填写项存 localStorage）
├─ sample_input.json        最初的演示快照（仅 Agent 演示兼容模式使用，生产走 snapshot_builder）
├─ pilot-baseline.csv       人工成本基线表（试点度量用）
├─ imports\                 导入模板与待导入文件
│  ├─ 平台数据导入模板.csv      （平台日指标）
│  ├─ 套餐明细导入模板.csv      （套餐/商品维度）
│  ├─ 评价明细导入模板.csv      （单条评价，差评 SLA 前提）
│  └─ inbox\                人工放导出文件的位置（内容不入库）
├─ config\env.example       环境变量样例（大模型为可选项）
├─ data\                    运行时生成，已忽略：*.db / logs\ / outbox\（推送草稿）
└─ test_*.py                单元测试（22 个文件 / 253 个用例）
```

---

## 12. 大模型工具调用（可选，批次 13）

规则识别不了的开放问题（例如"这周有没有值得关注的异常"）可以交给大模型，让它**自己决定查什么数据**再回答。
默认**关闭**：没有配置 Key 时整条路径不启用，Agent 仍按规则工作。

### 12.1 八个只读工具（按角色过滤）

| 工具 | 需要权限 | 说明 |
|---|---|---|
| `get_data_status` | `metrics:read` | 各平台数据新鲜度 |
| `get_metric_summary` | `metrics:read` | 近 N 天指标与环比（**财务字段按 `metrics:read:financial` 裁剪**） |
| `get_data_quality` | `quality:read` | 缺日、跳变、批次、新鲜度 |
| `get_data_gaps` | `quality:read` | 缺失日期清单（补数用） |
| `get_tasks` / `get_task_detail` / `get_task_completion` | `tasks:read:own` | 任务清单/详情/完成率（店长看全店，店员只看本人） |
| `build_daily_report` | `report:read` | 店长日报文本 |

**权限过滤在两处生效**：发给模型的 tools 数组里就没有越权工具；即使模型编造工具名，调度器也会拒绝
（不是靠 prompt 自觉，有测试固化）。店员拿到的指标结果里也**不存在** `gmv` 字段。

### 12.2 开启方式

```bash
set STORE_AGENT_LLM_API_KEY=sk-xxxx
set STORE_AGENT_LLM_BASE_URL=https://api.deepseek.com/v1
set STORE_AGENT_LLM_MODEL=deepseek-chat
```

OpenAI 兼容协议，DeepSeek / OpenAI / 本地兼容服务都适用。`GET /api/bootstrap` 会返回 `llm_mode`：
`off`（未配置）/ `plain`（只有纯文本问答）/ `tool_calling`（含工具调用）。

### 12.3 三条安全边界

1. **只读**：工具集里没有任何写操作；改价、赔付、外发仍走审批流程。
2. **留痕**：每次工具调用写 `agent_tool_runs`（`llm:<工具名>`，入参出参脱敏）并写审计 `llm_tool_call`；
   回答带结构化 `sources`（用了哪个工具、什么参数）。
3. **有上限**：最多 4 轮、8 次工具调用，超限会明确回答"先给出已知信息"，不会无限循环。

### 12.4 没有真实 Key 也能验证

`test_llm_agent.py`（14 个用例）用**本地 OpenAI 兼容桩服务**跑真实 HTTP：断言第一次请求带 `tools` 数组、
第二次请求把工具结果回灌为 `role=tool` 消息、最终回答取模型内容；另用脚本化响应覆盖多轮、参数非法、
工具报错、次数上限、权限过滤与降级。模型未配置或不可用时，Agent 自动退回规则模式并明确说明。

## 13. 常见问题

**Q：Windows 上报 `PermissionError: [WinError 32] 另一个程序正在使用此文件`，数据库文件删不掉或测试失败。**
A：这是 SQLite 连接未关闭导致的文件句柄泄漏，**已在 2026-10-18 修复**：所有数据库访问统一走 `_connection()` 上下文管理器，离开时必定 `close()`。若你看到该报错，说明代码被回退到了旧版本。

**Q：`python3` 命令不存在。**
A：Windows 上用 `python`；`python3` 适用于 macOS / Linux。

**Q：`import-dir` 显示某个文件 `skipped` 是什么意思？**
A：该文件内容（SHA-256 哈希）之前已经导入过，系统不重复写库——这是幂等保护，不是错误。

**Q：我用历史日期执行 `plan`，任务一生成就是“逾期”状态？**
A：预期行为——逾期判定以**当前时间**为准，回放过去的日期时这些任务的截止时间早已过去。正式使用时用 `plan --date today`。

**Q：体检提示某天缺数据，怎么补？**
A：从平台后台重新导出**包含该日期**的报表，放进 `imports/inbox/` 再执行 `import-dir`。导入是按天 upsert，补一天不会影响其它天，也不会产生重复行。

**Q：`import-dir` 退出码是 1，但我看到有文件成功了。**
A：退出码 1 表示**至少有一个文件失败**；成功的文件已经入库，请查看输出 `files` 列表里失败项（`status: failed`）的 `error` 字段。常见原因：列名与模板不一致、门店编码不匹配、数值非法、时间格式不对、一个文件混了两个平台。

**Q：员工忘记口令，或者要新建一个店员账号？**
A：新建用 `python store_cli.py auth-init --username staff02 --display-name 李四 --role employee`（口令交互输入、不回显）；重置用 `python store_cli.py auth-passwd --username staff02`。重置会**立即撤销该用户所有登录会话**。

**Q：页面提示"登录已过期，请重新登录"。**
A：令牌默认 12 小时有效（使用中滑动续期），过期或被登出后需重新登录。这是预期行为，不是故障。

**Q：为什么店员看不到 GMV？**
A：权限矩阵决定（第 6.3 节）。店员拿到的是**被裁剪过的数据结构**，响应里根本没有 `gmv`/`refunds` 字段。

**Q：端口 8766 被占用。**
A：改用 `python agent_server.py --port 8767`。

**Q：Agent 回答"尚未配置平台指标库"或"没有已校验的数据"。**
A：这是**预期行为**——系统不会用演示数字代替真实数据。请先按第 3 节导入平台报表。

**Q：`data/` 目录是什么？**
A：运行时自动创建，存放 SQLite 数据库与日志，已在 `.gitignore` 中排除，不要提交。

---

## 14. 安全与合规红线

### 14.1 P0 闸门现状（可复现）

| # | 闸门 | 状态 | 怎么验证 |
|---|---|---|---|
| 1 | 仅内网/VPN 可访问 | ✅ 只监听 `127.0.0.1` | 启动日志里的地址 |
| 2 | 统一后端鉴权与 RBAC | ✅ | `test_auth.py` / `test_auth_api.py` |
| 3 | 租户与门店隔离 | ✅ | `test_tenancy.py`（含 PostgreSQL RLS 非所有者实测） |
| 4 | 凭据进密钥管理器并可轮换 | ✅ 流程+自检 | `store_cli.py gate-check`、`rotation_checklist()`、`store_cli.py auth-passwd` |
| 5 | 危险能力关闭 | ✅ 自检 | `store_cli.py gate-check`（扫描运行时模块的 shell/删除/动态执行） |
| 6 | 顾客与员工数据最小化 | ✅ | 写入前脱敏（手机号/身份证/密钥字段）+ **保留期与删除策略**（`store_cli.py retention-plan` / `purge`，默认只计划、先归档再删、全程审计） |
| 7 | Webhook 验签与重放保护 | ✅ 能力就绪 | `security.verify_inbound()`（HMAC + 时间窗 + nonce 去重），待接入真实回调 |
| 8 | 审批策略不可绕过 | ✅ | `test_governance.py`（无证据不能完成、店员不能验收、驳回必填原因） |
| 9 | 审计日志可追溯 | ✅ | `/api/audit`、`store_cli.py audit`；无删除接口 |
| 10 | 备份与恢复演练 | ✅ | `store_cli.py backup / restore / drill` |

### 14.2 运维命令

```bash
python store_cli.py gate-check --dsn "$DSN"   # 危险能力 + 明文密钥 + PG RLS/角色自检
python store_cli.py backup --out data/backup/b.json.gz
python store_cli.py restore --in data/backup/b.json.gz --force
python store_cli.py drill                     # 备份→恢复到全新库→逐表比对内容哈希

python store_cli.py retention-plan            # 保留期清理计划（只统计，不删除）
python store_cli.py purge --export data/archive   # 只出计划（不加 --apply 绝不删）
python store_cli.py purge --apply --export data/archive   # 先归档待删行，再真删，并写审计
```

备份是**逻辑备份**（JSON.gz：每张表的全部行 + 行数 + 内容哈希），两个引擎通用；
`drill` 在 PostgreSQL 上会**新建一个临时数据库**做恢复目标（绝不覆盖源库），比对完删除。
数据量上去后应换成 `pg_dump` / WAL 归档，但"演练 + 哈希对账"的思路保留。

### 14.3 数据保留期（默认值，可在 store_config.json 的 retention 段修改；0 = 永久保留）

| 数据 | 期限 | 理由 |
|---|---|---|
| 平台/套餐指标、导入批次 | 1095 天 | 经营数据是资产，同比要用 |
| 评价明细（含顾客内容摘要） | 180 天 | 含顾客信息，按最小化原则限时 |
| 任务 / 证据 / 事件 / 审批单 | 730 天 | 申诉与复盘需要的证据链 |
| 审计日志 | 730 天 | 安全审计留痕 |
| 对话消息 / 会话 | 90 天 | 最可能含顾客原话 |
| 工具调用留痕 / 推送记录 | 180 天 | 用于排查与对账 |
| 过期会话 / 登录尝试 | 30 天 | 临时状态 |
| Webhook nonce | 1 天 | 只需覆盖重放窗口 |
| 门店 / 配置 / 成员 / 用户 | 永久 | 主数据由人工管理，避免误删 |

执行约定：**默认只出计划**（`retention-plan` 或 `purge` 不加 `--apply`）；加 `--apply` 才删除；
建议同时给 `--export` 先把待删行归档成 JSON.gz（可核对每表行数与截止日期）。
每一轮删除都会写 `retention_purge` 审计（写在删除之后，保证这条记录本身不被同轮清掉）。
`task_evidence.object_ref` 指向的截图等文件不存于本系统，需由文件存储侧按同样期限处理。

### 14.4 红线（工程约定）

1. **默认不依赖外部凭据**：没有模型 Key 就退回规则模式；没有 webhook 就只出草稿；两者都有测试。
2. 改价、赔付、公开回复、警告、奖惩、发布、上架**一律只生成草稿，必须人工审批**，系统不自动执行。
3. 不购买、不诱导、不伪造评价；任务与推送里不得出现顾客手机号等敏感信息（写入前自动脱敏）。
4. 平台密钥、口令、webhook 密钥不得写入代码/前端/日志/数据库：口令只存 scrypt 哈希、令牌只存 SHA-256；
   `gate-check` 会扫描明文密钥（运行时模块与测试文件都扫，允许 `# noqa: secret` 显式豁免夹具）。
5. **生产必须用非所有者角色连 PostgreSQL**，否则 RLS 被绕过（`gate-check --dsn` 会直接判不通过）。
6. 运行时模块禁止 shell 调用、任意删除与动态执行（`gate-check` 扫描，确需使用要写 `# noqa: dangerous` 说明理由）。

## 15. 版本记录

| 日期 | 变更 |
|---|---|
| 2026-10-18 | 批次 1：修复 SQLite 连接泄漏（16/16 测试在 Windows 全绿）；新增 `README.md`、`requirements.txt`、`.gitignore`、`config/env.example`、`imports/inbox/` 占位 |
| 2026-10-18 | 批次 15（P0 #6 收口）：新增 `retention.py` 数据保留与删除策略（20 张表逐表期限、默认只出计划、`--apply` 才删、先归档再删、`retention_purge` 审计）；`store_config.json` 写入 retention 段；CLI 新增 `retention-plan` / `purge`；`gate-check` 增加"保留策略已配置"检查；新增 13 个测试（253/253 通过） |
| 2026-10-18 | 生产化加固：CLI 编码、严格 CSP、TOTP MFA、HttpOnly Cookie、租户级备份/保留期、按规则数据就绪闸门、Docker Compose + Caddy、健康检查、图片证据、管理后台、任务验收与 D+7/D+28 结果模块；316 个用例全部通过 |
| 2026-10-18 | 批次 14（P0 闸门）：新增 `ops_backup.py`（逻辑备份/恢复/演练，内容哈希对账，PG 演练用临时数据库）与 `security.py`（Webhook 验签+nonce 重放防护、凭据轮换清单与 PostgreSQL 口令轮换、危险能力与明文密钥自检）；`store_cli.py` 新增 `backup/restore/drill/gate-check`；P0 十项闸门全部有可复现验证；新增 27 个测试（240/240 通过） |
| 2026-10-18 | 批次 13（二期-7）：新增 `llm_agent.py`（OpenAI 兼容工具调用：8 个只读工具 + 按角色过滤 + 调度容错 + `agent_tool_runs`/审计留痕 + 轮次上限）；`store_agent` 兜底路径优先走工具调用并可优雅降级；两套传输层与 `/api/bootstrap` 暴露 `llm_mode`；新增 14 个测试（213/213 通过，模型侧用本地兼容桩实测 HTTP） |
| 2026-10-18 | 批次 12（二期-6）：新增 `notify.py`（日报/逾期/待审批/质量四类消息 + 飞书/企微/outbox 三通道 + 发送账本幂等）与 `notifications` 表；`store_cli.py` 新增 `notify`/`notify-history`；`daily_sync.ps1` 接入日报推送（默认草稿）；新增 16 个测试（199/199 通过） |
| 2026-10-18 | 批次 8c（二期-2 续）：`tenant_id` 从"结构属性"升级为强制隔离——方言层自动注入租户（含冲突目标）、SQLite 老库自动补列与租户级唯一索引、新增 `registry.py` 门店注册表与跨租户守卫、PostgreSQL 开启 17 张表的行级安全（RLS）并由连接层设置会话租户；新增 8 个测试（183/183 通过） |
| 2026-10-18 | 批次 11（二期-5）：新增 `reporting.py`（看板数据 + 周报/月报）与 `dashboard.html` 看板界面；两套传输层各加 3 个接口（`/api/dashboard`、`/api/reports/weekly`、`/api/reports/monthly`）并接入工具留痕；`store_cli.py` 新增 dashboard/weekly/monthly；新增 15 个测试（175/175 通过） |
| 2026-10-18 | 批次 9（二期-3）：新增 `governance.py`（审批流 + 工具调用留痕 + 审计查询）与 `governance.html` 审批/审计界面；`approvals`、`agent_tool_runs` 从规划表提升为已落地表；新增 `approvals:request` 权限；两套传输层各加 5 个治理接口并接入工具留痕；`store_cli.py` 新增 approvals/decide/request/audit 与派单自动开审批单；新增 30 个测试（160/160 通过） |
| 2026-10-18 | 批次 8b（二期-2 续）：`db.py` 升级为双引擎方言层（占位符翻译、行规范化、RETURNING id、方言 DDL/补列、会话时区）；四个 store 与全部 CLI/服务入口都接受 `postgresql://` DSN；`schema.sql` 增补运行时唯一索引与 `tenant_id` 默认值；新增 3 个跨引擎等价性测试（130/130 通过）；实测导入/派单/服务/任务闭环全部在真实 PostgreSQL 16.2 上跑通 |
| 2026-10-18 | 批次 8（二期-2）：`schema.sql` 重写为权威 PostgreSQL 16 模型（16 张已落地 + 7 张规划中，逐表 `tenant_id`）；新增 `pg_sync.py`（建表/迁移/对账）并用真实 PostgreSQL 16.2 验证（27 条 DDL、14 表行数一致、7 项业务口径一致）；新增 `app_fastapi.py` FastAPI 入口（OpenAPI、与 stdlib 版行为等价）；新增 18 个测试（127/127 通过） |
| 2026-10-18 | 批次 7（二期-1）：新增 `auth.py`（scrypt 口令、随机令牌仅存哈希、12h TTL+滑动续期、登录节流锁定、RBAC 权限点、租户/门店作用域、审计与脱敏）；服务端改为 Bearer 令牌鉴权并新增 `/api/auth/login|logout|me`，默认 `session` 模式；`store_cli.py` 新增 `auth-init`/`auth-list`/`auth-audit`；`agent.html` 改为登录制；新增 32 个测试（109/109 通过） |
| 2026-10-18 | 批次 6：Agent 意图改为显式规则表 + 槽位解析；新增 `agent_tools.py` 只读工具集（指标/数据质量/任务/日报）与结构化依据；`agent_server.py` 新增 6 个接口（任务、质量、日报）并接入会话绑定与写操作门禁；`agent.html` 增加今日三件事、数据质量条、日报按钮；`daily_sync.ps1` 串联导入→体检→派单；新增 24 个测试（76/76 通过） |
| 2026-10-18 | 批次 5：新增 `snapshot_builder.py`（数据+配置→规则输入，取代手填快照）、`task_store.py`（任务/证据/事件/会话，指纹去重 + 状态机 + 逾期扫描）、`store_cli.py`、`store_config.json`；连接实现收敛到 `db.py`；新增 17 个测试（52/52 通过） |
| 2026-10-18 | 批次 4：新增 `data-quality` 数据质量体检（缺日检测、日环比跳变、批次审计、可配置新鲜度阈值）与 `refund_rate` / `negative_rate` 两个派生指标；`daily_sync.ps1` 加入体检步骤；新增 10 个测试（35/35 通过） |
| 2026-10-18 | 批次 2：新增 `import-dir` 批量导入（三类数据集按表头识别、逐文件容错、哈希幂等）、套餐/评价明细导入与两张模板、`scripts/daily_sync.ps1` 每日同步与日志；新增 9 个测试（25/25 通过） |
