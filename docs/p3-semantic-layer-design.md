# P3 语义层技术设计：需求 → 前端功能链路

> 设计依据：仓库外层《React 前端代码智能分析平台：技术选型与整体架构设计》。
> 与 [engineering.md](./engineering.md) 呼应：契约是唯一真源、告警而非静默降级、不编造节点与边——本阶段的所有设计延续这三条立场。

## 一、目标与范围

平台目标一句话：

> 输入一份需求文档、一段功能描述（后续再加两个 Git Commit 之间的代码变更），平台结合前端代码仓库，自动定位涉及的页面、UI 组件、组件属性、事件处理函数、状态管理、接口调用及页面跳转关系，生成可视化的前端功能链路结果。

本阶段（P3）范围：

- **输入模式 A（需求驱动）做通**：需求文档 / 自然语言功能描述 → 功能点 → 页面映射 → 一跳子图 → 影响面解释与回归建议 → 前端可视化。
- **Diff 模式只做设计预留**（见第八节），实现放在 P4。

### 与市面方案的差异化定位

| 类别 | 代表 | 边界 |
| --- | --- | --- |
| 纯静态图谱 | reactgraph / react-analyzer / dependency-cruiser | 只到结构层，无需求语义映射 |
| 文档/类型级工具 | react-docgen / Storybook | 单组件 Props 文档，无链路视角 |
| 运行时观测 | React DevTools / Meticulous | 依赖真实操作覆盖，未被点击的链路不可见 |
| AI 代码理解 | Cursor 仓库问答 / Sourcegraph Cody / DeepWiki | 对话式输出：无结构化图产物、无置信度证据链、不可复现 |
| 通用图谱 + AI | CodeQL / CodeGPT 类 RAG | 不理解 React 特有语义（JSX / hooks / 状态库） |

没有任何一个产品同时做到：**需求 → 结构化组件级功能链路图 + 每条结论带置信度与逐字引文证据 + 运行时证据交叉验证**。这是本平台的立足点；其代价是 AI 输出必须过服务端校验，本设计里所有约束都为这条服务。

## 二、现状盘点

> **状态说明（2026-11）**：本文是 P3 阶段**立项时的设计**，其中多数条目已经落地，且落地形态与本文有差异。阅读时请以 [technical-design.md](./technical-design.md) 与代码为准。主要差异：
>
> | 本文写的 | 实际落地 |
> | --- | --- |
> | `decompose → recall → judge → verify → assemble` 五节点 | **四节点**：校验内联在 `judge._validate_match`，无独立 `verify` 节点（`orchestrator.py`） |
> | 新增 `verify.py` / `explain.py` / `pipeline.py` | 均未新建；前两者分别内联进 `judge.py` 与 `subgraph.py`，后者即 `orchestrator.py` |
> | 影响面解释 / 回归建议 / `explanations` 契约 | **未实现**（唯一未落地的 P3 条目） |
> | 前端需求定位面板 | 已实现（`RequirementPanel.tsx`） |
> | 路由 / 页面节点 | 已实现（`routes.ts`） |
> | 编排可恢复性 | LangGraph + AIOMySQLSaver 已接，但定位是**审计而非续跑**（见 technical-design 6.9） |
>
> 链路层（`ActionChain`）与两路召回融合是 P3 之后的设计，见 [chain-semantic-design.md](./chain-semantic-design.md)。

**已完成（可直接复用）：**

- 契约 [`requirement.schema.json`](../shared/schemas/requirement.schema.json) 全量定义：`Feature` / `Candidate` / `Signal`（六种信号：`routePath` / `componentName` / `uiText` / `apiUrl` / `propName` / `propDescription`）/ `Match` / `Unmatched` / `Dropped`（四种拒绝原因）/ `RequirementWarning`；`Match.confidence` 硬上限 0.6，`quote` 必须命中候选语料。
  > 注意：当前实现是**列表成员判断**（`quote in detail.quote_corpus`），即「等于某一条完整语料」，不是子串包含。两种口径的差异与修法见 technical-design 6.5。
- 确定性召回 [`recall.py`](../backend/app/requirement/recall.py)：信号加权打分、按功能点分别召回再合并、`top_k` 截断。
- 功能点拆解 [`features.py`](../backend/app/requirement/features.py) + [`lexicon.json`](../backend/app/requirement/lexicon.json)：词典驱动的中文需求分词与检索词扩展。
- 任务编排 [`service.py`](../backend/app/requirement/service.py)：queued→running→succeeded/failed，前置校验同步拒绝（无图 / 示例图 / LLM 未配置时拒非 dry-run）。
- 持久化：`requirement_tasks` / `requirement_maps` 双表（memory + MySQL 双实现）。
- LLM 配置位：`Settings.llm_enabled / llm_base_url / llm_api_key / llm_model / llm_timeout_seconds / llm_max_candidates(30) / llm_max_calls_per_request(4)`。
- IR 侧的事实基础：组件、props、hooks/state、API 链路（封装器穿透）、事件、可见文案（`attrs.uiText`）、运行时证据（merged 图）。

**立项时的待建清单（现状见本节开头的状态说明）：**

| 缺口 | 立项时现状 | 本阶段目标 |
| --- | --- | --- |
| LLM 判定路径 | `service._locate` 明确标注"阶段 3 接入"，非 dry-run 直接拒绝 | 接通 dry_run=false 全链路 |
| 路由 / 页面节点 | `NodeKind.route` 已预留，无分析器产出；召回层已定义 `no_route_nodes` 告警 | ast-worker 新增路由分析器，打通 `routePath` 信号 |
| 编排可恢复性 | `asyncio.create_task` 直跑，进程重启任务即失败 | LangGraph StateGraph + MySQL checkpointer，节点级断点续跑 |
| 影响面解释 / 回归建议 | 完全未开始 | 基于 matches + 子图生成解释与回归测试点 |
| 前端需求定位面板 | 未开始（`requirements.py` API 已有但前端未消费） | 工作台新增需求定位入口，联动图谱聚焦 |

## 三、设计原则

1. **AI 只做「映射与解释」，不做「定位」。** 定位由确定性召回完成；LLM 的输入是功能点 + 候选集（≤30 个，每个带命中信号），输出只允许指向候选集内的节点。模型没有机会幻想出一个不存在的文件。
2. **模型输出必须过服务端校验才进 `matches`。** 四道闸（schema 已定义，本阶段实现）：
   - `nodeId` 不在候选集内 → `dropped/node_not_in_candidates`；
   - `quote` 在该节点源码里逐字找不到 → `dropped/quote_not_found`；
   - `featureId` 不是本次拆解出的功能点 → `dropped/unknown_feature`；
   - 字段形状不符 → `dropped/invalid_shape`。
   `loc` 一律取自 IR 节点，模型给的坐标不可信。
3. **推断层与事实层物理隔离。** IRGraph 是语法事实层（`source=static`，置信度 0.8~1.0：语法事实 1.0、符号推断 0.8~0.9），RequirementMap 是 AI 推断层（confidence 封顶 0.6）。两者的区别不是「1.0 vs 0.6」而是「有没有源码依据」：IR 的每一条结论都能回跳到 `file:line`，AI 的判定不能。被拒输出进 `dropped` 必须展示——审核模型是否在幻想，只能靠这份记录。
4. **dry-run 是一等降级路径。** 未配模型时 dry-run 依然完整可用（现状已保证）；配了模型后 dry-run 仍不调模型，作为调试召回质量的标准手段。

## 四、总体架构

```
需求文本
  │
  ▼
┌──────────────────────────────────────────────────────────────┐
│ LangGraph StateGraph（checkpoint 落 MySQL）                    │
│                                                              │
│  decompose ──► recall ──► judge ──► assemble                 │
│  （已有）    （已有）   （新）    （新）                      │
│   词典拆解   信号召回    LLM 判定  四道闸校验内联在 judge     │
│                         分批调用   + 子图提取                 │
└──────────────────────────────────────────────────────────────┘
  │
  ▼
RequirementMap（requirement.schema.json）
  │
  ▼
requirement_maps 表 ──► GET /requirements/{id}/map
  │
  ▼
前端需求定位面板 ──► 「聚焦子图」联动 React Flow 图谱
```

> 上图是**实际落地**的四节点形态。本文原本设计的 `verify` 节点已合并进 `judge`：四道闸全是纯函数、无 I/O、无模型，独立成节点只会让 checkpoint 多存一份无信息量的中间态。

各节点职责：

| 节点 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| decompose | 需求文本 | 功能点列表（含检索词） | 已有，`features.decompose` |
| recall | 功能点 + IR 图 | 候选集（带信号与得分） | 已有，`recall.recall` |
| judge | 功能点 + 候选集 | 模型判定，`_validate_match` 内联四道闸，产出 matches / dropped / unmatched | 分批调用，预算 `llm_max_calls_per_request`；字段名是 `confidence` 不是 `modelConfidence` |
| assemble | matches + IR 图 | 子图提取 + warnings 汇总 → `RequirementMap` | 子图遍历是确定性算法（`subgraph.build_subgraph`）；解释与回归建议**未实现** |

## 五、模块设计

### 5.1 路由分析器（ast-worker 新增 `src/routes.ts`）

需求定位的输出是「页面」维度，没有 route 节点时召回只剩 `uiText`/组件名信号，`routePath` 信号失效。这是本阶段唯一动 ast-worker 的部分。

- **只认受控 API 名单 + path 字面量**：`react-router-dom` / `react-router` 的 `createBrowserRouter([{ path, element }])`、`<Route path element>`、`createHashRouter`。Element 引用按现有 `ownership`/`resolver` 规则解析到项目内组件，产 `route` 节点 + `route ──renders──► page 组件` 边。
- **动态 path 记告警**。`:id` 参数段是正常形态（保留原文）；拼接变量、常量引用导致 path 静态不可得时，记 `route_path_dynamic`（warning），不编造路径。
- **沿用 IR 硬约束**：route 节点 id 同样是「文件路径 + 路由名」（`src/router.tsx#/users`），`loc` 指向 path 字面量所在行。
- **`mapping.json` 之外的零业务名分支**：不猜约定式路由（`pages/UserList.tsx` 自动成路由是 Next.js 语义）。但 **Next 项目必须支持文件约定**——`app/**/page.tsx` 与 `pages/**/*.tsx` 是 Next 的语言级约定，和 `createBrowserRouter` 同性质，不属于业务名硬编码。实现上 `routes.ts` 已在 `meta.framework === "next"` 时启用该分支（见 `analyzeRoutes`），本节原先「本项目不做」的表述已作废。

### 5.2 LLM 判定路径（backend 新增 `app/requirement/judge.py`）

> **落地差异**：实际实现没有 `LLMProvider` Protocol，而是 `LLMClient` 协议（`llm.py`，httpx 直连 OpenAI 兼容端点，单轮补全）；没有按批拆分，而是「一次全量 prompt + 重试预算」。模型输出的置信度字段名是 `confidence`（不是 `modelConfidence`），落库时该值进 `modelConfidence`。

- **Provider 抽象**：`LLMProvider` Protocol（`complete(messages, schema) -> dict`），OpenAI 兼容实现走 `llm_base_url` + `llm_api_key` + `response_format`（JSON Schema 约束）；测试用 `FakeProvider`。判定 prompt 的系统部分固定：只允许从候选集中选、必须给出逐字引文、候选不足时如实输出「未命中」。
- **输入裁剪**：每个候选只给 `nodeId / name / file / signals / uiText 样本（截断）`，不给全图。上下文总量按候选数线性可控。
- **分批与预算**：候选超过单批容量时按得分排序分批；`llm_max_calls_per_request=4` 是单次需求任务的总调用预算，超出部分记 `llm_budget_exhausted` 告警，剩余功能点进 `unmatched` 而不是静默。
- **判定结果结构**（模型输出契约，进 judge 模块内部 schema，`gen:contracts` 只对 requirement.schema 生效的扩展见 5.4）：

```json
{ "featureId": "f1", "pageNodeId": "src/pages/UserList.tsx#UserList",
  "quote": "搜索用户", "reason": "…", "confidence": 0.85 }
```

### 5.3 校验与落库（最终内联在 `judge._validate_match`，未新建 `verify.py`）

纯函数，无 I/O：

1. nodeId ∈ 候选集？（否则 dropped）
2. quote ∈ 该候选的 `quote_corpus`（召回阶段拼好的原文语料并集）？——**不是**从源码读取 loc 邻域再比对，实现更严格也更快；
3. featureId ∈ 本次 features？
4. `confidence = min(模型值, 0.6)` 落库，原始值存 `modelConfidence`。
5. `verifiedEvidence` **未填充**（契约已定义，代码未产出，见 technical-design 第十节 #9）。

### 5.4 子图遍历与解释（子图已实现于 `subgraph.py`，解释未实现）

- **子图遍历是确定性算法**：以 `match.pageNodeId` 为种子，沿 IR 边取**一跳**（页面 → 子组件 / hook / state / element / 调用链第一环），按 `SubgraphCounts` 八类计数返回。前端聚焦时直接喂布局，不重新推导。**取一跳而不是两跳**：两跳会把共享基础组件与全图级工具函数带进来，子图失去「证据范围」的含义；代价是 `component → fn → api` 链上的接口节点不在子图内，需要接口清单时按页面 id 直接在全图上检索 `requests` 边。
- **解释与回归建议是 LLM 调用（未实现）**：输入 = 每条 match 的子图摘要（节点清单 + 关键 attr，不含源码全文），输出 = 每 match 一段影响面解释 + 回归测试点清单（如「搜索无结果时的空态」「status 参数变更后表格刷新」）。同样过 judge 的 provider 与预算。
- **契约演进**：`requirement.schema.json` 新增 `explanations: [{ featureId, matchIndex, summary, regressionSuggestions: string[] }]`；走 `npm run gen:contracts` 重新生成两侧类型（参数已在 `generate_contracts.py` 固化，只改 schema 不改参数）。

### 5.5 LangGraph 编排（最终在 `app/requirement/orchestrator.py`，未新建 `pipeline.py`）

> **落地差异**：实际是**四节点**（decompose → recall → judge → assemble），线程 id 用 `task.id` 而非 `requirement_id`；定位是**审计而非续跑**，因此没有 `resume_from` 参数，`requirement_tasks` 也没加 `resumed_from` 列。理由见 technical-design 6.9。

- **StateGraph 五节点**（decompose → recall → judge → verify → assemble），state 携带 RequirementMap 的中间形态。
- **Checkpointer 用 AIOMySQLSaver**，连同一 MySQL 3307 实例（≥ 8.0.19 已满足），thread_id = `requirement_id`。节点失败（LLM 超时等）重试或人工续跑时从最近 checkpoint 恢复，已完成的召回不重算。
- **与 `RequirementLocateService` 的关系**：`_locate` 内部改为驱动 pipeline；dry-run 路径维持现状（纯召回、不进图、零模型依赖），保证无 LLM 环境的可测性与可跑性。
- **boot recovery 语义不变**：进程重启把 queued/running 任务置 `failed`（沿用既有统一 UPDATE）。
- **任务表扩展**：`requirement_tasks` 增加 `resumed_from` 可空列（幂等 `ALTER` 由启动建表逻辑处理——与「无 alembic」的既有约定一致，只加列不改列）。

## 六、API 与前端

### API（改动极小）

- `POST /requirements`：`dry_run=false` 路径接通即生效，接口形状不变。
- `GET /requirements/{id}/map`：返回定位产物（含 subgraph）。
- `POST /requirements/{requirement_id}/resume`：**未实现**（checkpoint 定位是审计而非续跑，见 5.5）。

### 前端（WorkbenchPage 新增「需求定位」面板）

- 输入区：需求文本 + 「仅召回（dry-run）」开关 + 提交；任务列表复用 analyses 面板的轮询模式。
- 结果区四段式：**功能点列表** → **候选（信号可解释：为什么这个页面被选中）** → **匹配结果（confidence + 逐字引文 + 证据）** → **未命中 / 被拒（unmatched / dropped / warnings 必须展示，不折叠进角落）**。`verifiedEvidence` 尚未填充，证据目前只有 quote 与 IR 节点坐标。
- 「聚焦子图」按钮：把 match.subgraph 的 nodeIds 喂给 GraphCanvas，高亮并局部布局。**链路落地的下一步是改为喂 `ActionChain.path`**（见 [chain-semantic-design.md](./chain-semantic-design.md) 第六节）——子图是「证据范围」，链路才是「功能链路」。

## 七、里程碑与验收

> 下表是立项时的计划。实际交付状态：S1 ✅、S2 ✅（`verify.py` 合并进 `judge.py`）、S3 ✅（`pipeline.py` 即 `orchestrator.py`，但**无续跑**）、S4 部分（`explain.py` 未实现，面板已上线）。

| 里程碑 | 交付 | 验收标准 |
| --- | --- | --- |
| S1 路由分析器 | ast-worker `routes.ts` + fixture（react-router demo） | ast-worker 测试绿；route 节点 + renders 边 + `route_path_dynamic` 告警；真实 fixture E2E 过 |
| S2 判定与校验 | `judge.py`（含内联校验）+ FakeProvider | 单测覆盖四道闸（候选外 / quote 不存在 / unknown_feature / 封顶生效）+ 预算耗尽降级；`APP_LLM_INTEGRATION` 门控的真实 provider 集成测试 |
| S3 LangGraph 编排 | `orchestrator.py` + AIOMySQLSaver | checkpointer roundtrip 集成测试；dry-run 路径回归不变 |
| S4 解释与前端 | 需求定位面板 | 前端 vitest 绿；根 typecheck 链无错误；手工 E2E：需求文本 → 面板四段式 → 聚焦子图 |

测试基建沿用既有约定：FakeProvider 进单元测试；真实 LLM / MySQL / 全链路分别用 `APP_LLM_INTEGRATION` / `APP_MYSQL_INTEGRATION` 门控。

## 八、P4 预告：Diff 影响分析（只做设计预留，不实现）

- 输入：base commit + head commit。编排层新增 `DiffService`：对两棵 IR 图（同文件+组件名键）做集合差，得到「新增 / 删除 / 修改 / 移动」四类变更节点。
- 影响传播是确定性图算法：变更节点沿 renders / hooks / api 边反向遍历到 route 节点，得到受影响页面集合。
- LLM 只负责把 diff 意图（对 changed 文件的 hunk 摘要）与影响面翻译成人话 + 回归建议，输出契约复用 `requirement.schema` 的演进版（`source` enum 增加 `"diff"`）。
- 需求 + diff 联合分析（原设计文档的模式 D）在此之后叠加：同一功能点集合分别对变更前后两棵图定位，差异即「需求落在变更上的证据」。

## 九、风险与对策

| 风险 | 对策 |
| --- | --- |
| LLM 幻觉（虚构节点、编造引文） | 候选集硬约束 + quote 逐字校验 + dropped 全量审计（schema 已定义，5.3 实现） |
| 中文需求 ↔ 中文文案召回质量 | lexicon 已覆盖；S1 路由分析器补 `routePath` 信号；`no_route_nodes` 告警让用户知道覆盖缺口 |
| LLM 不可用 / 超时 | dry-run 一等降级路径永远可用；judge 失败进 checkpoint 可续跑；`llm_unavailable` 告警可见 |
| 成本失控 | 候选 top_k=30 截断 + `llm_max_calls_per_request` 总预算 + 超预算显式告警 |
| 合并图 / 分析图过期 | RequirementMap 已带 `analysisId`，与最新图不一致时前端提示重新定位（schema 注释已有此约定，前端落地） |
