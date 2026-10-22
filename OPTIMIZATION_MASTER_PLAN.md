# 数字员工系统总优化方案（功能缺口 + 能力底座）

- 版本：v1.0
- 日期：2026-10-20
- 状态：**规划稿，本文档是本系统路线图的唯一真相源**。所有新增模块、数据表、接口均为**待实现规格**【未实现】，不代表已存在。
- 适用范围：`E:\项目\aiyuangong\门店运营系统\`（武汉狮子座店 `WH-LION-001` 单店试点）
- 盘点基准：`index.html` 第一节定义的**九大功能域**
- 排序主轴：**经营闭环优先**（先把单店经营效果做实，再铺能力底座）
- 对标依据：`BENCHMARK_AND_ROADMAP.md`（对标事实与架构拆解在本文档不重复展开）

## 真相源约定

| 主题 | 以谁为准 |
|---|---|
| 当前实现现状 | `README.md` + `PRODUCTION_READINESS.md` |
| **路线图与缺口清单** | **本文档**（`BENCHMARK_AND_ROADMAP.md` 第 8 章的路线图已由本文档接管） |
| 对标事实与架构机制拆解 | `BENCHMARK_AND_ROADMAP.md` 第 1-4 章 |
| 本仓库能力基线与护城河 | `BENCHMARK_AND_ROADMAP.md` 第 5 章 |
| 【未实现】标记 | 本文档中所有标注【未实现】的模块/表/接口均为待实现规格 |

---

## 1. 盘点口径

### 1.1 基准来源

九大功能域取自 `门店运营系统\index.html` 第一节"一个 Agent，九个功能域"：

| # | 功能域 | 该文档设定的职责 |
|---|---|---|
| 1 | 经营驾驶舱 | 营收、开台、订单、核销、毛利与异常 |
| 2 | 平台运营 | 美团、抖音等平台曝光、访问和转化 |
| 3 | 评价与客服 | 真实评价邀请、差评 SLA、问题复盘 |
| 4 | 团购与商品 | 套餐结构、价格、库存、时段和审批 |
| 5 | 任务与巡店 | 每日动作、证据、复核、逾期升级 |
| 6 | 员工执行 | 完成率、质量、提醒、申诉和奖惩建议 |
| 7 | 竞品监控 | 公开价格、套餐、活动与商圈变化 |
| 8 | 内容运营 | 选题、短视频、发布审批与到店归因 |
| 9 | 随时对话 Agent | 按身份回答、解释任务、指导执行和发起审批 |

### 1.2 五级判定

| 判定 | 含义 | 取证要求 |
|---|---|---|
| **已落地** | 有数据模型 + 运行时逻辑 + 测试 | 必须能指到具体文件与符号 |
| **部分落地** | 主体可用，但缺关键维度或实体 | 必须指出缺什么 |
| **已建模未接线** | `schema.sql` 有表定义，生产代码 0 引用 | 必须给出生产模块命中数 |
| **仅规划未建模** | 只出现在 `index.html` 等文档 | 必须给出文档出处与命中数 |
| **明确不做** | 文档已给出 P2 / 禁止结论 | 必须引用原文结论 |

### 1.3 取证方法

- 对代码判定使用**符号级检索**（文件 + 行号 + 命中数），不使用"大概有"这类表述。
- 对"已建模未接线"的表，用**生产模块命中数**（排除 `test_*.py`）取证，命中数为 0 即判定未接线。
- 对只在文档出现的功能，用关键词跨文件分组命中取证，以区分"代码里有"和"只有文档里有"。

---

## 2. 九大功能域覆盖矩阵

| # | 功能域 | 判定 | 代码证据 | 缺口 |
|---|---|---|---|---|
| 1 | 经营驾驶舱 | **部分落地** | `reporting.py` 的 `StoreDashboard.dashboard()` 聚合曝光/GMV/退款/评价并给出 5 个派生率；`analysis.py` 做周期归因；`dashboard.html` 现有 7 张卡片；毛利来自 `snapshot_builder._margin_rate()` + `store_config.json` 的 `package_costs` | **开台（翻台/客流）没有任何数据模型**：`开台` 仅出现在 `index.html`（2 处） |
| 2 | 平台运营 | **已落地** | `platform_data.py` 的 `METRICS` 共 9 项指标；5 个派生率 `view_rate`/`pay_rate`/`verify_rate`/`refund_rate`/`negative_rate` 见 `reporting.aggregate()` 与 `analysis.RATE_METRICS`；三类数据集按表头自动识别 + SHA-256 批次哈希幂等 | 数据仍靠人工导出（`connector_accounts` 表未接线） |
| 3 | 评价与客服 | **已落地** | 两条规则：`{date}-review-invite`（reputation 类）与 `{date}-negative-{review_id}`（reputation 类，含 SLA）；`reviews` 表三态 `open`/`replied`/`resolved`；SLA 阈值 `store_config.json.rules.negative_review_sla_hours`（默认 4 小时） | 无（合规边界明确：不购买、不诱导、不伪造评价） |
| 4 | 团购与商品 | **部分落地** | `PACKAGE_METRICS`（detail_views/paid_orders/verified_orders/gmv）+ `package_costs` + 套餐规则 `{date}-package-{package_id}`（含"一次只改一个变量"约束）；`offers` 表**未接线** | **库存、时段无数据模型**：`库存` 仅 4 处建议文案（`store_agent.py` 与 `analysis.py`），`时段` 仅 `index.html`（4 处） |
| 5 | 任务与巡店 | **部分落地** | `task_store.py` 状态机 + 指纹去重 + 逾期扫描；`evidence_store.py` 图片证据与哈希校验；任务升级规则 `{date}-escalation-{task_id}` | **巡店无独立流程**：`巡店` 仅在 `index.html` 出现 1 处，无巡店模板、检查项与复核实体 |
| 6 | 员工执行 | **部分落地** | `task_completion()` 完成率；`notify.py` 四类提醒；`outcome_tracker.py` 的 `task_feedback` 三态 `accepted`/`rejected`/`false_positive`；奖惩只出草稿（`{date}-escalation` 属 management 类且 `requires_approval=True`） | **申诉无实体**：`prototype.py` 只在任务理由里写"需保留申诉入口"，`retention.py` 只为申诉预留 2 年保留期，但**无表、无接口** |
| 7 | 竞品监控 | **完全缺失**（本次重评为可做） | `analysis.py` 仅 1 处**建议文案**提到"对比同期竞品曝光与到店权益变化"，无数据模型、无规则 | 全部缺失。`index.html` 原结论为 P2，本次重评后按"人工采样 v1"进方案（见第 4 章第 2 期） |
| 8 | 内容运营 | **仅规划未建模** | `短视频` 仅出现在 `index.html`（3 处） | 维持 `index.html` 的 P2 结论，本文档**只登记不排期** |
| 9 | 随时对话 Agent | **已落地** | `store_agent.py` 的 `INTENT_RULES` 共 14 条意图规则；`llm_agent.py` 提供 8 个只读工具 + 按角色过滤（`DEFAULT_MAX_ITERATIONS = 4`、`DEFAULT_MAX_TOOL_CALLS = 8`）；无 Key 时降级到规则模式 | 无（对话能力面的缺口属于底座，见第 3 章 D 类） |

### 2.1 跨域发现：数据模型真相源不完整

`schema.sql` 自称"权威数据模型"，但实际存在例外：

- `schema.sql` 共 **32** 张 `CREATE TABLE`。
- `outcome_tracker.py` 自带 DDL 另建 **2** 张表：`task_feedback`、`task_outcomes`。
- 这 2 张表**既不在 `schema.sql`，也不在 `pg_sync.py`**（`pg_sync.py` 中检索这两张表名命中为 0）。

即"32 张表在 schema，2 张表在模块"。这会让"新表一律走版本化迁移"的约定出现裂缝，第 3 期必须一并收口（见 4.3）。
---

## 3. 缺口清单

缺口按**性质**分四类。四类的处理方式完全不同：A 类要补数据来源，B 类要补实体，C 类要接线，D 类要补底座。

### 3.1 A 类 · 数据维度缺口（有业务意图，无数据模型）

| 编号 | 缺口 | 归属功能域 | 证据 | 影响 |
|---|---|---|---|---|
| A1 | 开台 / 客流 | 经营驾驶舱 | `开台` 仅命中 `index.html`（2 处） | 无法回答"翻台率够不够"，经营驾驶舱缺一块 |
| A2 | 时段分布 | 经营驾驶舱、团购与商品 | `时段` 仅命中 `index.html`（4 处） | `index.html` 要求"一次只改主图/标题/价格/**时段**中的一个变量"，但时段既无数据也无规则支撑 |
| A3 | 库存 / 物料 | 团购与商品 | `库存` 仅 4 处建议文案（`store_agent.py`、`analysis.py`），无表 | `index.html` 的审批闸门写了"改价、**库存**、适用门店和发布必须审批"，但库存数据不存在，闸门无对象可管 |
| A4 | 商圈 | 竞品监控 | 第 1 期已落地 `trade_areas`（**本店**所属商圈）；`商圈` 字样原本仅命中 `index.html`（3 处） | 竞品对比缺空间维度，无法界定"同商圈竞品"；**由第 2 期 `competitor_stores` 解决**：竞品门店带 `area_id` 指向本店 `trade_areas`，`trade_areas` 语义保持不变 |

### 3.2 B 类 · 制度有表述，无实体

这一类最值得注意：**制度与保留策略都已写好，但支撑它们的实体从未建立**。

| 编号 | 缺口 | 现有表述（证据） | 缺什么 |
|---|---|---|---|
| B1 | 申诉 | `prototype.py` 任务理由："纪律警告和奖惩必须由有权限的管理者确认，**且需保留申诉入口**"；`retention.py` 为任务与证据保留 2 年，注释写明是"任务与证据 2 年（**申诉与复盘**）" | 无申诉表、无申诉接口、无受理与结论流程。申诉入口只有一句要求，没有落点 |
| B2 | 巡店 | `index.html` 的"任务与巡店"功能域设定"每日动作、证据、复核、逾期升级" | 无巡店模板、检查项、缺项复核实体。巡店目前只能隐含在普通任务的 `category` 里 |
| B3 | 奖惩闭环 | 现有设计正确：`{date}-escalation` 只生成草稿并要求审批，`llm_agent.py` 与 `store_agent.py` 也把奖惩限定为建议 | 缺"申诉 → 复核 → 结论"的闭环，导致 B1 的制度要求悬空 |

> 说明：B3 的"只出草稿"是**正确的现状**（红线），不是缺陷。缺陷在于这条红线缺少配套的申诉出口。

### 3.3 C 类 · 已建模未接线

`schema.sql` 头部明确把模型分为两段：**A. 已落地** 与 **B. 规划中（未接线，勿在生产启用）**。B 段共 5 张表，生产模块命中数全部为 **0**：

| 编号 | 表 | 原设计目标（`schema.sql` 注释） | 对应的功能缺口 |
|---|---|---|---|
| C1 | `action_rules` | 面向后续批次（审批中心） | `prototype.py` 的 4 类规则全是硬编码，阈值改动要走改代码 + 发版，无版本、无审批、无回滚。表已定义 `rule_key`/`version`/`scope`/`condition_json`/`action_template_json`/`status(draft,approved,paused,retired)`/`approved_by`/`approved_at` |
| C2 | `offers` | 面向后续批次 | 套餐资产（`sale_price`/`estimated_variable_cost`/`status`）已有表结构，实际仍靠 `store_config.json` 的 `package_costs` 手工维护 |
| C3 | `connector_accounts` | 平台连接器 | 平台数据仍靠人工导出，无法记录授权账号与连接状态 |
| C4 | `ingestion_runs` | 平台连接器 | 入库运行记录散落在 `metric_import_batches`，未统一 |
| C5 | `metric_facts_daily` | 指标长表 | 当前为指标宽表，新增维度（如时段、库存）时扩展性受限 |
| C6 | `task_feedback`、`task_outcomes` | — | 由 `outcome_tracker.py` 自带 DDL 创建，未纳入 `schema.sql` 与 `pg_sync`（见 2.1） |

其中 **C1 是杠杆最大的一条**：规则不可版本化，等于经营闭环的最后一个环节（"改进"）没有落点——D+7/D+28 的结果算得出来，却没有地方承接调整。

### 3.4 D 类 · 跨域能力底座缺口

这一类的详细对标见 `BENCHMARK_AND_ROADMAP.md` 第 6 章的十二维差距矩阵，此处只登记与分期：

| 编号 | 缺口 | 现状（可定位） | 对应期次 |
|---|---|---|---|
| D1 | 能力暴露与作用域 | 权限硬编码在 `agent_tools.StoreTools._can()`，工具清单固定 8 个 | 第 5 期 |
| D2 | 知识库 / RAG | 完全没有（无相关模块与表） | 第 6 期 |
| D3 | SOP 流程编排 | `prototype.generate_daily_plan()` 硬编码规则，流程不可编排 | 第 7 期 |
| D4 | 工具接入与沙箱 | `agent_tools.StoreTools` 进程内 facade，无模板/实例、无沙箱 | 第 8 期 |
| D5 | 长期记忆与反馈进化 | 有 `outcome_tracker` 归因，无记忆与进化 | 第 9 期（规则侧已在第 3 期完成） |
| D6 | 多员工协作与渠道入站 | 单员工单店；渠道只有出站三通道 + 审批回调入站 | 第 10 期 |

### 3.5 缺口总览

| 类别 | 数量 | 处理方式 | 落点 |
|---|---|---|---|
| A 数据维度缺口 | 4 | 补数据来源与数据模型 | 第 1 期（A4 由第 2 期消费） |
| B 制度有表述无实体 | 3 | 补实体与流程 | 第 4 期 |
| C 已建模未接线 | 6 | 按需转正 + 收口 | 第 3 期（C1/C6），C2-C5 视需求随期转正 |
| D 跨域底座缺口 | 6 | 按依赖顺序补齐 | 第 5-10 期 |
---

## 4. 统一路线图（经营闭环优先，10 期）

### 4.0 通用约定

- 每期统一写全七个字段：**目标 / 能力边界 / 新增模块与数据表 / 接口变更 / 与现有模块的接缝 / 验收标准 / 风险与回退**。
- 所有"新增模块""新增数据表""新增接口"**全部为【未实现】规格**。
- 每期完成定义（DoD）统一为：新增用例通过 + 原有 499 个 Python 用例全绿 + `npm.cmd run check:js` 通过 + `python store_cli.py gate-check` 通过 + **无密钥默认模式**可端到端验证。
- 排序主轴为**经营闭环优先**：先用第 1-4 期把单店经营闭环做实，再用第 5-9 期铺平台化能力，最后做第 10 期扩张。
- 经营闭环链路为：**数据进来 → 看清 → 诊断 → 派单 → 执行与验收 → 归因 → 改进**。第 1 期补"数据进来"的维度，第 2 期补"看清竞品"的维度，第 3 期补"改进"的落点，第 4 期补"执行侧"的制度出口。

---

### 阶段一 · 经营闭环收口（第 1-3 期）

#### 第 1 期 经营数据入口扩展（开台 / 时段 / 库存 / 商圈）

> **状态：已于 2026-10-20 实施完成。** 落地模块 `store_ops_data.py`；六张新表；`/api/ops-data/status|summary|import` 三条接口；`ops:import` 权限；
> 看板两张默认关闭的卡片；低峰时段与库存补货两条仅出草稿的规则；`store_cli.py plan`、`scripts/daily_sync.py` 与 `import-dir` 已接入。
> 验证：`python -m unittest discover -p "test_*.py"` → **541 passed**（新增 42 个用例，见 `test_store_ops_data.py`）；`npm run check:js` 通过。
> 与原规划的差异：门店侧批次账本独立为 `ops_import_batches`（平台侧为 `metric_import_batches`），因为两者本来就在不同的库里。

- **目标**：把"看清"的数据维度从平台侧扩展到门店侧，让经营驾驶舱能回答"哪个时段效率低、哪些物料该补"。
- **能力边界**：4 类数据集走**人工导出 + `import-dir` 导入**。不做自动采集，不接入 POS/收银系统，不做多门店聚合。
- **新增模块与数据表**【未实现】：`store_ops_data.py`（4 类数据集的校验、导入、汇总）；表 `store_traffic_daily`（开台/客流）、`time_slot_metrics`（时段分布）、`inventory_items` 与 `inventory_alerts`（库存/物料与预警）、`trade_areas`（商圈，供第 2 期复用）。
- **接口变更**【未实现】：`POST /api/ops-data/import`、`GET /api/ops-data/status`、`GET /api/ops-data/summary`；`GET /api/dashboard` 响应**新增** `time_slots` 与 `inventory_alerts` 字段（向后兼容，不删改现有字段）。
- **与现有模块的接缝**：复用 `platform_data.py` 的表头自动识别 + SHA-256 批次哈希幂等 + 字段校验范式；复用 `snapshot_builder.build_snapshot()` 把新数据组装进规则输入；沿用 `analysis.py` 的"阈值常量 + 版本号"写法。`prototype.generate_daily_plan()` 新增两条规则（低峰时段提效、库存/物料预警），均 `requires_approval=True`。
- **验收标准**：4 类数据集可导入，且重复导入 `rows_written = 0`；单文件失败不影响其它文件且命令退出码为 1；dashboard 出现"时段分布"与"库存预警"两张卡片，**两张卡片各有独立开关且默认关闭**；两条新规则在缺数时**不产出任务**并给出"缺数"原因（延续"不编造"原则）；现有 499 用例 + 新增用例全绿。
- **风险与回退**：新增维度会让看板噪音变大 → 每张卡片独立开关且默认关闭；门店口径与平台口径混淆 → 新数据集一律带 `source` 字段并在看板标注来源；回退方案是只保留开台与时段两类，库存与商圈后置。

#### 第 2 期 竞品监控 v1（人工采样）

> **本节已于 2026-10-21 扩写为实施级设计。** 相对 2026-10-20 的条目级定义，本次厘清了商圈归属、取消了重复接口、补齐了对比口径与迁移要求。
> **最主要的调整**：原条目默认可复用 `trade_areas` 做商圈归属，但第 1 期实际落地的 `trade_areas` 是**本店所属商圈**（主键含 `store_id`），**无法表达竞品门店的商圈**——本期的 `competitor_stores` 就是为解决这一冲突而新增的。
> 本章全部落地项仍为**【未实现】**。

- **目标**：让"竞品监控"从一句建议文案变成可用的**四维对比**能力，回答"同商圈竞品的价格带、套餐结构、活动有没有变化，商圈整体在涨价还是降价"。
- **能力边界**：按 `index.html` 既定建议走**人工每周采样导入**，先解决数据合法来源与商品匹配。**不做自动抓取**，不自动执行任何竞品侧动作；**v1 不加看板卡片**（对比结果只经 API、对话与草稿任务呈现），避免重演"新维度一上线就把看板堆满"。
- **新增模块与数据表**【未实现】：`competitor.py`（采样导入、匹配、四维对比）；表 `competitor_stores`、`competitor_offers`、`competitor_matches`；导入台账**复用第 1 期的 `ops_import_batches`**，不另开第二本账。
- **接口变更**【未实现】：`GET /api/competitor/compare`、`GET /api/competitor/matches`、`POST /api/competitor/matches`。**原条目的 `POST /api/competitor/samples` 取消**——采样与其它数据集一样走统一收件箱（`import-dir` / `POST /api/ops-data/import`），不出现第二套导入通道。
- **与现有模块的接缝**：导入复用第 1 期的统一收件箱与批次哈希幂等；对比结果作为 `analysis.py` 诊断的**补充证据**（`evidence` 增加 `type: "competitor"`）而非新的结论来源；`prototype.py` 只新增本期一条规则，阈值写入快照 `competitor.thresholds` 便于审计；产出物沿用"草稿 + `requires_approval`"形态。
- **验收标准**：见本节"验收标准"。
- **风险与回退**：见本节"风险与回退"。

##### 调整点（相对条目级定义的 8 处）

1. **商圈归属重新建模**：新增 `competitor_stores`（竞品门店维度，带 `area_id`），`area_id` **必须存在于本店 `trade_areas`**（同租户、同门店），否则该行导入直接判错。`trade_areas` 保持第 1 期语义不动。
2. **取消重复接口**：`POST /api/competitor/samples` 与第 1 期已建的统一收件箱重复，取消。
3. **竞品数据接入统一收件箱**：新增第 8、9 类数据集，`detect_dataset` 按表头分投，且不得抢走平台侧三类与第 1 期四类的表头；CLI 补 `import-competitor-store` / `import-competitor-offer` 两个单文件入口。
4. **需要一条版本化迁移（v4）**：`ops_import_batches.dataset` 在 PostgreSQL 侧带 CHECK 枚举，而 `create table if not exists` 与 A 段重放**都不会修改已存在表的 CHECK 约束**。因此必须新增 `Migration(version=4)`：`drop constraint if exists` 掉旧约束，再以显式名 `ck_ops_import_batches_dataset` 重建；`schema.sql` 同步使用同一约束名，使新库与迁移后的库收敛。
5. **补合规留痕字段**：采样行必须带**采样人、采集方式、来源说明**。竞品信息是第三方公开数据，来源必须自证；不采集任何非公开数据、不做需要登录态的抓取。
6. **补对比口径的定量定义**：原条目只说"按商圈与价格带对比"，不足以实现。本期明确档宽、分位、窗口、最小样本与变化阈值（见"对比口径"）。
7. **新增一条规则**：`prototype.py` 新增类目为 `competitor` 的规则，`requires_approval=True`，只出草稿，每个商圈最多一条。
8. **明确不加看板卡片**：看板卡片留作后续按需增加（沿用第 1 期 `ops_cards` 的默认关闭模式）。

##### 数据模型【未实现】

| 表 | 关键字段 | 主键 / 幂等键 | 说明 |
|---|---|---|---|
| `competitor_stores` | `competitor_id`、`competitor_name`、`area_id`、`note`、`source_name`、`imported_at` | `(tenant_id, store_id, competitor_id)` | 竞品门店维度。`area_id` 必须存在于本店 `trade_areas`；`store_id` 始终是**本店**（采样主体） |
| `competitor_offers` | `competitor_id`、`sample_date`、`package_name`、`price`、`activity`、`activity_note`、`source_method`、`source_note`、`sampled_by`、`source_name`、`imported_at` | `(tenant_id, store_id, competitor_id, sample_date, package_name)` | **每次采样各保留一行**（不覆盖历史），这是环比的前提；`source_method` / `source_note` / `sampled_by` 用于合法来源自证 |
| `competitor_matches` | `platform`、`package_id`、`competitor_id`、`competitor_package_name`、`status`、`confirmed_by`、`confirmed_at`、`note` | 唯一 `(tenant_id, store_id, platform, package_id, competitor_id, competitor_package_name)` | `status` ∈ `pending` / `confirmed` / `rejected`；**只有 `confirmed` 参与对比** |

导入的数据集定义（与第 1 期四类并列，共九类）：

| 数据集 | 表头必需字段 | 落库 |
|---|---|---|
| 竞品门店（`competitor_store`） | `store_id,competitor_id,competitor_name,area_id`（可选 `note`） | `competitor_stores` |
| 竞品采样（`competitor_offer`） | `date,store_id,competitor_id,package_name,price` + **必填** `sampled_by,source_method,source_note`（合规留痕，见调整点 5）；可选 `activity,activity_note` | `competitor_offers` |

##### 匹配是硬闸门

- **不做任何自动或模糊匹配**：不存在"按名字猜"的候选生成。
- `competitor_matches` 由人工通过 `POST /api/competitor/matches` 创建；创建即视为人工挑选，默认落 `confirmed`，也可显式落 `pending`（暂存）或 `rejected`（显式排除）。
- **只有 `confirmed` 的映射进入价格与套餐对比**；`pending` / `rejected` 一律不参与。
- 对比结果中单列**"待匹配的竞品套餐数"**（出现在采样里但没有任何 `confirmed` 映射的竞品套餐），提示需要人工挑选，避免用错误匹配得出误判结论。
- **权限**：导入沿用第 1 期的 `ops:import`；**新增 `competitor:match`**，仅 `owner` / 区域负责人 / 店长持有，与导入权限分开。每次确认或驳回都写 `audit_events`。

##### 对比口径（四维）

| 维度 | 口径 | 输出 |
|---|---|---|
| **价格** | 同商圈、已确认匹配的竞品套餐价格 | 价格带分档分布（档宽 `competitor_price_band_size`，默认 20 元）+ P25 / P50 / P75 分位 + 样本数 |
| **套餐** | 已确认匹配的套餐 | 本店在售套餐数 vs 同商圈竞品套餐数；价格档位覆盖率（本店覆盖了哪些档、竞品覆盖了哪些档）；每个已确认匹配的**价差**。更细的结构（时长 / 内容项）需要后续扩展采样字段，v1 不做 |
| **活动** | 采样行的 `activity` | 有活动的竞品套餐数、活动类型分布、与本店活动是否同类；`activity` 仅作事实陈列，**不做效果推断** |
| **商圈变化** | 商圈价格带的中位数**环比** | 本窗口 vs **上一等长窗口**的中位数变化率；这是本期的主指标，回答"商圈整体在涨价还是降价" |

对比窗口与样本约束：

- 窗口：`competitor_window_days` 默认 **28 天**（按周采样约 4 个采样点），环比取上一等长窗口。
- 最小样本：`competitor_min_samples` 默认 **5**。**低于阈值只报"样本不足，不下结论"**，不输出分位与变化率（延续 `index.html` 的"低样本不下结论"）。
- 变化阈值：`competitor_area_shift_percent` 默认 **10%**。
- 规模上限：`competitor_max_matches` 默认 **50**，单次对比最多纳入的已确认匹配条数，避免超大商圈拖慢查询；超出时按竞品套餐名稳定排序取前 N 条，并在结果里如实标注"已截断"。
- 阈值全部写入快照的 `competitor.thresholds`，便于事后审计"当时用的是哪套口径"。

##### 规则【未实现】

`prototype.py` 新增**一条**规则，类目 `competitor`，`requires_approval=True`、`status=proposed`（只出草稿）。满足任一条件即触发：

1. 某商圈的**价格带中位数环比变化 ≥ `competitor_area_shift_percent`**；
2. 已确认匹配的套餐中，本店价格**低于同档 P25** 或**高于 P75**。

- `task_id`：`{date}-competitor-{area_slug}`，**每个商圈最多一条**，避免同一商圈刷出多条重复草稿。
- 产出形态沿用既有约定：理由是数据依据，动作是"复核竞品变化并提交单变量调整草稿"，证据要求包含"竞品采样截图 / 采样时间 / 唯一变更变量 / 审批记录"。
- **缺数据不产出任务**：无采样、样本不足、无已确认匹配时，`rule_readiness.competitor` 记 `degraded` 并写明原因，同时写入快照 `notes`。

##### 迁移与保留期同步【未实现】

- `schema.sql` A 段：新增 3 张表与其索引、RLS 策略；`ops_import_batches.dataset` 的 CHECK 改为显式命名 `ck_ops_import_batches_dataset` 并纳入两类新数据集。
- `Migration(version=4, name="competitor_monitoring")`：`drop constraint if exists ops_import_batches_dataset_check` → `drop constraint if exists ck_ops_import_batches_dataset` → `add constraint ck_ops_import_batches_dataset check (...)`。**这是本期的硬前置**：不新增迁移，既有 PostgreSQL 上的 `dataset` 枚举会拒绝新数据集。
- `pg_sync.py` 的 `TABLES` 补 3 条 `TableSpec`（`db="ops"`；`competitor_offers` 的 `date_columns` 为 `sample_date`）。
- `retention.py` 与 `store_config.json`：`competitor_offers` 保留 **1095 天**；`competitor_stores`、`competitor_matches` 属主数据，**永久保留（0）**；新增 5 个 `rules` 键（`competitor_price_band_size` / `competitor_window_days` / `competitor_min_samples` / `competitor_area_shift_percent` / `competitor_max_matches`）。未同步登记会使 `policy_from_config` 因"未知表"直接报错。

##### 验收标准【未实现】

- **表头识别**：两类新数据集能被 `import-dir` 识别，且**不抢走**平台侧三类与第 1 期四类的表头。
- **幂等与容错**：同一文件重复导入 `rows_written = 0`（状态 `skipped`）；单文件失败不影响其它文件且命令退出码为 1。
- **商圈完整性**：`area_id` 不在本店 `trade_areas` 的采样被拒绝并给出明确原因。
- **匹配闸门**：未确认匹配的竞品套餐**不进入**对比；`pending` / `rejected` 不影响结论；确认与驳回都有审计留痕；对比结果含"待匹配的竞品套餐数"。
- **对比口径**：价格带分档与 P25 / P50 / P75 计算正确；环比取上一等长窗口；样本低于 `competitor_min_samples` 时只报"样本不足"，不输出结论性数字。
- **规则**：满足触发条件时每个商圈产出 **1 条** `competitor` 类草稿（`requires_approval=True`、`status=proposed`）；缺数据时不产出任务且 `rule_readiness` 给出原因。
- **如实回报**：无采样 → "本期无竞品采样数据"；无已确认匹配 → 不产出价格与套餐对比。延续"缺数不编造"。
- **回归**：现有 541 个 Python 用例 + 34 个 e2e 用例保持全绿；`python store_cli.py gate-check` 通过。

##### 风险与回退【未实现】

- **数据合法性与反爬风险** → v1 只接受人工导出文件，系统不做任何网络抓取；采样行强制带采样人、采集方式与来源说明，来源可自证。
- **商品匹配错误导致误判** → 匹配必须人工确认且需 `competitor:match` 权限，对比结果标注确认人与确认时间；未确认一律不参与。
- **商圈定义漂移**（本店商圈改了导致历史采样失去归属）→ `competitor_offers` 保留每次采样原值，不因 `trade_areas` 变动而回写历史。
- **样本太少得出误导性结论** → 最小样本阈值 + "样本不足不下结论"；阈值写入快照可追溯。
- **回退方案**：停用对比能力，仅保留采样台账与人工查看（数据不丢）；若 `Migration v4` 尚未执行，新数据集导入会被数据库约束拒绝，此时**不降级为静默跳过**，而是明确报错要求先迁移。

##### 实施批次（开工方案）

**开工前检查**（五条都要先过）：

1. 工作区基线为绿：`python -m unittest discover -p "test_*.py"` → **541 passed**；`$env:PLAYWRIGHT_CHANNEL='chrome'; npm run test:e2e` → **34 passed**（desktop + mobile）；`npm run check:js` 通过；`python store_cli.py gate-check` → `ok: true`。
2. **端口 8766 必须空闲**：e2e 会自启服务，若端口被陈旧进程占用则会被 `reuseExistingServer` 复用。已有实例证明——陈旧的测试服务不带 `STORE_AGENT_MFA_ENFORCE=1` 时登录不返回 MFA 要求，表现为 10 条 e2e 断言失败，**与代码改动无关**。开工前先确认端口空闲。
3. 工作区既有的未提交改动（`static/agent.css`、`tests/e2e/responsive.spec.js`）**保持不动**，不纳入本期任何批次。
4. 数据落点确认：本期新表全部落在**门店侧库**（`--store-db`，即 `store_ops.db`），平台指标库（`--db`）不动。
5. 约束放宽只允许发生在一处：`Migration v4`。**不得在业务代码里绕过 `ops_import_batches.dataset` 的约束**。

**依赖关系**：五个批次**严格串行**，不做并行。

```
A 迁移与数据底座（硬前置）
  └─ B 竞品数据导入
       └─ C 匹配闸门与权限
            └─ D 四维对比与规则
                 └─ E 文档与全量收口
```

- A 不先做完，后面所有导入在既有 PostgreSQL 上都会被约束拒绝，因此**不可跳过、不可后置**。
- C 依赖 B：没有竞品套餐名就无法建立"本店套餐 ↔ 竞品套餐"的映射。
- D 依赖 B + C：对比的唯一输入是**已确认匹配**，而匹配来自 C。

**批次明细**

| 批次 | 目标 | 主要改动 | 本批验收 |
|---|---|---|---|
| **A. 迁移与数据底座** | 让两类新数据集在 PostgreSQL 上写得进去（硬前置） | `Migration(version=4, name="competitor_monitoring")`；`schema.sql` A 段新增 3 张表 + 索引 + RLS，并把 `ops_import_batches.dataset` 的 CHECK 改为显式名 `ck_ops_import_batches_dataset`；`pg_sync.py` 的 `TABLES` 补 3 条 `TableSpec`（`db="ops"`）；`retention.py` 与 `store_config.json` 同步保留期与 5 个 `rules` 键 | `pg_sync migrate --status / --dry-run / --apply / --verify` 全通过；新库建成 3 表；既有库执行 v4 后能写入两类新 `dataset`；新库与迁移后库的约束名一致；`policy_from_config` 不再报未知表 |
| **B. 竞品数据导入** | 两类数据集接入统一收件箱 | `competitor.py` 的采样与竞品门店导入（沿用表头识别 + SHA-256 批次哈希幂等 + 字段校验范式）；商圈完整性校验；九类 `detect_dataset`；`import_inbox` 分投；CLI `import-competitor-store` / `import-competitor-offer`；2 份导入模板 | 平台侧三类与第 1 期四类的导入回归**不破**；新数据集导入 `rows_written > 0`、重复导入 `= 0`；单文件失败不影响其它且退出码为 1；`area_id` 不在本店 `trade_areas` 时明确报错 |
| **C. 匹配闸门与权限** | 把"只有人工确认的匹配才可用"落成机制 | 新权限 `competitor:match`（owner / 区域负责人 / 店长，与 `ops:import` 分开）；`GET` / `POST /api/competitor/matches`；确认与驳回写 `audit_events`；路由契约测试 53 → 55 | employee 与 operator 调 `POST` 返回 403；店长可创建并确认；确认与驳回各写一条审计；`pending` / `rejected` 记录存在但不被下游采用 |
| **D. 四维对比与规则**（**已并入第 11 期**） | 产出可用对比与草稿 | `GET /api/competitor/compare`；`snapshot_builder` 注入 `competitor` 段与 `rule_readiness.competitor`；`prototype.py` 新增 `competitor` 类规则；`store_cli.py plan` 与 `scripts/daily_sync.py` 接入 | 价格带分档与 P25 / P50 / P75 数值正确；环比取上一等长窗口；样本 < 门槛时只报"样本不足"；未确认匹配不进入对比且结果含"待匹配的竞品套餐数"；规则每商圈产出 **1 条**草稿（`requires_approval=True`、`status=proposed`）；缺数不产出任务 |
| **E. 文档与全量收口** | 同步文档并做最终回归 | `README.md`（九类数据集、3 条接口、权限矩阵加 `competitor:match`、路由数 53 → 55、目录说明加 `competitor.py`、版本记录）；`PRODUCTION_READINESS.md`；本节标记为已实施 | 全量回归：Python 用例 + e2e（desktop + mobile）+ `check:js` + `gate-check` 全绿；无密钥默认模式端到端可跑 |

> **进度**：**A 批（迁移与数据底座）已于 2026-10-21 完成**——`schema.sql` 41 张表 / 32 张 RLS，`Migration(version=4)` 落地（18 条语句，含三张表 DDL 与 RLS），`pg_sync.TABLES` 28 条，`retention` 与 `store_config.json` 已同步（13 个 `rules` 键 / 30 张表策略）。
> 验证：**544 个 Python 用例通过**（新增 3 个 v4 用例）+ `check:js` 通过 + `gate-check` `ok: true`；并在真实 PostgreSQL 上复现了升级路径——旧约束拒绝 `competitor_store` → v4 后约束名收敛为 `ck_ops_import_batches_dataset` → 两类新 `dataset` 可写入 → `verify` ok。
> **B 批（竞品数据导入）尚未开始**：SQLite 侧的竞品三张表随 B 批的 `competitor.py` 一并建立，当前 SQLite 库中不存在（`retention-plan` 对未建表报 `skip`，符合预期）。
> **B 批（竞品数据导入）已于 2026-10-21 完成**——新增 `competitor.py`（竞品门店与采样明细导入、商圈完整性校验、合规三字段必填），SQLite 侧竞品三表随该模块建立；九类数据集接入统一收件箱（`store_ops_data.import_inbox` 增加竞品分投），CLI 新增 `import-competitor-store` / `import-competitor-offer`，新增 2 份导入模板。
> 验证：**566 个 Python 用例通过**（新增 22 个，见 `test_competitor.py`）+ `check:js` 通过 + `gate-check` `ok: true`；CLI 实测三份真实模板一次导入成功、重复导入全部 `skipped`、坏文件 `failed=1` 且**退出码 1**。
> 实施中的两点补充：① 合规三字段（`sampled_by` / `source_method` / `source_note`）按调整点 5 的口径**强制必填**，本节数据集表已同步修正（原写作"可选"自相矛盾）；② 统一收件箱增加一次"挂起重试"——竞品门店要校验商圈 `area_id`，而商圈文件未必排在其前面，重试后同一批文件的导入结果与文件名顺序无关。
> **C 批（匹配闸门与权限）已于 2026-10-21 完成**——新增 `competitor:match` 权限（owner / 区域负责人 / 店长，与 `ops:import` 分开）；`GET` / `POST /api/competitor/matches` 两条接口（路由 53 → **55** 条）；确认与驳回写 `audit_events`（含 before/after 状态迁移）；`CompetitorStore` 新增 `confirmed_matches()` 作为对比的唯一输入，`upsert_match()` 拒绝未登记竞品与无采样的竞品套餐。
> 验证：**583 个 Python 用例通过**（新增 17 个）+ `check:js` 通过 + `gate-check` `ok: true`；Live 实测 owner（18 项权限）可确认、operator（11 项权限）被 403 且提示"缺少权限：competitor:match"，确认 → 驳回 → 再确认各留一条审计。
> 实施中的两点补充：① `confirmed_by` / `confirmed_at` 只在 `confirmed` 时写入，pending / rejected 的决策人与时间由审计承载，避免留下半套"已确认"信息；② `GET /api/competitor/matches` 除匹配清单外一并返回**尚未匹配的竞品套餐**（只陈列、不猜对应关系），否则人工挑选时没有候选可看。
> **D 批（四维对比与规则）已并入第 11 期**（见「阶段五」）：竞品对比既是产品能力，也是 Agent 回答竞品问题的数据前提，拆成两期做会出现"工具已接好、数据还没有"的悬空状态。第 11 期从 D 批的位置接上，**不再单独执行 D 批**。

**每批的收口定义（DoD）**：新增用例通过 → **既有用例全绿** → `npm run check:js` 通过 → `python store_cli.py gate-check` 通过 → 无密钥默认模式可端到端验证。

**各批必须覆盖的测试场景**（先把边界定下来，不留到实现时临场决定）：

- **A**：约束名收敛（新库与迁移后库同名）；v4 幂等（重复 `--apply` 不报错）；`--verify` 对缺表与校验和漂移的判定；`retention` 未知表回归。
- **B**：九类表头**双向**互不抢占（新不抢旧、旧不抢新）；`area_id` 越界拒绝；金额与日期校验；文件内重复行拒绝；批次哈希幂等；目录级单文件容错。
- **C**：权限矩阵（三种角色 × 两个接口）；`status` 非法值拒绝；审计字段完整（确认人与确认时间）；跨租户访问拒绝。
- **D**：分位与分档算法（含边界值与并列值）；样本不足分支；环比窗口对齐；截断标注；规则两条触发路径各一例；缺数不产出。
- **E**：文档口径一致性（路由数、权限矩阵、数据集数量）。

**止损点与回退**：

- 任一批结束时既有用例不绿 → **该批不合并**，按本节"风险与回退"处理。
- **A** 批若 `Migration v4` 在既有库上失败 → 回滚到迁移前（`pg_sync migrate` 自带恢复点与账本），**不手工改约束**。
- **D** 批若对比口径与预期不符 → 保留 A-C 批成果（数据与匹配仍可用），仅停用 `compare` 与规则，退回"采样台账 + 人工查看"，数据不丢。

#### 第 3 期 规则外置与版本化 + 归因回灌

> **实施进度（2026-10-22）**：第 1-6 步全部完成——`rule_store.py`（版本 + 状态机 + 单一 approved 约束）、`rule_seed.py`（7 条内置规则导出，`rules-seed --approve` 可生效）、`rule_eval.py` + 逐字段一致性回归、`/api/rules` 四个接口 + `rules:read/write/decide` 权限、`generate_daily_plan(rules=...)` 注入与“未就绪回退内置 + `plan_rule_fallback` 审计”。第 5 步：`analysis.RULE_VERSION`/`DIAGNOSIS_RULE_VERSION` 读表版本 + 代码兜底，`task_feedback`/`task_outcomes` 收口进 `schema.sql` 与 `pg_sync`（`Migration v6`）。第 6 步：任务追溯 `rule_key`/`rule_version`（`Migration v7`）；`rule_outcomes.py` + `rule_outcomes` 表（`Migration v8`）按规则版本聚合人工反馈与结果窗口，产出"阈值由 X 调整为 Y"的提案；`GET /api/rules/{rule_key}/outcomes` 与 `store_cli.py rules-outcomes`；提案**从不回写** `action_rules`，未人工确认时系统行为不变。三张归因表已纳入保留策略。

- **目标**：把 `prototype.py` 的 4 类硬编码规则迁入已建模的 `action_rules` 表，让阈值可审批、可版本化、可回滚；把 D+7/D+28 归因回灌为规则调整建议。
- **能力边界**：规则条件与动作模板外置 + 版本管理 + 审批 + 回滚。**不做自动调参**（提案必须人工确认），不做拖拽式规则编辑器。
- **新增模块与数据表**【已实施】：`rule_store.py`（规则 CRUD、版本、状态）、`rule_eval.py`（快照 → 规则判定）；**转正** `action_rules`（从 `schema.sql` 的"B. 规划中"迁至"A. 已落地"）；新增 `rule_outcomes`（规则效果归因）；**模型真相源收口**——把 `task_feedback`、`task_outcomes` 纳入 `schema.sql` 与 `pg_sync`（见 2.1、3.3 C6）。
- **接口变更**【已实施】：`GET` / `POST /api/rules`、`POST /api/rules/{rule_key}/versions`、`POST /api/rules/{rule_key}/decide`、`GET /api/rules/{rule_key}/outcomes`。
- **与现有模块的接缝**：`snapshot_builder` 继续提供规则输入；审批复用 `governance.py`；归因复用 `outcome_tracker.py` 的 `task_feedback` / `task_outcomes`；`analysis.py` 的 `RULE_VERSION`（`"1.0"`）与 `DIAGNOSIS_RULE_VERSION` 从代码常量改为**读表版本 + 代码兜底**；`prototype.generate_daily_plan()` 保留为"内置默认规则 + 规则快照执行器"，行为不变。
- **验收标准**：把现有 4 条规则原样导入表后，**同一快照产出的任务与改造前逐字段一致**（以现有 `test_prototype.py` 作为回归锚点）；改阈值必须走审批且留审计；版本可切换与回滚，历史任务能追溯所用规则版本；规则加载失败时**自动回退到代码内置默认规则并告警**；归因能产出"某阈值建议由 X 调整为 Y"的提案，未确认时行为完全不变。
- **风险与回退**：外置后出现"改了配置没生效"的静默失败 → 启动时做规则完整性自检并写入 `ops_runs`；版本膨胀 → 同一 `rule_key` 仅允许一个 `approved` 版本，其余转 `retired`；回退方案是关闭表读取，直接使用内置规则。

---

### 阶段二 · 执行侧与底座补齐（第 4-5 期）

#### 第 4 期 巡店独立流程 + 申诉闭环

> **实施进度（2026-10-22）**：已完成。`inspection.py`（固定字段模板 + 版本/单一生效版本 + 巡店任务 + 缺项复核）、`appeal.py`（一任务一次有效申诉、驳回必须写结论）；`inspection_templates`/`inspection_items`/`appeals` 三张表进 `schema.sql` 与 `pg_sync`（`Migration v9`）并纳入保留策略（模板与检查项永久、申诉 730 天）；六条接口 + `inspection:read/manage/submit`、`appeals:submit/decide` 五个权限（路由 62 → 68）；**未结案申诉阻断奖惩审批**；看板任务按 category 区分巡店。

- **目标**：把巡店从隐含动作升为可配置检查表流程；补齐员工申诉实体，让"奖惩只出草稿"这条红线有配套的制度出口。
- **能力边界**：巡店模板（检查项 / 证据要求 / 责任人 / 阈值）+ 巡店任务生成 + 缺项复核；申诉提交、受理、结论并与奖惩草稿联动。**只支持固定字段**，不做自由字段与拖拽编排（编排能力留给第 7 期）。
- **新增模块与数据表**【已实施】：`inspection.py`、`appeal.py`；表 `inspection_templates`、`inspection_items`、`appeals`。
- **接口变更**【已实施】：`GET` / `POST /api/inspection-templates`、`POST /api/inspections`、`GET /api/inspections/{id}`、`POST /api/appeals`、`POST /api/appeals/{id}/decide`。
- **与现有模块的接缝**：复用 `task_store.py` 状态机与指纹去重（巡店任务仍是 task，带 `source=inspection`）；复用 `evidence_store.py` 图片证据与哈希校验；奖惩草稿复用 `governance.py` 审批；通知复用 `notify.py`。
- **验收标准**：配置一个巡店模板后能生成巡店任务、上传证据、自动标记缺项并进入复核；员工可对奖惩草稿提申诉；**申诉未结案时奖惩不得生效**（延续"系统绝不直接给员工定罚款或纪律处分"）；巡店与普通任务在看板可区分；申诉记录纳入现有保留策略（`retention.py` 已为申诉预留 2 年）。
- **风险与回退**：模板过度配置化会抬高维护成本 → 固定字段 + 模板版本号，不做自由字段；申诉入口被滥用 → 申诉必须绑定具体任务与理由，且同一任务仅允许一次有效申诉；回退方案是巡店退回普通 task。

#### 第 5 期 能力注册与作用域底座（原 P0）

> **实施进度（2026-10-22）**：已完成。`capability_scope.py`（作用域值对象 + required ⊆ allowed 校验）、`capabilities.py`（15 个工具注册为 `tool:<name>` 能力，`available_for()` 按身份过滤，受控调用越权即拒并写 `agent_tool_runs` 的 `blocked` 留痕）；`capabilities`/`capability_bindings` 进 `schema.sql` 与 `pg_sync`（`Migration v10`）并纳入保留策略（主数据永久）；`GET /api/capabilities`（路由 68 → 69）。**实施偏差**：计划原文希望 `ToolSpec` 由注册表生成；为让「发给模型的 tools 数组与改造前逐字一致」这条验收可证伪，本期方向定为 ToolSpec 仍是声明真相源、注册表从它派生并提供等价视图（`as_tool_specs()` 与 `TOOL_SPECS` 逐字比对用例锁定），工具参数适配继续由 `llm_agent._invoke` 承担（注册表只负责授权与留痕，不复制第二份调用契约）。

- **目标**：把 `agent_tools.StoreTools` 的进程内 facade 升级为带 id、作用域、授权的能力注册表，作为第 6-9 期的公共依赖。
- **能力边界**：只做注册、描述、授权与调用契约。不做沙箱、不做远程执行、**不改动现有 8 个只读工具的行为与返回结构**。
- **新增模块与数据表**【已实施】：`capabilities.py`、`capability_scope.py`；表 `capabilities`、`capability_bindings`。
- **接口变更**【已实施】：`GET /api/capabilities`（按当前身份过滤返回可用能力清单）。
- **与现有模块的接缝**：`agent_tools.StoreTools` 的 8 个方法注册为 capability，facade 退化为适配层；`llm_agent.ToolSpec` 改由注册表生成；调用留痕继续写 `agent_tool_runs`；权限判定复用 `auth.py` 的权限点，不新造一套。
- **验收标准**：能列出全部已注册能力及其作用域；越权调用被拒绝且有留痕；发给模型的 tools 数组与改造前逐字一致（以现有 `test_agent_tools.py` / `test_llm_agent.py` 作为回归锚点）；新增"required 作用域 ⊆ 允许作用域"的校验用例。
- **风险与回退**：注册表与 facade 并存会形成两套真相源 → 明确**注册表为唯一真相源**；回退方案是保留 facade 直连路径。
---

### 阶段三 · 平台化能力（第 6-9 期）

#### 第 6 期 知识库 RAG（原 P1）

> **实施进度（2026-10-22）**：已完成（向量为后续增强项）。`knowledge.py`：Markdown 章节切分 + 章节内切片、标题/关键词结构感知打分（中文 bigram + 英文词，不引入分词依赖）、三条引用治理规则（重编号 / 截断分档 / 剥离页脚）、引用带 `document_id`/`chunk_id`/`heading_path` 可回查；**无命中明确返回「知识库没有相关内容」**；导入按 sha256 幂等。五张表进 `schema.sql` 与 `pg_sync`（`Migration v11`）并纳入保留策略（知识库/文档/chunk 永久；引用与入库任务 180 天）。三条接口 + `knowledge:read/manage` 权限（路由 69 → 72）；`search_knowledge` 工具（15 → 16）与 `POST /api/chat` 的 `citations`（向后兼容）。**未做**：向量检索（本环境无 `pgvector`；计划本身要求「扩展不可用时退化为结构索引 + 关键词」，当前即该退化路径，向量默认关闭）；SSE 流式对话未回填引用。

- **目标**：让门店手册、平台规则、历史复盘可被检索，并在回答中带**可回链的来源引用**。
- **能力边界**：只做"文档 → 结构化索引 → 带引用检索"。不做自动写回知识库（留给第 9 期），不做多模态 OCR，不做概念层与多级索引。
- **新增模块与数据表**【已实施，向量未做】：`knowledge.py`（解析、切片、索引、检索、引用）；表 `knowledge_bases`、`knowledge_documents`、`knowledge_chunks`、`knowledge_citations`、`knowledge_ingest_jobs`。
- **接口变更**【已实施】：`POST /api/knowledge/search`、`POST /api/knowledge/documents`（导入）、`GET /api/knowledge/bases`；`POST /api/chat` 响应**新增** `citations` 字段（向后兼容）。
- **与现有模块的接缝**：新增 `search_knowledge` 能力，走第 5 期的能力注册表；引用内容复用 `auth.py` 的脱敏函数；导入校验与幂等沿用 `platform_data.py` 的批次哈希范式。`snapshot_builder` 与 `prototype` 不动。
- **验收标准**：导入门店手册后检索命中且引用可回链到原文位置；**无命中时明确回答"知识库没有相关内容"，不得编造出处**（对齐"缺数不编数字"）；引用按首次出现顺序重编号、超限截断、**剥离模型自造的"参考来源"页脚**；超出作用域的知识桶不可检索；`citations` 缺省时不改变既有响应。
- **风险与回退**：中文分词与切分质量不确定 → 先用"标题/章节 + 关键词"打底，向量作为增强项，每步可独立关闭。**检索默认落在现有 PostgreSQL，优先 `pgvector`，扩展不可用时退化为结构索引 + 关键词，不引入外部向量库或 RAGFlow**；回退方案是纯关键词检索。

#### 第 7 期 SOP 状态机 + 技能图（原 P2）

> **实施进度（2026-10-22）**：已完成。`sop.py`：4 类节点（agent/human/approval/condition）、图校验（唯一入口、节点类型、边引用、非终止节点必须有出口、条件分支必须有 default）、版本与单一生效版本。`sop_runtime.py`：8 态状态机（非法转换一律拒绝）、断点快照（`sop_step_runs` + `context_json`）、`submit_human` / `resume`（人工提交与审批恢复同一入口，审批复用既有 `/api/approvals*`）、SOP 级 `max_hours`（默认 24）与步骤级超时自动追加超时出口。四张表进 `schema.sql` 与 `pg_sync`（`Migration v12`）并纳入保留策略（定义/版本永久、运行与步骤 730 天）。五条接口 + `sop:read/manage/run`（路由 72 → 77）。**边界固化**：task 状态变更不直接改写 sop_run，必须经 `submit_human`（有用例锁定）；`prototype` 保留为规则节点的一种执行器。**未做**：拖拽画布与图编辑界面（计划明确不做，只提供图校验与运行预览接口）。

- **目标**：把硬编码的规则流升级为**可编排、可断点续跑、可版本化**的 SOP。
- **能力边界**：4 类节点（数字员工 / 人工 / 人工审批 / 条件分支）、8 态状态机、**节点级能力作用域**、断点续跑、版本管理、最小可用的图编辑与预览。不做拖拽画布，不做多团队协作（第 10 期）。
- **新增模块与数据表**【已实施】：`sop.py`（图定义、校验、版本）、`sop_runtime.py`（执行状态机 + 断点快照）；表 `sops`、`sop_versions`、`sop_runs`、`sop_step_runs`。
- **接口变更**【已实施】：`POST /api/sops`、`POST /api/sops/{id}/versions`、`POST /api/sops/{id}/runs`、`GET /api/sop-runs/{id}`、`POST /api/sop-runs/{id}/resume`；审批节点**复用现有 `/api/approvals*` 与回调接口**，不新建审批通道。
- **与现有模块的接缝**：复用 `task_store.py` 的状态机语义与指纹去重；审批与留痕复用 `governance.py`；`prototype` 保留为"规则节点"的一种执行器，不做重写。边界必须写清——`task` 是"派发给人的工作项"，`sop_run` 是"跨角色流程实例"，任务挂在 `sop_step_run` 上。
- **验收标准**：一条"日巡店"SOP 全链路跑通（人工节点提交 → 审批暂停并落快照 → 免登录链接或回调恢复 → 完成）；非法状态转换被拒且有测试；保存版本产生新版本且旧运行仍能解释所用版本；**SOP 级最长执行时长默认 24 小时、可配置**，步骤级超时自动追加超时出口；断点恢复后上下文不丢。
- **风险与回退**：与 `task_store.py` 的状态机语义重叠，易出现两套状态互相矛盾 → 用上述边界定义隔离，并用测试固化"task 状态变更不直接改写 sop_run 状态"；回退方案是保留 `prototype` 直接生成任务的旧路径，SOP 作为可选编排层。

#### 第 8 期 工具模板/实例 + 沙箱（原 P3）

> **实施进度（2026-10-22）**：已完成（受控入口白名单 + 独立子进程）。`tool_manifest.py`：manifest 校验（入口必须在白名单内、超时 1-3600 秒、网络策略枚举）与 ZIP 导入导出，**导出时 secret 字段一律清空**。`tool_registry.py`：模板/实例/凭据三表，凭据只存 sha256 哈希与掩码（不落明文）；网络策略 deny、跨店调用、超时三类拒绝都写 `agent_tool_runs` 留痕。`tool_sandbox.py`：受控入口白名单 + 子进程执行 + 超时 + 结果 200 KiB 上限，崩溃/超时不影响主进程。三张表进 `schema.sql` 与 `pg_sync`（`Migration v13`）并纳入保留策略（永久）。五条接口 + `tool:read/manage/invoke`（路由 77 → 82）。
> **安全决策（必须留档）**：README 14.4 第 6 条禁止运行时模块出现 shell 调用与动态执行。本期按计划**显式**为 `tool_sandbox.py` 的受控子进程入口申请豁免（行内 `# noqa: dangerous` + 理由注释），**没有放宽全局扫描规则**，也没有给其它模块开口子；沙箱不支持任意用户脚本，只执行白名单内的内置入口，因此不引入任意代码执行能力。
> **未做**：多运行时、流式服务发布（计划明确不做）；文件类资源上限（1 MiB/10 MiB/100 MiB/1000 条目）在当前「受控入口、无文件访问」模型下不适用，仅作为 manifest 声明与结果上限生效。

- **目标**：工具从进程内 facade 升级为**可登记、可部署、可隔离执行**的资产。
- **能力边界**：模板与实例两级、凭据隔离、manifest 导入导出（**导出时清空 secret**）、沙箱执行。**只支持单一受控 Python 运行时**，不做多运行时、不做流式服务发布。
- **新增模块与数据表**【已实施】：`tool_registry.py`、`tool_manifest.py`、`tool_sandbox.py`；表 `tool_templates`、`tool_instances`、`tool_credentials`。
- **接口变更**【已实施】：`GET /api/tools`、`POST /api/tool-instances`、`POST /api/tool-instances/{id}/invoke`、`GET /api/tool-templates/export`、`POST /api/tool-templates/import`。
- **沙箱规格**：独立子进程 + 受限工作目录 + 资源上限（单次读取 1 MiB / 单文件 10 MiB / 工作区 100 MiB / 条目数 1000 / 结果 200 KiB）+ 网络策略 `deny`（默认）/ `allowlist` / `all` + 执行超时 1-3600 秒。
- **与现有模块的接缝**：第 5 期注册表把 `tool_instance` 作为一种能力来源；`llm_agent.ToolCallingAgent` 的 `ToolSpec` 与轮次上限（4 轮 / 8 次工具调用）保留；调用留痕继续写 `agent_tool_runs`；凭据管理复用 `security.py` 的轮换与明文密钥自检。
- **验收标准**：导入一个模板 → 派生两个实例（各自不同凭据）→ 在沙箱内成功执行 → 导出的 ZIP 中 secret 字段为空 → 越权调用、超时、违反网络策略三类均被拒绝且有留痕 → 沙箱进程崩溃不影响主进程存活。
- **风险与回退**：**与现有安全红线直接冲突**——README 14.4 第 6 条明令"运行时模块禁止 shell 调用、任意删除与动态执行"，且 `gate-check` 会扫描。沙箱必须**显式新增独立扫描白名单**（只豁免 `tool_sandbox.py` 的受控子进程入口并写明理由注释），**不得静默绕过或放宽全局扫描**。回退方案是只保留"工具模板元数据 + 进程内执行"，不启用沙箱。

#### 第 9 期 记忆 + 反馈进化（原 P4）

> **实施进度（2026-10-22）**：已完成。`memory.py`：三类记忆（profile/preference/fact）的**规则式抽取**（可解释、无模型依赖）、按内容哈希去重并累加 hits、按租户/门店/用户隔离召回、支持遗忘。`skill_evolution.py`：偏好达到证据阈值（默认 2 次）时蒸馏为 `sop_step` 提案、反思提案、人工确认（approved/rejected，重复确认被拒）。两张表进 `schema.sql` 与 `pg_sync`（`Migration v14`）并纳入保留策略（各 730 天）。四条接口 + `memory:read/write`、`evolution:read/decide`（路由 82 → 86）。**不做规则侧改动**（规则调参已在第 3 期完成，提案里只记录「如涉及规则调整，复用第 3 期的规则版本机制」）；提案只记录，**未人工确认时系统行为完全不变**（有用例对照）。

- **目标**：让"做过的判断"沉淀为可复用记忆，让技能可蒸馏与反思。
- **能力边界**：三类记忆（`profile` / `preference` / `fact`）的抽取与召回；技能蒸馏与反思。**规则调参已在第 3 期完成，本期不做规则侧改动**。
- **新增模块与数据表**【已实施】：`memory.py`（抽取、召回、去重）、`skill_evolution.py`（蒸馏与反思提案）；表 `memory_records`、`evolution_proposals`。
- **接口变更**【已实施】：`GET` / `POST /api/memory`、`GET /api/evolution/proposals`、`POST /api/evolution/proposals/{id}/decide`。
- **与现有模块的接缝**：消费 `agent_sessions` / `agent_messages`；提案如涉及规则改动，复用第 3 期的规则版本机制；审批复用 `governance.py`；记忆按租户 / 门店 / 角色隔离。
- **验收标准**：记忆按租户 / 门店 / 角色隔离且可召回；蒸馏出的技能草案**必须人工确认才生效**；提案被拒时行为完全不变。
- **风险与回退**：记忆污染 → 只允许三类记忆 + 去重 + 来源标记；回退方案是关闭记忆抽取，仅保留会话记录。

---

### 阶段四 · 扩张（第 10 期）

#### 第 10 期 多员工/团队与渠道扩展（原 P5，远期）

> **实施进度（2026-10-22）**：已完成。`digital_employee.py`：数字员工岗位档案 + 能力引用（能力 id 必须在第 5 期能力注册表内，否则拒绝登记）；`team.py`：团队 = 数字员工领导（`leader_employee_id`）+ 人工兜底（`human_fallback`），成员支持数字员工与真人两类，团队任务采用**指派 + 认领**（`offered → claimed → completed`，非法转换一律拒绝）。**明确不做**：竞标与血条赛制（计划要求）。四张表进 `schema.sql` 与 `pg_sync`（`Migration v15`）并纳入保留策略（档案永久、团队任务 730 天）。五条接口 + `workforce:read/manage`（路由 86 → 91）。**入站统一收口复用既有的 `/api/inbound/messages/{provider}`**（飞书/企微适配器已在第一阶段落地），本期不新建第二套通道。

- **目标**：支持多数字员工（岗位、能力档案、工号）、团队协作与任务认领，以及统一的多渠道入站。
- **能力边界**：仅定义能力边界与前置条件。团队领导采"数字员工扮演 + 人工兜底"的混合制；任务机制为"指派 + 认领"，**不引入竞标与血条赛制**；渠道先做飞书与企微的**入站**统一收口。
- **前置条件**：第 5 期作用域底座、第 7 期 SOP 状态机、第 8 期工具隔离**全部完成**；`tenant_id` + RLS + `registry.py` 已具备的多店支撑**不得重复建设**。
- **新增模块与数据表**【已实施】：`digital_employee.py`（岗位与能力档案）、`team.py`（团队与任务认领）；表 `digital_employees`、`teams`、`team_members`、`team_tasks`。
- **接口变更**【已实施，入站复用既有通道】：`GET` / `POST /api/digital-employees`、`GET` / `POST /api/teams`、`POST /api/teams/{id}/tasks`、`POST /api/inbound/{channel}`（统一入站收口）。
- **与现有模块的接缝**：复用 `notify.py` 的出站三通道与 `channel_store.py` 的绑定关系；入站签名校验复用 `security.verify_inbound()`；授权一律走第 5 期作用域。
- **验收标准**：能创建多个数字员工并各自绑定能力作用域；团队成员的任务可指派与认领且全程留痕；飞书/企微入站消息通过签名校验后进入统一会话入口；越权访问按门店与角色被拒。
- **风险与回退**：多员工会显著放大权限与审计面 → 必须以第 5 期作用域为**唯一授权入口**，禁止在业务代码里新增硬编码权限判断；回退方案是保持单员工模式，仅开放多渠道入站。

---

### 阶段五 · 对话与竞品收口（第 11 期）

#### 第 11 期 竞品四维对比 + 对话问数能力（并入原第 2 期 D 批）

> **本章于 2026-10-21 新增。** 原第 2 期 D 批（四维对比与规则）**并入本期**——竞品对比既是产品能力，也是"Agent 能回答竞品问题"的数据前提，拆成两期做会出现"工具已接好、数据还没有"的悬空状态。
> **前置**：第 2 期 A/B/C 批已完成（竞品数据已入库、匹配闸门与权限已就位），第 11 期从 D 批的位置接上。
> **本章已于 2026-10-21 实施完成**（功能 + SSE + 16 个正式用例）。
> 验证：**602 个 Python 用例通过** + `check:js` 通过 + `gate-check` `ok: true`；并用 DeepSeek Key 完成真实联调——库存/时段/竞品/经营归因四类"人话"问法全部用真实数据作答，高风险问句 0 秒被闸门拦下且未调用模型。
> 实施中的两点偏差：① 竞品对比补了 **`by_area` 分商圈口径**（只做全量聚合与"每商圈一条规则"自相矛盾）；② 新增 **`.env` 加载器**并加了**测试隔离守卫**（否则本机 Key 会被测试捡去打真实模型）。

- **目标**：让 Agent **按人话回答问题**——不再依赖关键词命中，能对自由提问（含第 1、2 期新增的门店侧与竞品数据）给出带依据的真实回答。
- **能力边界**：接**大模型**（OpenAI 兼容，Key 由使用者以环境变量提供）走工具调用；**模型只读**，所有写操作照旧只出草稿并需人工审批；无 Key 时降级为**已收紧的规则模式**，不静默失败。竞品对比沿用第 2 期已写定的四维口径，不新开口径。
- **新增模块与数据表**【未实现】：`llm_agent.py` 增流式客户端与工具；`agent_tools.py` 扩展 5 个问数工具；`competitor.py` 增四维对比；表侧**复用**第 2 期已建的 `competitor_*`，不新增表。
- **接口变更**【未实现】：`POST /api/chat/stream`（SSE）、`GET /api/competitor/compare`；`/api/chat` 与 `/api/competitor/matches` 保持向后兼容。**路由 55 → 57**。
- **与现有模块的接缝**：对话仍走 `store_agent.StoreOperationsAgent`（只改路由顺序与判定），工具宿主**扩展 `StoreTools`**（加入 ops / competitor store 引用）而不新建工具源，`ToolCallingAgent` 的调度结构不动；新工具一并写 `agent_tool_runs` 留痕；四维对比直接复用 `CompetitorStore.confirmed_matches()` 作为唯一输入。
- **验收标准**：见本节"测试与验收"。
- **风险与回退**：见本节"风险与回退"。

##### 根因与实证（本章的依据）

在跑着的服务上以店主身份问了 8 句"人话"，三处缺陷全部可复现：

| 提问 | 实际被判为 | 判定 |
|---|---|---|
| 为什么昨天订单少了 | `explain_tasks` | **答非所问**：回的是"任务为什么生成" |
| 同商圈竞品有没有降价 | `approval_required` | **风险词误判**：回"我不会直接执行改价/赔付" |
| 哪个时段生意最差 / 库存还够不够 / 帮我看看经营情况 | `capability_help` | **答不了**：回同一段固定能力清单 |
| 今天哪些物料要补货 | `list_tasks` | 巧合命中"今天"，不是真懂 |
| 为什么曝光掉了 / 今天要做什么 | `store_diagnosis` / `list_tasks` | 正确 |

三处根因：**① 风险词"提及即拦"**（问竞品降价被当成要改价）；**② 宽泛关键词劫持**（`match_intent` 是"第一个命中即返回"，"为什么"抢走了经营归因）；**③ 能力面缺口 + 兜底无能**（第 1、2 期新增的门店侧与竞品数据既无意图也无工具，未命中就回固定清单）。

##### 关键改动（5 处）

**1. 路由改造：模型优先，安全闸门仍在最前**

新顺序：`权限拦截 → 高风险执行诉求拦截 → 有 Key 走工具调用（模型）→ 无 Key 走关键词意图（已收紧）→ 兜底引导`。

要点是把关键词层从"第三道"降为**兜底**——它现在排在模型之前，配了 Key 也轮不到模型说话。**安全闸门不交给模型**（红线），仍是纯规则，且位置不变。

**2. 高风险判定从"提及"改成"执行诉求"**

判定式：命中**风险对象**（价 / 价格 / 赔付 / 赔偿 / 警告 / 处罚 / 罚款 / 奖惩 / 发布 / 上架 / 公开回复）**且**命中**强动作表达**（帮我…/ 把…改成 / 下调 / 上调 / 执行 / 下发 / 发布 / 上架 / 回复掉）**且不含疑问标记**（吗 / 呢 / ? / ？ / 怎么 / 如何 / 为什么 / 能不能 / 是否 / 有没有 / 该不该）——三者同时成立才拦。

效果对照（必须成为用例）：`同商圈竞品有没有降价` → 放行；`帮我把套餐降价 10 元` → 照拦；`差评怎么回复` → 放行；`帮我把这条差评回复掉` → 照拦。模型侧再加一道 system 提示：不得输出可执行的高风险指令。

**3. 补 5 个问数工具**

现有 10 个工具**全是平台侧 + 任务**，第 1、2 期数据对模型完全不可见，这是"问库存/时段/竞品都答不了"的直接原因。

| 工具 | 数据来源 | 需要权限 |
|---|---|---|
| `get_store_traffic`（开台 / 客流 / 翻台率） | `store_ops_data.StoreOpsDataStore` | `metrics:read` |
| `get_time_slots`（时段分布） | 同上 | `metrics:read` |
| `get_inventory_alerts`（库存预警） | 同上 | `metrics:read` |
| `get_competitor_matches`（匹配台账 + 尚未匹配的竞品套餐） | `competitor.CompetitorStore` | `metrics:read` |
| `get_competitor_compare`（四维对比） | 本期新增 | `metrics:read` |

工具宿主**扩展 `StoreTools`**（构造时注入 ops / competitor store）而非新建工具源——`ToolCallingAgent` 的 `ToolSpec` 调度结构保持不变；等**第 5 期能力注册表**落地后再把这批硬编码工具收敛进注册表（本期不抢第 5 期的活）。

**4. SSE 流式输出**（仓库现无任何流式实现，从零建）

- 新增 `POST /api/chat/stream`，事件契约：

| 事件 | 载荷 | 说明 |
|---|---|---|
| `status` | `{"text": "正在查询经营数据…", "step": 1}` | 调工具前先让用户看到进展 |
| `delta` | `{"text": "…增量文本…"}` | 答案增量；前端追加渲染 |
| `done` | `{"intent": …, "sources": […], "mode": "tools" / "rules", "llm_enabled": true / false, "degraded": false}` | 收尾；`mode` 让前端如实标注"这次是模型答的还是规则答的" |
| `error` | `{"text": …, "degraded": true}` | 模型不可用/超时；前端按降级提示渲染 |

- 模型侧**每一轮都用 `stream: true`**：`delta.content` 直接转发为 `delta` 事件，`delta.tool_calls` 按 `index` 累积组装；`finish_reason=tool_calls` 就执行工具进入下一轮，`stop` 则发 `done`。**只有这样才能边调工具边出字**。
- 前端：流式读取器放在 `static/session-client.js`——`test_frontend_contract.py` 明令 4 个页面脚本禁止出现 `fetch(`，而共享客户端不在禁令内且已处理 `credentials`/CSRF/401。`static/agent.js` 只调用客户端的新方法，并补上"正在思考…"的加载态与发送按钮禁用。
- **无 Key 时走同一接口**：一次性发 `delta` + `done`（`mode=rules`），前端只有一条代码路径。

**5. 竞品四维对比（原 D 批，口径不变）**

沿用第 2 期已写定的口径，本章不重复展开：价格（档宽 `competitor_price_band_size` 默认 20 元 + P25/P50/P75 + 样本数）、套餐（套餐数 / 档位覆盖率 / 已确认匹配的价差）、活动（有活动的竞品数与类型分布，只陈列不推断）、商圈变化（价格带中位数环比上一等长窗口）。五条硬约束同样沿用：**只有 `confirmed` 匹配参与**；`competitor_min_samples`（默认 5）以下只报"样本不足"不下结论；无采样/无匹配如实回报；`competitor_max_matches`（默认 50）截断并标注；新增一条 `competitor` 类目规则，**每商圈最多一条**、`requires_approval=True`、只出草稿。

##### 测试与验收【未实现】

- **实证探针转正**：本章"根因与实证"表里的 8 句**原样**作为回归用例，逐条断言 `intent` 与答案要点——"为什么昨天订单少了"必须走经营归因；"哪个时段生意最差 / 库存还够不够 / 帮我看看经营情况"必须给出**真实数据**而不是能力清单。
- **高风险正负例**：不少于 4 组，必须包含 `差评怎么回复`（放行）与 `帮我把这条差评回复掉`（拦截）这一对。
- **工具覆盖**：5 个新工具各有用例；权限过滤（店员拿不到越权工具）；模型编造工具名被调度器拒绝；每次调用写入 `agent_tool_runs`。
- **SSE 契约**：事件顺序为 `status → delta* → done`；`done` 必带 `mode` 与 `llm_enabled`；模型不可用时走 `error` 或降级 `done` 且不崩；无 Key 时同一路径可用。
- **降级三角**：无 Key / Key 失效 / 超时三种情况都必须退到规则模式并**如实说明**，不得静默失败。
- **多轮**：连续追问（"那昨天呢"）能接住上一问的窗口与对象。
- **真实联调**：用使用者提供的 Key 跑一遍上述用例，逐条核对答案与库内数据一致。
- **回归**：现有 583 个 Python 用例 + 34 个 e2e 用例保持全绿；`check:js` 与 `gate-check` 通过。

##### 风险与回退【未实现】

- **模型不可用 / 未配 Key** → 同一接口降级走规则模式，`done.mode=rules`；前端如实标注，不假装是模型回答。
- **超时** → 保留 4 轮 / 8 次工具调用上限，新增**单次问答总时限（默认 90 秒）**；超时按"部分结果 + 明确说明"收尾，不静默截断。
- **成本不可控** → 轮次与工具调用次数上限不变；工具返回做体积裁剪；单次问答的 token 用量写入 `agent_tool_runs` 便于事后核算。
- **模型越权** → 模型只有只读工具，写操作一律走草稿 + 人工审批；高风险判定仍由**纯规则**把守，不因接模型放松；system 提示明确禁止输出可执行的高风险指令。
- **前端契约破坏** → 流式读取器只放 `static/session-client.js`，页面脚本继续保持零 `fetch(`，`test_frontend_contract.py` 的门禁不放宽。
- **回退方案**：`/api/chat`（非流式）与关键词意图层**保留不动**，出问题时前端切回旧接口即可，数据与权限层不受影响。

---

### 4.11 依赖关系与推进建议

```
第 1 期（经营数据）──┐
第 2 期（竞品采样）──┤ 均不依赖底座，可最先做
                     │
第 3 期（规则版本化 + 归因回灌）──┐
                                  │ 经营闭环在此收口
第 4 期（巡店 + 申诉）────────────┘
                     │
第 5 期（能力注册与作用域）← 第 6-9 期的公共前置
   ├── 第 6 期（知识库 RAG）
   ├── 第 7 期（SOP 状态机）
   ├── 第 8 期（工具 + 沙箱）
   └── 第 9 期（记忆 + 进化）
第 10 期（多员工/团队/渠道）← 依赖 第 5 + 7 + 8 期
第 11 期（竞品对比 + 对话问数）← 依赖 第 2 期 A/B/C（已完成）+ 模型 Key
```

推进建议：

1. **第 1-4 期为一个连续单元**，目标是让经营闭环真正闭合（数据补齐 → 规则可改 → 执行侧有制度出口）。这四期不依赖任何平台化底座，可以立即开工。
2. **第 5 期单独一期收口**，因为它是第 6-9 期的公共前置；它与第 1-4 期无依赖，若人力允许可与第 3-4 期并行。
3. **第 6-9 期互相独立**，但都依赖第 5 期。第 7 期与第 8 期有弱耦合（SOP 节点要调用工具实例），建议第 7 期先于第 8 期。
4. **第 10 期最后做**，且必须在前置全部完成后才启动。
5. **第 11 期是当前的实际入口**：第 2 期 A/B/C 批已完成、D 批并入本章，它只依赖已完成的成果与一个模型 Key；第 3-10 期均可后置。第 5 期落地后，本节新增的 5 个硬编码工具应随之收敛进能力注册表。
---

## 5. 接口与契约影响

### 5.1 本次任务的影响

**无。** 本次只产出文档，不改动任何代码、接口、表结构或配置。

### 5.2 文档中提出的接口与数据表

- 本文档提出的**所有**新增模块、数据表、接口**均为待实现规格**【未实现】，不代表已存在。
- 现有 **50 条** `/api/*` 路由的契约在演进中保持不变；新能力一律走新增路由，并由 `test_api_coverage.py` 的路由契约测试继续锁定。
- `store_configs` 与 `schema_migrations` 仍是配置与迁移的唯一真相源。

### 5.3 命名与兼容约定（约束后续实现）

| 约定 | 内容 |
|---|---|
| 路由前缀 | 沿用现有 `/api/` 前缀与按能力分组的方式，**不引入 `/api/v2` 之类的版本前缀** |
| 向后兼容 | 响应只做**加法**：新增字段不得删改现有字段；`dashboard`、`chat` 的新增字段缺省时不得改变既有行为 |
| 迁移 | 新表一律走 `schema.sql` + `pg_sync` 的**版本化迁移**，不做裸 DDL、不在业务模块里自建表 |
| 表段落 | `schema.sql` 的"A. 已落地 / B. 规划中"分段注释必须与实际状态同步；**表转正时注释与 `pg_sync` 迁移必须一并更新，不允许只改一半**（第 3 期 C1/C6 收口即为此约定） |
| 审批通道 | 所有审批复用现有 `governance.py` 与 `/api/approvals*`，**不新建第二套审批通道** |
| 降级 | 所有新增能力必须提供"无外部凭据也能跑通"的降级路径，并保持"缺数不编造" |

---

## 6. 与 BENCHMARK_AND_ROADMAP.md 的分工

### 6.1 职责划分

| 主题 | 归属文档 | 说明 |
|---|---|---|
| 对标事实（StaffDeck / CrewMeld 的架构与机制） | `BENCHMARK_AND_ROADMAP.md` 第 1-4 章 | 本文档不重复，只在需要时引用结论 |
| 本仓库能力基线、护城河、口径勘误 | `BENCHMARK_AND_ROADMAP.md` 第 5 章 | 现状真相源仍以 `README.md` + `PRODUCTION_READINESS.md` 为准 |
| 十二维架构差距矩阵 | `BENCHMARK_AND_ROADMAP.md` 第 6 章 | 本文档第 3 章 D 类与之对应 |
| 对标事实核验记录 | `BENCHMARK_AND_ROADMAP.md` 第 9 章 | — |
| **九大功能域覆盖矩阵与缺口清单** | **本文档第 2-3 章** | 功能视角，旧文档不含 |
| **统一路线图（10 期）** | **本文档第 4 章** | **旧文档第 8 章的 P0-P5 路线图已由本文档接管** |

### 6.2 旧文档的处置

- `BENCHMARK_AND_ROADMAP.md` 定位**降级为"对标依据"**：保留对标事实、架构拆解、差距矩阵与核验记录，不再作为路线图真相源。
- 已在其头部加**指路声明**（3 行），指向本文档，避免两套路线图并存。
- 两份文档的新旧期次对应关系：

| 旧文档（已接管） | 本文档 |
|---|---|
| P0 能力注册与作用域底座 | 第 5 期 |
| P1 知识库 RAG | 第 6 期 |
| P2 SOP 状态机 + 技能图 | 第 7 期 |
| P3 工具模板/实例 + 沙箱 | 第 8 期 |
| P4 记忆 + 反馈进化 | 第 9 期（规则调参部分前移到第 3 期） |
| P5 多员工/团队与渠道扩展 | 第 10 期 |
| （旧文档无） | 第 1-4 期为本次新增：经营数据入口、竞品监控、规则版本化与归因回灌、巡店与申诉 |
---

## 7. 文档自身核验记录

本节记录本文档撰写时的实际核验动作与结果，便于后续判断文档是否已过期。

### 7.1 对标事实抽样回查（5/5 通过）

2026-10-20 通过 GitHub raw 内容抓取，对本文档引用的 5 条对标断言做字符串级回查：

| # | 断言 | 回查目标 | 结果 |
|---|---|---|---|
| 1 | `HarnessLimits` 五项资源上限（1 MiB / 10 MiB / 100 MiB / 1000 / 200 KiB） | StaffDeck `backend/app/harness/contracts.py` | 通过 |
| 2 | 节点级能力作用域含 `required` 子集校验 | StaffDeck `backend/app/skills/skill_schema.py` | 通过 |
| 3 | 引用治理三条硬规则（重编号 / 截断分档 / 剥离页脚） | StaffDeck `backend/app/knowledge/citations.py` | 通过 |
| 4 | 8 态状态机含 `human-waiting` 与 `cancelled` | CrewMeld `README.md` | 通过 |
| 5 | 工具模板/实例两级 + 导出时清空 secret | CrewMeld `README.md` | 通过 |

### 7.2 本仓库结论符号级核验（12/12 通过）

| # | 断言 | 结果 |
|---|---|---|
| 1 | `index.html` 含九大功能域 | 通过 |
| 2 | `platform_data.py` 的 `METRICS` 覆盖 9 项指标 | 通过 |
| 3 | `store_agent.py` 恰有 14 条 `IntentRule` | 通过 |
| 4 | `prototype.py` 含 4 类业务规则（评价邀请 / 差评 / 套餐 / 升级） | 通过 |
| 5 | `prototype.py` 另有 1 条兜底"数据补齐"任务 | 通过 |
| 6 | 5 张规划表（`action_rules`/`offers`/`connector_accounts`/`ingestion_runs`/`metric_facts_daily`）生产模块命中数合计为 **0** | 通过 |
| 7 | `task_feedback`/`task_outcomes` 不在 `schema.sql`，也不在 `pg_sync.py`，由 `outcome_tracker.py` 自建 | 通过 |
| 8 | `reporting.py` 含 5 个派生率 | 通过 |
| 9 | `llm_agent.py` 轮次上限 4、工具调用上限 8 | 通过 |
| 10 | `retention.py` 为申诉预留 2 年保留期 | 通过 |
| 11 | `schema.sql` 含"A. 已落地 / B. 规划中"分段 | 通过 |
| 12 | `dashboard.html` 现有 7 张卡片 | 通过 |

### 7.3 基线复跑

| 命令 | 结果 | 说明 |
|---|---|---|
| `python -m unittest discover -p "test_*.py"` | **Ran 499 tests, OK**（179.9s） | 与 `README.md` / `PRODUCTION_READINESS.md` 声明的 499 一致 |
| `npm.cmd run check:js` | 通过（退出码 0） | 前端与 e2e 脚本语法检查 |
| Playwright 浏览器用例 | 本次未复跑 | 不属于本次只读文档任务范围；沿用文档记录的 30 个 |

### 7.4 口径交叉核对

| 项 | 现有文档说法 | 本次实测 | 判定 |
|---|---|---|---|
| `/api/*` 路由数 | `PRODUCTION_READINESS.md`：45 条；`README.md` 5.1：50 条 | `app_fastapi.py` 实测 **50 条** | **以 50 条为准**；45 条已过期 |
| RLS 表数 | `PRODUCTION_READINESS.md` 批次 8c：17 张 | `schema.sql` 实测 `ENABLE ROW LEVEL SECURITY` **23 处** | **以 23 为准**；17 是当时的快照 |
| 数据模型真相源 | `schema.sql` 自称"权威数据模型" | schema 32 张表，另有 2 张表由 `outcome_tracker.py` 自建 | **存在例外**，已在 2.1 与 3.3 C6 登记，第 3 期收口 |
| Python 用例数 | 499 | 复跑 499 passed | 一致 |

### 7.5 改动边界

本次任务的实际改动边界（`git status --porcelain` 实测）：

```
 M static/agent.css                 ← 本次未触碰（任务开始前即为已修改状态）
 M tests/e2e/responsive.spec.js     ← 本次未触碰（任务开始前即为已修改状态）
?? BENCHMARK_AND_ROADMAP.md         ← 上一轮新增；本次仅在其头部加 3 行指路声明
?? OPTIMIZATION_MASTER_PLAN.md      ← 本次新增（本方案正文）
```

即：**本次只新增本文档一个文件，并在旧文档头部加 3 行指路；未修改任何生产代码、测试或配置。**

### 7.6 已知待同步项（本文档不代为修改）

| # | 待同步项 | 归属 |
|---|---|---|
| 1 | `PRODUCTION_READINESS.md` 的"45 条 `/api/*` 路由"应更新为 **50 条** | 现有文档口径维护 |
| 2 | `PRODUCTION_READINESS.md` 的"17 张表启用 RLS"应更新为 **23 张** | 现有文档口径维护 |
| 3 | `README.md` 10.4 节称"交互式审批卡片属于下一批"，而 `PRODUCTION_READINESS.md` 已记录审批回调 adapter（mock/飞书/企微）完成 | 现有文档口径维护 |
| 4 | `README.md` 第 11 节目录说明标注 `platform_data.py` 为"SQLite 统一指标库"，实际运行时同时支持 PostgreSQL | 现有文档口径维护 |

以上四项均属现有文档的口径维护，本文档遵循"只新增、不改动既有文档正文"的边界约定，仅在此登记。

---

## 8. 附录

### 8.1 证据索引（缺口矩阵的证据落点）

| 判定 | 文件 | 符号 / 位置 |
|---|---|---|
| 九大功能域基准 | `index.html` | 第一节"一个 Agent，九个功能域" |
| 平台指标 9 项 | `platform_data.py` | `METRICS` |
| 套餐指标与评价三态 | `platform_data.py` | `PACKAGE_METRICS`、`REVIEW_STATUSES` |
| 5 个派生率 | `reporting.py` / `analysis.py` | `aggregate()`、`RATE_METRICS` |
| 4 类业务规则 | `prototype.py` | `{date}-review-invite`、`{date}-negative-`、`{date}-package-`、`{date}-escalation-` |
| 兜底数据补齐规则 | `prototype.py` | `{date}-data-repair` |
| 14 条意图规则 | `store_agent.py` | `INTENT_RULES` |
| 8 个只读工具与轮次上限 | `llm_agent.py` | `ToolSpec`、`DEFAULT_MAX_ITERATIONS`、`DEFAULT_MAX_TOOL_CALLS` |
| 权限硬编码 | `agent_tools.py` | `StoreTools._can()`、`PermissionDenied` |
| 结果归因与反馈三态 | `outcome_tracker.py` | `task_feedback`、`task_outcomes`、`DECISIONS`、`OUTCOME_STATUSES` |
| 申诉保留期 | `retention.py` | `action_tasks: 730` 行内注释"任务与证据 2 年（申诉与复盘）" |
| 申诉入口要求 | `prototype.py` | `{date}-escalation-` 任务的 `reason` 文案 |
| 模型分段 | `schema.sql` | 头部注释"A. 已落地 / B. 规划中（未接线，勿在生产启用）" |
| 5 张未接线表 | `schema.sql` | `offers`、`action_rules`、`connector_accounts`、`ingestion_runs`、`metric_facts_daily` |
| 看板卡片 | `dashboard.html` | 7 个 `<h2>` 卡片标题 |
| 规则阈值配置 | `store_config.json` | `rules` 段 6 个键 |
| 安全红线 | `README.md` | 14.4 节 6 条工程约定 |

### 8.2 术语对照

| 本方案用词 | 含义 | 现有对应物 |
|---|---|---|
| 经营闭环 | 数据进来 → 看清 → 诊断 → 派单 → 执行与验收 → 归因 → 改进 | 现有前六步已通，第七步缺落点 |
| 能力底座 | 让系统可扩展、可复制的公共机制 | 见 D 类缺口 |
| 规则外置 | 把硬编码规则迁到可版本化的数据表 | `prototype.py` → `action_rules` |
| 归因回灌 | 把结果窗口的归因结果转化为规则调整建议 | `task_outcomes` → `rule_outcomes` |
| 已建模未接线 | 表已定义但生产代码 0 引用 | `schema.sql` B 段的 5 张表 |
| 制度有表述无实体 | 文档与保留策略提到了，但没有表与接口 | 申诉、巡店 |

### 8.3 对标来源与抓取日期

- 对标信息抓取日期：**2026-10-20**（Asia/Shanghai）。
- 对标仓库与关键路径的完整清单见 `BENCHMARK_AND_ROADMAP.md` 第 1.2 节与第 10.1 节。

---

**文档结束。** 第 4 章全部为待实现规格；本文档不改变任何现有实现。