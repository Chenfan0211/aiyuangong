# 数字员工平台对标与演进路线（StaffDeck / CrewMeld 双对标）

> **路线图已迁移（2026-10-20）**：本文档定位为**对标依据**，其第 8 章的 P0-P5 路线图已由 `OPTIMIZATION_MASTER_PLAN.md` 接管。
> 后续规划与缺口清单以该文档为唯一真相源；本文档保留对标事实、架构拆解、差距矩阵与核验记录。
> 新旧期次对应关系见 `OPTIMIZATION_MASTER_PLAN.md` 第 6.2 节。

- 版本：v1.0
- 日期：2026-10-20
- 状态：**调研 + 规划稿**。本文档不含任何已实施代码；第 8 章全部为**待实现规格**。
- 适用范围：`E:\项目\aiyuangong\门店运营系统\`（武汉狮子座店 `WH-LION-001` 单店试点）
- 对标信息抓取时间：2026-10-20（Asia/Shanghai）
- 对标权重：`OpenBMB/StaffDeck` 为主（架构与机制最完整），`proinsight-io/crewmeld` 为辅（SOP 编排、资产模型、审批断点最清晰）
- 关注维度：**架构与机制**。不含产品形态横评，不含技术选型推荐

---

## 0. 阅读约定

1. **现状真相源**：本仓库的"当前实现"以 `README.md` 与 `PRODUCTION_READINESS.md` 为准。本文档只负责"对标与演进路线"，不承担现状真相源职责。
2. **未实现标记**：本文档中所有标注 **【未实现】** 的模块名、数据表名、接口路径均为**待实现规格**，不代表它们已经存在。第 8 章各期的"新增模块与数据表"整节默认全部为【未实现】。
3. **口径冲突处理**：本文档与现有文档口径不一致处，在 5.4 节显式列出并说明以谁为准，不静默覆盖。
4. **可回查性**：第 2、3 章的功能性结论均附 GitHub 仓库内文件路径；第 5、6 章关于本仓库的"现状"判断均可定位到仓库内文件或符号。

---

## 1. 调研基线

### 1.1 对标仓库快照

| 项目 | 仓库 | Star（2026-10-20） | 开源协议 | 主语言 | 默认分支 | 仓库创建 | 最近推送 |
|---|---|---:|---|---|---|---|---|
| StaffDeck（主对标） | `OpenBMB/StaffDeck` | 2004 | AGPL-3.0 | Python（后端）+ TypeScript（前端） | `main` | 2026-07-13 | 2026-10-20 |
| CrewMeld（辅对标） | `proinsight-io/crewmeld` | 995 | 非标准（GitHub 识别为 `NOASSERTION`，商用前必须自行核对 `LICENSE`） | TypeScript | `main` | 2026-04-13 | 2026-08-28 |

版本锚点：

- **StaffDeck v0.5.0**（2026-09-01，更快更稳的执行）。版本脉络：2026-07-15 开源 → v0.2.0（08-03，harness v2 + IM）→ v0.3.0（08-06，SOP 编辑与沙箱）→ v0.4.0（08-18，多员工协作）→ v0.5.0（09-01）。开发方为面壁智能（ModelBest）、NEU-ModelBest 数据智能联合实验室、THUNLP、OpenBMB、AI9Stars。
- **CrewMeld `main`**（2026-08-28）。

仅作定位、不作深度对标的项目：`zengzag/WorkAvatar`（260★，MIT，桌面端数字员工 + 离线优先本地知识库 + 插件生态）。

### 1.2 关键可回查路径

| 项目 | 路径 | 用途 |
|---|---|---|
| StaffDeck | `backend/app/` | 后端能力分层总入口 |
| StaffDeck | `backend/app/harness/contracts.py`、`sandbox.py` | 沙箱资源上限与网络策略 |
| StaffDeck | `backend/app/skills/skill_schema.py` | SOP 技能图与节点级能力作用域 |
| StaffDeck | `backend/app/knowledge/citations.py`、`service.py`、`okf.py` | 结构感知检索与引用治理 |
| StaffDeck | `backend/app/memory/service.py` | 长期记忆抽取与召回 |
| StaffDeck | `backend/app/evolution/service.py` | 反馈到演进提案 |
| StaffDeck | `backend/app/tools/tool_schema.py` | 工具执行策略 |
| StaffDeck | `design-multi-agent-team-decisions.md` | 多 Agent 团队决策记录（HITL） |
| StaffDeck | `design-model-api-protocols.md` | 模型协议层落地方案（含评审 NO-GO 记录） |
| CrewMeld | `README.md` | 产品特性、五层架构、部署矩阵 |

### 1.3 检索方法

GitHub Search API 按 `"digital employee"`、`AI employee platform`、`数字员工`、`retail store AI agent operations` 四组关键词检索，按 star 排序取前 12-20 条。零售/门店专用方向检索结果全部为 0-1 star 的个人练习仓库，不具备工程参考价值，故不做对标。

---

## 2. StaffDeck 架构拆解

### 2.1 定位

企业级数字员工平台。目标是把"个人的工作经验、业务流程、判断标准"转成**可持续运转、可接管重复工作、可沉淀为组织资产**的数字员工。数字员工在这里是有**岗位、工号、能力档案、工作记录**的一等实体，而不是一段提示词。

官方自述的四项核心能力：

1. **构建与管理数字员工**：岗位、工号、能力档案、工作记录；支持能力成长、权限隔离、发布与复用。
2. **状态机驱动的流程技能**：自然语言生成结构化 SOP，用状态机精确执行复杂流程；支持多流程实时切换、上下文保持、可视化编辑、版本管理与分支演化。
3. **文档结构感知的知识检索**：在文档、章节、页面、摘要等多级建立可导航索引，让数字员工先估算信息可能在哪儿，再逐步定位原文；支持知识桶、定向检索、来源引用与检索调试。
4. **自主执行与持续改进**：通过 HTTP API、MCP 与定时任务执行真实业务操作，再用长期记忆、完整 trace、人工接管、用户反馈与反馈分析闭环改进。

### 2.2 能力分层

`backend/app/` 下的目录本身就是一张能力地图：

| 目录 | 职责 |
|---|---|
| `agents/` | 数字员工实体与分支演化（`schema.py`、`branching.py`） |
| `skills/` | SOP 技能：蒸馏、编辑、反思、schema、步骤 id、工具授权、嵌套校验 |
| `harness/` | 隔离执行：沙箱、命令、文件系统、执行上下文、产物、注册表、技能脚本 |
| `knowledge/` | 知识库：解析、结构索引、检索、引用、知识格式（`okf.py`） |
| `memory/` | 长期记忆：抽取任务与服务 |
| `capabilities/` | 能力注册与作用域（含 `local_*` 本地实现与 `testkit.py`） |
| `evolution/` | 反馈聚合与演进提案 |
| `tools/` | 工具执行：MCP 客户端/内置、HTTP 请求、A2A 客户端与恢复、外部任务、执行器 |
| `teams/` | 团队：schema、service、sweeper、wakeup |
| `a2a/` | Agent 间协议适配（`codex_adapter.py`） |
| `channels/` | 渠道接入 |
| `observability/` | 事件日志、会话计时、span |
| `security/`、`scheduled_tasks/`、`session/`、`db/`、`llm/`、`api/`、`public_api/` | 其余基础设施 |

### 2.3 机制一：沙箱 harness 的执行契约与产物

`harness/` 把"执行"拆成了可约束、可观测、可留痕的几件事：`executor.py` 负责执行，`sandbox.py` 负责隔离，`filesystem.py` 负责文件边界，`artifacts.py` 负责产物，`execution_context.py` 负责上下文，`contracts.py` 定义契约，`registry.py` 负责注册，`skill_script.py` 承接技能脚本。

两个关键契约：

- **资源上限**（`harness/contracts.py` 的 `HarnessLimits`）：单次读取 1 MiB、单文件 10 MiB、单工作区 100 MiB、条目数 1000、单次返回结果 200 KiB。全部有下界校验。
- **网络策略**（`harness/sandbox.py`）：`SandboxNetworkMode` = `all` / `allowlist` / `deny`，按租户配置；非法策略直接拒绝执行并报 `SANDBOX_POLICY_INVALID`。

**可借鉴**：把"资源上限 + 网络策略"作为执行前的强制契约，而不是运行时的尽力而为。**不适用**：其沙箱依赖独立运行时与容器，本项目单店体量不需要照搬。

### 2.4 机制二：技能图 + 节点级能力作用域

这是 StaffDeck 最有参考价值的一处设计。`skills/skill_schema.py` 定义了两层结构：

- **`SkillGraphNode`**：节点级定义，含 `node_id`、`type`（默认 `collect_info`）、`name`、`instruction`、`optional`、`condition`、`expected_user_info`、`allowed_actions`。即"流程是一张图，每个节点带类型、条件与允许动作"。
- **`SkillCapabilityRefs`**：**节点级能力暴露**。每个节点显式声明本节点激活期间可用的 `general_skill_ids`、`tool_ids`、`knowledge_base_ids`，另有对应的 `required_*` 字段，并通过校验器强制 `required_*` 必须是所选 `*_ids` 的子集。

配套的作用域枚举定义在 `app/capability_scope.py`，由 `capabilities/scope.py` 导出：`GENERAL_CAPABILITY_SCOPE` 与 `SOP_SPECIFIC_CAPABILITY_SCOPE`，即**能力作用域区分"通用"与"SOP 专属"两档**。

`skills/` 其余模块补全了技能生命周期：`skill_distiller.py`（自然语言 → 结构化 SOP）、`skill_editor.py`（编辑）、`skill_reflection.py`（反思）、`step_ids.py`、`nesting.py`（SOP 嵌套校验）、`tool_authorization.py`（工具授权）、`llm_limits.py`。

**可借鉴**：能力不做全局开关，而是**按流程节点收窄**——节点没声明要用的工具与知识库，执行时就拿不到。这比"全局角色权限"更细，也更贴近门店场景（同一个店员在不同流程步骤里的可见范围本就不同）。

### 2.5 机制三：结构感知知识检索与引用治理

`knowledge/` 分四块：`parser.py` 解析、`okf.py` 结构化知识格式与概念检索、`schema.py` 数据契约、`service.py` 入库与检索编排、`citations.py` 引用治理。

数据模型（见 `service.py` 的导入）：`KnowledgeBucket`（知识桶）、`KnowledgeBase`、`KnowledgeDocument`、`KnowledgeChunk`、`KnowledgeConcept`（概念层）、`KnowledgeIngestJob`（入库任务）。入库走分阶段流水线 `INGEST_STAGES`。

`citations.py` 里有一段很值得抄的**引用治理**逻辑：

- 引用按**首次出现顺序**重编号（`compact_knowledge_citation_labels`）；
- 截断上限分档：引用摘录 6000 字符、引用摘要 800 字符、概念摘录 2400 字符；
- **剔除模型自造的参考页脚**：当检索没有产出可持久化的引用时，模型仍可能自己编一段"参考来源：…"页脚，代码用 `SOURCE_FOOTER_PATTERN`（匹配 `参考来源` / `参考资料` / `引用来源` / `资料来源`）把它剥掉，不允许模型凭空造出处。

**可借鉴**：引用治理的三条硬规则（重编号、截断分档、剥离无据页脚）可以直接迁移。**注意**：`service.py` 体量很大（约 100 KB），说明这套知识库的复杂度不低，本项目应大幅裁减后再落地。

### 2.6 其他机制

- **工具执行策略**（`tools/tool_schema.py` 的 `ToolExecutionPolicy`）：超时 1-3600 秒；`execution_mode` 支持 `sync` 与 `detached`；`async_strategy` 支持平台自身 worker（`staffdeck_worker`）或由提供方托管任务（`provider_task`），后者要配状态地址、轮询间隔、任务 id/状态/结果字段名、状态映射与最长跟踪时长（上限 30 天）。工具与能力作用域绑定。
- **长期记忆**（`memory/service.py`）：`MemoryRecord`，记忆类型限定为 `profile` / `preference` / `fact` 三类，有独立的抽取提示词（`memory_extractor_prompt.md`），抽取结果带固定来源标记，按租户/用户/员工召回。
- **反馈与进化**（`evolution/service.py`）：核心实体是 `EvolutionProposal`（演进提案），配合 `SkillFeedback`、`MessageFeedback`、`AgentResourceBinding`，由 `SkillEditor` 落地修改，并在改动前做 SOP 嵌套校验。即**反馈 → 提案 → 编辑技能**，不是直接把反馈写进提示词。
- **多 Agent 团队**（`design-multi-agent-team-decisions.md`）：明确写着"贯穿原则：HITL（Human in the Loop）"。锁定决策包括——团队领导由数字员工扮演 + 人类控制台兜底；任务采用"指派 + 认领 + 竞标"混合制，竞标用 3 轮血条赛制（HP = 100 − Σ(10 − 该轮得分) × 3，归零淘汰）；任务等于绑定 `(team, task)` 的独立 harness 会话，团队上下文注入任务描述/竞标记录/黑板 top-K/花名册；黑板按团队隔离，写入走轻量入库流水线并定期沉淀到知识库；权限角色为 Owner / 协作人 / 成员。表为 `teams` / `team_members` / `team_tasks`。**该文档还明确记录了被否选项与理由**（如"纯 agent TL"被推迟到 v2 因为唤醒循环与错误派发的运行时风险），这种把否决策也留档的写法同样值得借鉴。
- **可观测**（`observability/`）：`event_log.py` 事件日志、`session_timings.py` 会话计时、`spans.py` span（其他模块按 span 埋点，如 `llm_operation`）。

### 2.7 StaffDeck 借鉴结论

| 结论 | 内容 |
|---|---|
| 直接借鉴 | 节点级能力作用域；引用治理三条硬规则；`HarnessLimits` + 网络策略式的执行契约；反馈 → 提案 → 编辑的闭环形态；决策记录里保留被否选项与理由 |
| 改造后借鉴 | SOP 技能图（裁到 4 类节点：执行 / 人工 / 审批 / 分支）；知识库（裁掉概念层与多级索引，只留文档-章节-分片）；记忆（只留 profile / preference / fact 三类） |
| 不照搬 | 桌面端分发、OIDC SSO、A2A 协议、竞标血条赛制、独立向量库与 RAGFlow 依赖 |
---

## 3. CrewMeld 架构拆解

### 3.1 定位与三资产模型

定位：企业 AI 数字员工平台，为 AI Agent 提供编排、部署与运行时能力，支持私有化部署。

平台把 AI 能力组织成**三类资产**：

| 资产 | 职责 | 编排粒度 | 创建方式 |
|---|---|---|---|
| 数字员工 | 任务执行实体 | — | 入职向导 |
| SOP | 多角色协作流程，支持跨小时/跨天执行 | 步骤级 | 可视化画布 |
| 工具 | 原子能力，毫秒到秒级 | 操作级 | 模板实例化 / OpenAPI 导入 |

依赖关系：工具被数字员工按需绑定和调用；数字员工与真人员工由 SOP 编排成多角色协作流。

数字员工的属性构成：身份信息（姓名、头像、角色类型、人设）、工具实例、LLM 模型配置、知识库绑定、系统连接、运行状态（待命 / 运行 / 暂停 / 错误）、运营指标（已完成任务数、成功率、平均耗时）。入职向导固定 6 步：选角色或直接创建 → 基本信息 → 工具绑定（**仅已部署的实例可绑定**）→ 知识库绑定（多对多）→ 模型配置 → 确认入职（初始状态为待命）。

### 3.2 五层产品架构

```
触发层       8 渠道(企微/钉钉/飞书/微信OA/邮件/短信/Telegram/Discord) + CRON + 手动/Open API
   ↓ 消息与触发事件
SOP 编排层   数字员工 + 真人员工 + 人工审批 + 条件分支
   ↓ 调度数字员工节点
数字员工层   对话管理 + 意图路由 + 工具调度
   ↓ LLM 决策并调用工具
工具服务层   OpenSandbox 隔离的 Node.js / Python 进程；JSON 工具与 HTTP/SSE 服务
   ↓ 通过用户新增的连接
外部系统     数据库 / 自定义 API / CRM / Shopify / ERP / OA
   ↓ 平台自身依赖
基础设施     PostgreSQL + Redis + MinIO + RAGFlow + LLM
```

另外还有一条独立的**客服架构**：Web / H5 / 渠道 / 数字员工 API → 身份 + API Key + Origin 策略 → 共享会话运行时 →（作用域内 RAG 问答 | OCR 与文件 | 人工转接）。关键设计是**运行时守卫**：客服模式下允许有据可依的知识检索，但**跳过 SOP 与工具执行**。即"同一个运行时，靠守卫收敛能力边界"。

### 3.3 SOP 编排与 8 态执行状态机

- **步骤类型 4 种**：数字员工 / 真人员工 / 人工审批 / 条件分支。
- **执行状态机 8 态**：`pending`、`running`、`completed`、`human-waiting`（断点恢复）、`timeout`、`error`、`failed`、`cancelled`。所有状态转换都做合法性校验，非法转换被拒绝。
- **触发类型 3 类**：定时（cron 表达式 + 队列调度）、事件驱动（渠道消息 / webhook 回调）、手动（控制台按钮 / Open API）。
- **断点续跑**：流程走到人工审批节点时引擎暂停，并把**完整状态快照持久化到数据库**；随后通过审批人配置的渠道（飞书 / 企微 / 钉钉审批卡片、邮件 HTML 卡片）发送通知，审批人通过**免登录链接**决策；引擎从断点恢复，并发用**先到先得**控制。
- **超时机制**：步骤级超时自动追加"超时"出口；SOP 级最长执行时长默认 24 小时、可配置。
- **可视化编辑**：拖拽节点编排、边定义流转、节点属性面板（执行者 / 超时 / 重试）、每次保存产生新版本的版本管理。
- **执行追踪**：执行记录列表（可按 SOP 类型 / 时间 / 状态过滤）、步骤级时间线、流式日志、待审批队列。

### 3.4 工具模板/实例与沙箱

- **两层模型**：模板与实例分离，把**代码与配置解耦**；一个模板可派生多个实例，各自持有独立参数与凭据，独立部署、独立运行。模板来源为官方预置或用户安装（自建 / ZIP 导入）。
- **ZIP 导入导出**：用于跨环境迁移；标记为 secret 的环境变量**导出时被清空**，导入后必须手工补填，防止凭据泄漏。这一条与本项目"密钥不入代码/前端/日志/数据库"的红线同源。
- **沙箱**：工具实例代码在隔离的 OpenSandbox 容器中运行（Docker 或 Kubernetes 运行时），与主平台进程分离；支持 JavaScript 与 Python；沙箱故障不影响平台稳定性。
- **Dev Studio**：可按工具清单中的 `service.type` 构建与测试三类服务——`json`（结构化工具/API）、`http`（网页或通用 HTTP 服务，代理方法/查询串/重定向/相对静态资源）、`sse`（流式 HTTP 服务，禁用代理缓冲）。
- **服务发布**：认证可选 API Key 或匿名访问；网络暴露默认内部 `/services/{instanceId}/`，可选择性对外公开发布。

### 3.5 部署形态

Docker Compose（开发与小规模）与 Helm（生产 Kubernetes）两套形态。Compose 用 profile 组合：默认最小栈（应用 + 数据库 + Redis）、`opensandbox-docker` + `minio`（工具沙箱）、`ragflow`（知识库）、`ollama` / `ollama-cpu`（本地模型）。官方标注的最小内存：最小栈约 500 MB，带工具沙箱约 1.5 GB。开发依赖 PostgreSQL 17 + Redis 7，包管理用 Bun，框架为 Next.js + Socket.IO。E2E 预置三个 RBAC 角色用户：`super_admin` / `admin` / `member`。

### 3.6 CrewMeld 借鉴结论

| 结论 | 内容 |
|---|---|
| 直接借鉴 | SOP 的 4 类步骤与 8 态状态机；审批节点暂停 + 全量状态快照 + 免登录链接恢复；SOP 级最长执行时长（默认 24h）+ 步骤级超时出口；模板/实例两级与"导出清空 secret" |
| 改造后借鉴 | 可视化画布（本期只做最小可用编辑，不做拖拽）；执行追踪的步骤级时间线（本项目已有 `audit_events` 可扩展） |
| 不照搬 | 8 渠道、Redis + MinIO + RAGFlow 三件套、OpenSandbox 的 K8s 运行时、Helm、Node.js/SSE 服务发布 |

---

## 4. 共性骨架与差异点

两家产品在"数字员工该长什么样"上高度收敛，差异主要在取舍。

| 层 | 共性骨架 | StaffDeck 的取舍 | CrewMeld 的取舍 |
|---|---|---|---|
| 身份 | 数字员工是一等实体：岗位/角色、能力档案、运行状态、运营指标 | 强调"工号 + 能力成长 + 发布复用"，把经验沉淀为组织资产 | 强调"入职向导 + 人设 + 运行状态机"，更贴近 HR 心智 |
| 编排 | 流程是一张可版本化的图，支持人工节点与人工审批节点 | 自然语言蒸馏出 SOP，状态机执行，支持分支演化 | 可视化画布拖拽，4 类步骤，8 态状态机，跨天执行 |
| 能力 | 能力按作用域暴露，工具与知识库按需绑定 | **节点级**能力作用域（`SkillCapabilityRefs`），最细 | 员工级绑定（仅已部署实例可绑），较粗 |
| 交互 | 多渠道 + 定时 + 事件 + 手动 | IM 系统 + 飞书 connector | 8 渠道 + CRON + Open API |
| 治理 | 审批、审计、trace、人工接管 | HITL 贯穿；反馈→提案→编辑技能 | 审批是一等步骤；断点快照 + 免登录决策 |
| 基础设施 | PG + 队列/缓存 + 对象存储 + 知识检索 + LLM 网关 | 桌面端分发 + 本地/lan/public 三档监听 | PG + Redis + MinIO + RAGFlow，Compose/Helm 双形态 |
| 知识 | 结构感知检索 + 来源引用 | 自研多级索引 + 概念层 + 引用治理 | 依托 RAGFlow，客服模式用运行时守卫限权 |
| 记忆 | 长期记忆沉淀用户画像/偏好/事实 | 独立 `MemoryService` + 三类记忆 + 抽取提示词 | README 未单列，归属会话运行时 |

**对本项目最要紧的一条结论**：两家都把"能力作用域"当成架构的一等公民（StaffDeck 细化到节点，CrewMeld 到员工），而本项目目前的作用域是**角色级、硬编码在工具方法里**。这是所有后续能力（知识库、SOP、工具、记忆）的共同前置依赖——所以演进路线把"能力注册与作用域底座"排在最前面（P0）。

---

## 5. 自身基线

### 5.1 形态与技术栈

- 门店：武汉狮子座店（`store_id = WH-LION-001`），**单店试点**。
- 形态：单店生产化加固版本。
- 技术栈：Python 3.12 + FastAPI + PostgreSQL 16 + Docker Compose + Caddy。
- 验证基线（2026-10-20 复核，见第 9 章）：**499 个 Python 用例全部通过**；`npm.cmd run check:js` 通过；Playwright 30 个浏览器用例（desktop + mobile）为文档记录基线。
- 工作分支：`dev/dev-20261020`。

### 5.2 模块地图

按职责分组（文件均位于 `门店运营系统\`）：

| 分组 | 模块 |
|---|---|
| 入口与传输 | `app_fastapi.py`（完整生产入口，含 OpenAPI）、`agent_server.py`（标准库备用入口，仅 127.0.0.1）、`runtime_pages.py` |
| 对话与规则内核 | `store_agent.py`（意图规则表、槽位解析、角色可见性、高风险拦截）、`prototype.py`（规则引擎，纯函数）、`analysis.py`（经营分析规则，带版本号） |
| 数据 | `platform_data.py`（统一指标库）、`platform_sync.py`（导入命令行）、`snapshot_builder.py`（快照组装）、`db.py`（连接层）、`pg_sync.py`（PostgreSQL 建表/迁移/对账）、`schema.sql`（权威数据模型） |
| 任务与治理 | `task_store.py`（任务状态机、指纹去重、逾期扫描）、`governance.py`（审批/留痕/审计）、`approval_callbacks.py`（回调 adapter） |
| 安全与身份 | `auth.py`（scrypt 口令、令牌哈希、RBAC、租户作用域、审计与脱敏）、`security.py`（Webhook 验签与重放防护、凭据轮换、闸门自检）、`registry.py`（门店注册表与跨租户守卫）、`retention.py`（保留与删除策略） |
| 输出 | `reporting.py`（看板 + 周/月报）、`notify.py`（消息推送三通道）、`evidence_store.py`（图片证据） |
| 运维 | `ops_runner.py`（统一调度）、`ops_store.py`、`ops_backup.py`（备份/恢复/演练）、`backup_manager.py` |
| 结果闭环 | `outcome_tracker.py`（反馈 + D+7/D+28 结果归因） |
| 大模型 | `llm_agent.py`（OpenAI 兼容工具调用）、`agent_tools.py`（只读工具集与角色过滤）、`channel_store.py` |

### 5.3 不可回退的护城河

以下能力是本项目相对于通用数字员工平台的**差异化资产**，演进过程中不得退化：

1. **确定性规则引擎**。`prototype.generate_daily_plan()` 是纯函数，由经营快照算出可派发、可验收、可审计的动作草稿；`analysis.py` 带 `RULE_VERSION = "1.0"` 与 `DIAGNOSIS_RULE_VERSION = "1.0"`，阈值是显式常量（`CHANGE_THRESHOLD_PERCENT = 10.0`、`RATE_THRESHOLD_POINTS = 1.0`、`RATING_THRESHOLD = 0.2`）。**判断不依赖大模型**，可解释、可测试。
2. **合规护栏**。改价、赔付、外部发布、警告、奖惩一律只生成草稿并标记 `requires_approval`；系统不自动执行（README 14.4 第 2 条）。
3. **不编造数字**。无数据时明确回答"缺数"，而不是给一个看起来合理的数字（冒烟基线已固化该行为）。
4. **统一指标库 + 幂等导入**。三类数据集按表头自动识别；同文件按 SHA-256 批次哈希去重；按天幂等更新；单文件失败不影响其它文件。
5. **数据质量体检**。缺日检测、日环比跳变、批次审计、可配置新鲜度阈值。
6. **租户与门店隔离**。`tenant_id` 强制隔离 + PostgreSQL **23 张表**启用行级安全（RLS）（`schema.sql` 共 32 张 `CREATE TABLE`），且生产必须用非所有者角色连接否则 `gate-check` 直接判不通过。
7. **审批与审计**。无证据不能完成任务、店员不能验收、驳回必填原因；审计日志无删除接口。
8. **证据链与结果归因**。图片证据 + 哈希校验；`outcome_tracker.py` 记录 `accepted` / `rejected` / `false_positive` 三类反馈，并按 D+7 / D+28 窗口计算 `computed` / `no_data` / `pending` 结果。
9. **加密备份与恢复演练**。本地无密钥模式、AES-GCM、SHA-256 内容对账、30/12 保留策略、S3 假客户端契约测试。
10. **安全生命周期**。scrypt 口令、令牌只存哈希、12h TTL + 滑动续期、登录节流锁定、TOTP MFA、HttpOnly Cookie、CSRF 双提交。
11. **按角色过滤的只读工具**。发给模型的 tools 数组里就没有越权工具；即使模型编造工具名，调度器也会拒绝（有测试固化）。店员拿到的指标结果里不存在 `gmv` 字段。
12. **无密钥默认模式**。没有模型 Key 退回规则模式；没有 webhook 只出草稿；告警写 outbox 草稿；回调走 mock。全链路可离线验证。

### 5.4 口径差异与勘误

| 项 | 现有文档说法 | 本次实测 | 判定 |
|---|---|---|---|
| `/api/*` 路由数 | `PRODUCTION_READINESS.md`：45 条 | `app_fastapi.py` 实测 **50 条** `@app.<method>("/api/...")`；`README.md` 5.1 节也写 50 条 | **以 50 条为准**；`PRODUCTION_READINESS.md` 的 45 条已过期，待后续单独同步（本文档不改该文件） |
| Python 用例数 | `README.md` / `PRODUCTION_READINESS.md`：499 | 复跑 **499 passed**（229.9s） | 一致，以 499 为准 |
| Playwright 用例数 | 30 个（desktop + mobile） | 本次未复跑（不属于只读文档任务范围） | 沿用文档记录，标注"本次未复跑" |
| RLS 表数 | `PRODUCTION_READINESS.md` 批次 8c 记录：17 张表启用 RLS | `schema.sql` 实测 `ENABLE ROW LEVEL SECURITY` **23 处**，`CREATE TABLE` 共 32 处 | **以 23 为准**；17 是批次 8c 当时的快照，已过期 |
| `门店运营系统-结构与差距分析及调整方案.md`（仓库内） | 初稿判断（部分"未实现"结论已过期） | 其文件头部已自带状态更新声明 | 沿用其声明，以 `PRODUCTION_READINESS.md` + `README.md` 为准 |
---

## 6. 差距矩阵

"差距"分级：**低** = 已达到或超过对标；**中** = 有等效能力但形态不同；**中高** = 能力雏形存在但缺关键机制；**高** = 完全缺失或形态落后一代。

| # | 维度 | 本项目现状（可定位） | StaffDeck | CrewMeld | 差距 | 本期目标 | 可复用模块 |
|---|---|---|---|---|---|---|---|
| 1 | 身份与能力档案 | 有 `users`/`memberships` 与 RBAC 权限点（`auth.py`）；**无"数字员工"实体**，无岗位/工号/能力档案/运行状态 | 数字员工有岗位、工号、能力档案、工作记录，支持能力成长/权限隔离/发布复用 | 数字员工 = 身份信息 + 工具实例 + 模型配置 + 知识库绑定 + 运行状态 + 运营指标 | 中 | 否（P5） | `auth.py`、`registry.py`、`users`/`memberships` |
| 2 | SOP / 流程编排 | `prototype.generate_daily_plan()` 硬编码 4 类动作草稿（纯函数）；`task_store.py` 有任务状态机但流程不可编排 | 状态机驱动的流程技能；自然语言→结构化 SOP；多流程切换、上下文保持、可视化编辑、版本管理、分支演化 | 画布编排；4 类步骤；8 态状态机；断点续跑；步骤级超时；SOP 级默认 24h | **高** | **是（P2）** | `task_store.py` 状态机与指纹去重、`governance.py` |
| 3 | 能力暴露与作用域 | 权限硬编码在方法内（`agent_tools.StoreTools._can()` + 权限点字符串）；工具清单固定 8 个；模型 tools 数组按角色过滤 + 调度器拒绝 | `CapabilityScope`（GENERAL / SOP_SPECIFIC）+ `SkillCapabilityRefs` **节点级**能力暴露与子集校验 | 员工级绑定，仅已部署实例可绑 | 中高 | **是（P0）** | `auth.py` 权限点、`agent_tools.py` 角色过滤、`llm_agent.ToolSpec` |
| 4 | 工具接入与沙箱 | `agent_tools.StoreTools` 进程内 facade；无模板/实例、无沙箱、无 MCP/OpenAPI 导入；README 14.4 第 6 条**明令禁止运行时 shell 调用与动态执行** | `harness/`（沙箱、网络策略、资源上限、产物）+ `tools/`（执行器、MCP、HTTP、A2A、外部任务）+ `ToolExecutionPolicy` | 模板/实例两级、OpenSandbox 容器隔离、ZIP 迁移清空 secret、Dev Studio 三类服务 | **高** | **是（P3）** | `agent_tool_runs` 留痕、`security.py` 凭据自检、`gate-check` 危险能力扫描 |
| 5 | 知识库 / RAG | **完全没有**（无 `knowledge` 相关模块与表） | 文档结构感知检索（文档/章节/页/摘要多级索引），知识桶、定向检索、来源引用、检索调试；引用治理三条硬规则 | 知识库多对多绑定 + RAGFlow；客服模式运行时守卫（可检索、禁 SOP 与工具） | **高** | **是（P1）** | `auth.py` 脱敏、`platform_data.py` 校验与幂等范式 |
| 6 | 长期记忆 | 只有会话记录（`agent_sessions`/`agent_messages`），无画像/偏好/事实抽取 | `memory/service.py` 的 `MemoryRecord`，三类记忆 + 独立抽取提示词 + 按租户/用户/员工召回 | README 未单列 | 中 | **是（P4）** | `agent_sessions`/`agent_messages` |
| 7 | 反馈与进化 | `outcome_tracker.py` 已有反馈与 D+7/D+28 归因；`analysis.RULE_VERSION` 是手写常量。**有归因、无进化** | `evolution/`（`EvolutionProposal` + `SkillFeedback` + `MessageFeedback`）+ `skills/` 的蒸馏/编辑/反思 | README 未单列 | 中高 | **是（P4）** | `outcome_tracker.py`、`analysis.py` 的 `RULE_VERSION` |
| 8 | HITL 审批 | `governance.py` 审批流 + `approvals` 表 + 免登录回调（mock/飞书/企微，签名 + 时间窗 + nonce + 幂等回执）+ 审计；红线：只出草稿 | HITL 贯穿原则；决策记录明确"人始终可介入" | 人工审批是一等步骤；断点快照 + 免登录链接 + 先到先得 | **低**（已是强项） | 否（P2 复用，不重做） | `governance.py`、`approval_callbacks.py`、`notify.py` |
| 9 | 渠道触达 | 出站三通道（飞书/企微/outbox）+ 审批回调入站 + 签名校验；**无统一多渠道入站** | IM 系统 + 飞书 connector；`channels/` | 8 渠道 + CRON + 手动/Open API | 中 | 否（P5） | `notify.py`、`channel_store.py`、`security.verify_inbound()` |
| 10 | 多员工协作 | **单员工单店**；但 `tenant_id` + RLS + `registry.py` 已支撑多租户/多店 | `teams/` + `a2a/`；TL 由数字员工扮演 + 人工兜底；指派+认领+竞标（3 轮血条制）；黑板语义 | 数字员工与真人员工由 SOP 编排成多角色协作流 | **高** | 否（P5） | `tenant_id` + RLS + `registry.py` |
| 11 | 可观测 | `/livez`、`/readyz`、`/metrics`（Prometheus 文本）+ 结构化日志 + `agent_tool_runs` + `audit_events` + `ops_runs`（重试/dead letter） | `observability/`（事件日志、会话计时、span） | 执行记录列表、步骤级时间线、流式日志、待审批队列 | 低 | 部分（P2 补步骤级时间线） | `audit_events`、`agent_tool_runs`、`ops_runs` |
| 12 | 部署分发 | Docker Compose + Caddy HTTPS + 云主机；版本化迁移 + 恢复点 | 桌面端四平台分发 + 三档监听（local/lan/public）+ OIDC SSO | Compose + Helm；最小栈约 500 MB、带沙箱约 1.5 GB | 低（单店已够） | 否 | `compose.yaml`、`Dockerfile`、`deploy/` |

**矩阵读数**：12 个维度中，本项目在 **HITL 审批、可观测、部署分发**三项已达到或超过对标；在 **SOP 编排、工具与沙箱、知识库、多员工协作**四项落后一代；其余为中等差距。本期（P0-P4）聚焦的正是第 3、4、5、6、7 项。

---

## 7. 演进原则与明确不做清单

### 7.1 硬约束（不可回退）

1. **确定性优先**。规则引擎与阈值判定保持纯函数、可测、可解释；大模型只参与表达与调度，**不参与数值判定**。
2. **合规护栏不回退**。改价、赔付、外发、警告、奖惩一律只出草稿并带 `requires_approval`；敏感信息写入前脱敏。
3. **不编造**。无数据明确说"缺数"，无检索结果明确说"没找到"，不猜、不补。
4. **不推翻现有接口契约**。现有 50 条 `/api/*` 路由保持兼容；新能力一律走新增路由，且由 `test_api_coverage.py` 的路由契约测试继续锁定。
5. **真相源不变**。`store_configs` 与 `schema_migrations` 仍是配置与迁移的唯一真相源；所有新表走版本化迁移，不做裸 DDL。
6. **轻依赖**。优先复用现有 PostgreSQL 16 与进程内能力；**不把 Redis / MinIO / RAGFlow 设为前置依赖**。若某期出现硬性需求，在该期"风险与回退"中单独论证。
7. **安全红线延续**。口令只存 scrypt 哈希、令牌只存 SHA-256、生产必须用非所有者角色连 PG（否则 RLS 被绕过）、`gate-check` 的危险能力与明文密钥扫描不放松。
8. **无密钥可验证**。每一期都必须能在没有真实模型 Key、没有真实 webhook、没有真实 S3 的前提下完成端到端验证（沿用现有 mock/桩做法）。

### 7.2 明确不做清单（本期）

- 不引入独立向量数据库，不引入 RAGFlow。
- 不做对象存储（MinIO）；真实 S3 上传仍留在"待密钥"清单里，不属于本期。
- 不做多运行时工具服务发布（CrewMeld 的 `json` / `http` / `sse` 三类服务）；P3 只支持单一受控运行时。
- 不做拖拽式可视化编排（P2 只做最小可用的图编辑与预览）。
- 不做桌面端分发、OIDC SSO。
- 不做多团队竞标与血条赛制。
- 不做任何外部写操作的自动执行（改价/赔付永远只出草稿）。
- 不做演进提案的自动发布（提案必须人工确认，见 P4）。
- 不改动 `static/agent.css` 与 `tests/e2e/responsive.spec.js`（工作区既有未提交改动，保持原样）。
---

## 8. 分期路线（实施级）

### 8.0 通用约定

- 本节所有"新增模块""新增数据表""新增接口"**全部为【未实现】规格**，不代表已存在。
- 每期的完成定义（DoD）统一为：新增用例通过 + 原有 499 个 Python 用例全绿 + `npm.cmd run check:js` 通过 + `python store_cli.py gate-check` 通过 + 无密钥模式可端到端验证。
- 依赖顺序为 **P0 → P1 → P2 → P3 → P4 →（P5 远期）**。P1 与 P2 在 P0 完成后可并行；P3 依赖 P0；P4 依赖 P2。

---

### P0 能力注册与作用域底座

- **目标**：把 `agent_tools.py` 的进程内 facade 升级为**带 id、作用域、授权的能力注册表**，作为 P1-P4 的公共依赖。
- **能力边界**：只做能力的注册、描述、授权与调用契约。不做沙箱、不做远程执行、不改动现有 8 个只读工具的行为与返回结构。
- **新增模块与数据表**：`capabilities.py`（注册表 + `CapabilityDescriptor` + `CapabilityScope`）、`capability_scope.py`（作用域枚举）；表 `capabilities`（能力定义）、`capability_bindings`（按租户/门店/角色绑定与作用域）。
- **接口变更**：新增 `GET /api/capabilities`（按当前身份过滤返回可用能力清单）。现有 50 条路由不变。
- **与现有模块的接缝**：`agent_tools.StoreTools` 的 8 个方法注册为 capability，facade 退化为适配层；`llm_agent.ToolSpec` 改为由注册表生成；调用留痕继续写 `agent_tool_runs`；权限判定复用 `auth.py` 的权限点，不新造一套。
- **验收标准**：注册表能列出全部已注册能力及其作用域；越权调用被拒绝且有留痕；`llm_agent` 发给模型的 tools 数组与按角色过滤行为与改造前逐字一致（用现有 `test_agent_tools.py` / `test_llm_agent.py` 做回归锚点）；新增用例覆盖"required 作用域 ⊆ 允许作用域"的校验。
- **风险与回退**：注册表与 facade 并存可能形成两套真相源 → 明确**注册表为唯一真相源**，facade 只做适配；回退方案是保留 facade 直连路径，注册表降级为只读清单。

---

### P1 知识库 RAG

- **目标**：让门店手册、平台规则、历史复盘可被检索，并在回答中带**可回链的来源引用**。
- **能力边界**：只做"文档 → 结构化索引 → 带引用检索"。**不做**自动写回知识库（留给 P4）、不做多模态 OCR、不做概念层与多级索引（StaffDeck 的 `okf.py` 概念层不照搬）。
- **新增模块与数据表**：`knowledge.py`（解析、切片、索引、检索、引用）；表 `knowledge_bases`、`knowledge_documents`、`knowledge_chunks`、`knowledge_citations`、`knowledge_ingest_jobs`。
- **检索实现**：默认落在现有 PostgreSQL。优先启用 `pgvector` 扩展做向量检索；`CREATE EXTENSION vector` 不可用时**退化为表内结构索引 + 关键词检索**（文档/章节两级 + 中文应用层切分），不引入外部向量库。
- **接口变更**：新增 `POST /api/knowledge/search`、`POST /api/knowledge/documents`（导入）、`GET /api/knowledge/bases`；`POST /api/chat` 响应**新增** `citations` 字段（向后兼容，不删改现有字段）。
- **与现有模块的接缝**：`agent_tools` 新增 `search_knowledge` 能力并走 P0 注册表；引用内容复用 `auth.py` 的脱敏函数；导入校验与幂等沿用 `platform_data.py` 的批次哈希范式；`snapshot_builder` / `prototype` 不动。
- **验收标准**：导入一份门店手册后，检索命中且引用可回链到原文位置；**无命中时明确回答"知识库没有相关内容"，不得编造出处**（对齐现有"缺数不编数字"的降级原则）；引用编号按首次出现顺序重排；超出作用域的知识桶不可检索；`citations` 字段缺省时不改变既有响应。
- **风险与回退**：中文分词与切分质量不确定 → 先用可测的"标题/章节 + 关键词"路线打底，向量作为增强项，任何一步都可独立关闭；回退方案是纯关键词检索。

---

### P2 SOP 状态机 + 技能图

- **目标**：把硬编码的 4 类动作草稿升级为**可编排、可断点续跑、可版本化**的 SOP 图。
- **能力边界**：节点图（数字员工节点 / 人工节点 / 人工审批节点 / 条件分支）、**节点级能力作用域**、断点续跑、版本管理、最小可用的图编辑与预览。不做多团队协作（P5）、不做拖拽画布。
- **新增模块与数据表**：`sop.py`（图定义、校验、版本）、`sop_runtime.py`（执行状态机 + 断点快照）；表 `sops`、`sop_versions`、`sop_runs`、`sop_step_runs`。
- **状态机**：对齐 CrewMeld 的 8 态 —— `pending` / `running` / `completed` / `human-waiting` / `timeout` / `error` / `failed` / `cancelled`；所有转换做合法性校验，非法转换直接拒绝；步骤级超时自动追加"超时"出口；SOP 级最长执行时长默认 24 小时、可配置。
- **接口变更**：新增 `POST /api/sops`、`POST /api/sops/{id}/versions`、`POST /api/sops/{id}/runs`、`GET /api/sop-runs/{id}`、`POST /api/sop-runs/{id}/resume`。审批节点**复用**现有 `/api/approvals*` 与 `/api/approvals/callback/*`，不新建审批通道。
- **与现有模块的接缝**：复用 `task_store.py` 的状态机语义与指纹去重；审批与留痕复用 `governance.py`（`approvals`、`agent_tool_runs`）；**`prototype.generate_daily_plan()` 保留为"规则节点"的一种执行器**，不做重写。边界必须写清：`task` 是"派发给人的工作项"，`sop_run` 是"跨角色流程实例"，任务挂在 `sop_step_run` 上。
- **验收标准**：一条"日巡店"SOP 全链路跑通——人工节点提交 → 审批节点暂停并落快照 → 免登录链接或回调恢复 → 完成；非法状态转换被拒且有测试；保存版本产生新版本，且旧运行仍能解释自己用的是哪个版本；断点恢复后上下文不丢；步骤级时间线可通过现有 `audit_events` 查询。
- **风险与回退**：与 `task_store.py` 的状态机语义重叠，易出现两套状态互相矛盾 → 用上面的边界定义隔离，并用测试固化"task 状态变更不直接改写 sop_run 状态"；回退方案是保留 `prototype` 直接生成任务的旧路径，SOP 作为可选编排层。

---

### P3 工具模板/实例 + 沙箱执行

- **目标**：工具从进程内 facade 升级为**可登记、可部署、可隔离执行**的资产。
- **能力边界**：模板与实例两级、凭据隔离、manifest 导入导出（导出时清空 secret）、沙箱执行。**只支持一种受控运行时**（Python），不做 Node.js / SSE 服务发布。
- **新增模块与数据表**：`tool_registry.py`、`tool_manifest.py`、`tool_sandbox.py`；表 `tool_templates`、`tool_instances`、`tool_credentials`。
- **沙箱规格**：独立子进程 + 受限工作目录 + 资源上限（对齐 StaffDeck `HarnessLimits` 的量级：单次读取 1 MiB、单文件 10 MiB、工作区 100 MiB、条目数 1000、结果 200 KiB）、网络策略 `deny` / `allowlist` / `all`（默认 `deny`）、执行超时 1-3600 秒、`sync` 与 `detached` 两种执行模式并支持轮询跟踪。
- **接口变更**：新增 `GET /api/tools`、`POST /api/tool-instances`、`POST /api/tool-instances/{id}/invoke`、`GET /api/tool-templates/export`、`POST /api/tool-templates/import`。
- **与现有模块的接缝**：P0 注册表把 `tool_instance` 作为一种能力来源；`llm_agent.ToolCallingAgent` 的 `ToolSpec`、`DEFAULT_MAX_ITERATIONS = 4`、`DEFAULT_MAX_TOOL_CALLS = 8` 保留不变；调用留痕继续写 `agent_tool_runs`；凭据管理复用 `security.py` 的轮换与明文密钥自检。
- **验收标准**：导入一个模板 → 派生两个实例（各自不同凭据）→ 在沙箱内成功执行 → 导出的 ZIP 中 secret 字段为空 → 越权调用、超时、违反网络策略三类情况均被拒绝且有留痕 → 沙箱进程崩溃不影响主进程存活。
- **风险与回退**：**与现有安全红线直接冲突**——README 14.4 第 6 条明令"运行时模块禁止 shell 调用、任意删除与动态执行（`gate-check` 扫描）"。沙箱执行必然需要子进程调用，因此必须**显式新增独立扫描白名单**（只豁免 `tool_sandbox.py` 的受控子进程入口，并写明理由注释），**不得静默绕过或放宽全局扫描**。回退方案是只保留"工具模板元数据 + 进程内执行"，不启用沙箱。

---

### P4 记忆 + 反馈进化闭环

- **目标**：让"做过的判断"沉淀为可复用记忆，让结果归因驱动规则与技能的**版本迭代**。
- **能力边界**：长期记忆的抽取与召回（`profile` / `preference` / `fact` 三类）；反馈聚合 → 演进提案 → **人工确认** → 新版本生效。**不做**自动发布、不做自动调参。
- **新增模块与数据表**：`memory.py`（抽取、召回、去重）、`evolution.py`（反馈聚合 → 提案）；表 `memory_records`、`evolution_proposals`。
- **接口变更**：新增 `GET` / `POST /api/memory`、`GET /api/evolution/proposals`、`POST /api/evolution/proposals/{id}/decide`。
- **与现有模块的接缝**：直接消费 `outcome_tracker.py` 的反馈与 `outcomes` 结果；接 `analysis.py` 的 `RULE_VERSION`；**提案通过后生成新的规则版本号，不覆盖旧版本**，保证历史任务仍能解释自己用的是哪一版规则；决策动作写入 `audit_events`。
- **验收标准**：D+7 结果产出后能生成一条具体的演进提案（例如"某阈值建议由 10% 调整为 X%"）并给出来源数据；提案必须人工确认后才生效（未确认时行为与现在完全一致）；新旧规则版本可同时查询，历史任务可追溯所用版本；记忆召回应按租户/门店/角色隔离。
- **风险与回退**：自动调整阈值会侵蚀"确定性"这一护城河 → 硬约束为**提案只建议、参数变更必须人工确认并写审计**，且规则引擎在提案未确认前保持行为不变；回退方案是只落地记忆，不做提案。

---

### P5 多员工/团队与渠道扩展（远期）

- **目标**：支持多数字员工（岗位、能力档案、工号）、团队协作与任务认领，以及统一的多渠道入站。
- **能力边界**：仅定义能力边界，不写细节实现。团队领导采用"数字员工扮演 + 人工兜底"的混合制；任务机制采用"指派 + 认领"，**本期不引入竞标与血条赛制**；渠道先做飞书与企微的**入站**统一收口。
- **前置条件**：P0 能力作用域底座、P2 SOP 状态机、P3 工具隔离**全部完成**；`tenant_id` + RLS + `registry.py` 已具备的多店支撑不得重复建设。
- **与现有模块的接缝**：复用 `notify.py` 的出站三通道与 `channel_store.py` 的绑定关系；入站签名校验复用 `security.verify_inbound()`。
- **风险与回退**：多员工会显著放大权限与审计面 → 必须以 P0 的作用域为唯一授权入口，禁止在业务代码里新增硬编码权限判断。

---

### 8.9 依赖与推进建议

```
P0（公共底座）──┬── P1（知识库）──┐
                └── P2（SOP 状态机）┴── P4（记忆 + 进化闭环）
                └── P3（工具 + 沙箱）
P5（远期）← 依赖 P0 + P2 + P3
```

推进建议：P0 单独一期收口（它是所有后续能力的前置）；P1 与 P2 在 P0 之后可并行；P3 独立推进；P4 必须在 P2 之后，因为它要消费 SOP 的执行结果与结果归因。
---

## 9. 文档自身核验记录

本节记录本文档撰写时的实际核验动作与结果，便于后续判断文档是否已过期。

### 9.1 对标事实抽样回查（5/5 通过）

2026-10-20 通过 GitHub raw 内容抓取，对本文档 5 条关键断言做了字符串级回查：

| # | 断言 | 回查目标 | 结果 |
|---|---|---|---|
| 1 | `skill_schema.py` 定义 `SkillGraphNode` 与 `SkillCapabilityRefs`，且含 `required_tool_ids` | `backend/app/skills/skill_schema.py` | 通过 |
| 2 | `HarnessLimits` 的 5 项资源上限（1 MiB / 10 MiB / 100 MiB / 1000 / 200 KiB） | `backend/app/harness/contracts.py` | 通过 |
| 3 | 沙箱网络策略为 `all` / `allowlist` / `deny` | `backend/app/harness/sandbox.py` | 通过 |
| 4 | 引用治理包含 `SOURCE_FOOTER_PATTERN` 与 6000 / 800 字符上限 | `backend/app/knowledge/citations.py` | 通过 |
| 5 | 8 态状态机含 `human-waiting`、`cancelled` 与断点续跑 | `proinsight-io/crewmeld` `README.md` | 通过 |

### 9.2 本仓库结论抽样回查（8/8 通过）

| # | 断言 | 结果 |
|---|---|---|
| 1 | `prototype.generate_daily_plan` 纯函数存在 | 通过 |
| 2 | `analysis.py` 含 `RULE_VERSION = "1.0"` 与三个阈值常量 | 通过 |
| 3 | `outcome_tracker.py` 含三类反馈与三种结果状态 | 通过 |
| 4 | `agent_tools.py` 含 `StoreTools` 与 `PermissionDenied` | 通过 |
| 5 | `llm_agent.py` 含 `DEFAULT_MAX_ITERATIONS = 4` / `DEFAULT_MAX_TOOL_CALLS = 8` | 通过 |
| 6 | 不存在 `knowledge.py` / `memory.py` / `sop.py` / `tool_registry.py` / `evolution.py` | 通过 |
| 7 | README 14.4 声明禁止运行时 shell 调用与动态执行 | 通过 |
| 8 | `schema.sql` 含行级安全定义 | 通过 |

### 9.3 基线复跑

| 命令 | 结果 | 说明 |
|---|---|---|
| `python -m unittest discover -p "test_*.py"` | **Ran 499 tests, OK**（229.9s） | 与 `README.md` / `PRODUCTION_READINESS.md` 声明的 499 一致 |
| `npm.cmd run check:js` | 通过（退出码 0） | 前端与 e2e 脚本语法检查 |
| Playwright 浏览器用例 | 本次未复跑 | 不属于本次只读文档任务范围；沿用文档记录的 30 个 |

补充实测：`app_fastapi.py` 中 `@app.<method>("/api/...")` 共 **50** 条；`schema.sql` 中 `CREATE TABLE` 共 **32** 处、`ENABLE ROW LEVEL SECURITY` 共 **23** 处。

### 9.4 改动边界

本次任务的实际改动边界（`git status --porcelain` 实测）：

```
 M static/agent.css                 ← 本次未触碰（任务开始前即为已修改状态）
 M tests/e2e/responsive.spec.js     ← 本次未触碰（任务开始前即为已修改状态）
?? BENCHMARK_AND_ROADMAP.md         ← 本次唯一新增文件
```

即：**只新增本文档一个文件，未修改任何生产代码、测试、配置或其他文档**。

### 9.5 已知待同步项（本文档不代为修改）

- `PRODUCTION_READINESS.md` 的"45 条 `/api/*` 路由"应更新为 **50 条**。
- `PRODUCTION_READINESS.md` 的"17 张表启用 RLS"应更新为 **23 张**（`schema.sql` 共 32 张表）。
- 上述两项属于现有文档的口径维护，本文档遵循"只新增、不改动既有文件"的边界约定，故仅在此登记。

---

## 10. 附录

### 10.1 来源链接与抓取日期

抓取日期：**2026-10-20**（Asia/Shanghai）。

| 来源 | 链接 |
|---|---|
| StaffDeck 仓库 | https://github.com/OpenBMB/StaffDeck |
| StaffDeck README（英文） | https://github.com/OpenBMB/StaffDeck/blob/main/README.md |
| StaffDeck 多 Agent 团队决策记录 | https://github.com/OpenBMB/StaffDeck/blob/main/design-multi-agent-team-decisions.md |
| StaffDeck 模型 API 协议落地方案 | https://github.com/OpenBMB/StaffDeck/blob/main/design-model-api-protocols.md |
| StaffDeck 技能图定义 | https://github.com/OpenBMB/StaffDeck/blob/main/backend/app/skills/skill_schema.py |
| StaffDeck 沙箱契约 | https://github.com/OpenBMB/StaffDeck/blob/main/backend/app/harness/contracts.py |
| StaffDeck 引用治理 | https://github.com/OpenBMB/StaffDeck/blob/main/backend/app/knowledge/citations.py |
| CrewMeld 仓库 | https://github.com/proinsight-io/crewmeld |
| CrewMeld 官网 / 手册 | https://crewmeld.ai/ ；https://proinsight.gitbook.io/crewmeld |
| WorkAvatar（仅定位参考） | https://github.com/zengzag/WorkAvatar |

### 10.2 术语对照

| 本文档用词 | StaffDeck | CrewMeld | 本仓库对应物 |
|---|---|---|---|
| 数字员工 | Digital Employee / 数字员工 | Digital Employee | **暂无实体**（最接近的是有效用户 + 角色） |
| 能力 | Capability | Tool / Knowledge Base | `agent_tools.StoreTools` 的方法 + 权限点 |
| 能力作用域 | `CapabilityScope`（GENERAL / SOP_SPECIFIC） | 员工级绑定 | 角色级权限点（硬编码） |
| SOP / 流程 | Skill（图 + 状态机） | SOP | `prototype.generate_daily_plan()` 的固定 4 类动作 |
| 工具模板 / 实例 | Tool（模板/实例） | Tool Template / Instance | 无（仅进程内 facade） |
| 知识库 | Knowledge Base / Bucket | Knowledge Base（RAGFlow） | 无 |
| 记忆 | Memory（profile / preference / fact） | 未单列 | 仅会话记录 |
| 演进提案 | `EvolutionProposal` | 未单列 | 无（仅有 `outcome_tracker` 归因） |
| 断点续跑 | 状态机 + 会话持久化 | 人工审批节点暂停 + 状态快照 | 无 |
| 人工介入 | HITL | Human Approval | `governance.py` 审批流 + 免登录回调 |

---

**文档结束。** 第 8 章全部为待实现规格；本文档不改变任何现有实现。