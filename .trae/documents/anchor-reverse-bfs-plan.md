# 需求定位改造：接口锚点 + 反向 BFS 归属

## Context

现有召回是「路由锚定候选 + 页面级词法评分」（[recall.py](file:///D:/pythonProjects/UiCodeAnaylysis/MyUiCodeAnalysis/backend/app/requirement/recall.py) 的 `collect_page_evidences`/`_score_page`），命中依赖页面自身命名/文案含关键词，深层组件的间接信号会被稀释。用户确认改为**截图 6.11 方案**：接口（method+path）是比自然语言更可靠的定位锚点，从锚点沿图边**反向 BFS 归属到 route**，词法只负责「发现种子」不负责「归属」。契约事实已核对：`NodeKind.api`、`EdgeKind.requests/calls/contains/renders`、`RuntimeTrace.networkEvents`（contracts/runtime.py L432）均已存在。

**范围**：只改召回与定位编排输入；judge 判定/quote 校验、specdoc、前端全部不动。之前暂停的「聊天助手接入项目」方案依赖本改造，待本方案落地后再继续。

## 改动

### 1. 新模块 backend/app/requirement/anchor.py

- `normalize_api_path(url: str) -> str`：去 origin/query/hash、折叠重复斜杠、去尾斜杠；`{id}`/`${id}`/`:id`/纯数字段/UUID 统一为 `:param`；比较大小写不敏感、保留原文
- `AnchorHit` dataclass：`nodeIds: list[str]`、`edgeIds: list[str]`（完整命中路径）、`anchorKind`、`confidence`
- `AnchorIndex.build(graph)`：
  - api 索引：`key = METHOD + " " + normalize(path)`，遍历 `NodeKind.api` 节点（name 即 "METHOD url"，attrs 有 method/url）
  - 反向邻接：requests（caller→api，深度1）、calls（callee→caller，深度3）、contains（child→parent，深度2）、renders（child→parent，深度3；route→page，深度1）
  - 页面集合复用 [recall.py:176](file:///D:/pythonProjects/UiCodeAnaylysis/MyUiCodeAnalysis/backend/app/requirement/recall.py) 的 `page_nodes(graph)`
  - 缓存：模块级 `{analysisId: AnchorIndex}`，图版本变化失效重建
- `locate_anchors(graph, terms, requirement) -> list[AnchorHit]`：
  - 种子锚点系数：api 归一化命中 1.0、routePath 1.0、routeTitle 0.9、uiText/viewText 0.7、组件名 0.6、handler 名 0.5
  - 反向 BFS 到 route 为止，深度上限按上表；置信度 = 路径上各边 evidence 置信度最小值 × 锚点系数
  - 带 route 的种子直接产出命中；到不了 route 的命中点进降级列表（复用现有 `no_route_nodes` 告警惯例）

### 2. recall.py 重构（对外签名不变）

- `recall(graph, terms, top_k)` 签名与 `RecallResult`（candidates/warnings/route_by_page）保持不变——RequirementOrchestrator 与 judge 零改动
- 内部：`collect_page_evidences`/`_score_page` 替换为 `locate_anchors` + 按 route 聚合：页面得分 = 该页命中置信度最大值 + 其余命中衰减累加（cap 1.0）；排序截断沿用
- `CandidateDetail` 增加 `anchor_hits`（nodeIds/edgeIds/kind/confidence），orchestrator 构建 judge payload 时带上精确命中路径——judge 从「选页面」变「确认链路」
- **降级兜底**：无任何种子命中时回退现有词法评分路径（保证不比现状差），加 `fallback_lexical` warning
- 契约：`contracts/requirement.py` 的 `Candidate`/`Signal` 增加可选字段 `anchorNodeIds`/`anchorEdgeIds`（其余字段不动）→ 更新 `shared/schemas/requirement.schema.json` → 跑 `shared/scripts/generate_contracts.py` 重新生成（注意跨文件 $ref 内联生成的既有惯例）

### 3. 运行时强化

- 取项目最新 succeeded `RuntimeTrace`（RuntimeRepository，核对 [repositories.py](file:///D:/pythonProjects/UiCodeAnaylysis/MyUiCodeAnalysis/backend/app/storage/repositories.py) 方法名）：`networkEvents` 的 method+url 归一化后匹配 api 索引 + `trace.page.url` 反向归属页面 → **api→页面直连命中，置信度 0.95**（高于纯静态路径），并从 page.url 推断 baseURL 前缀补上静态分析拿不到的绝对地址
- trace 缺失/无 networkEvents 时静默跳过（运行时数据是可选增强）

### 4. judge/orchestrator（最小改动）

- judge 判定逻辑与 `match_quote` 逐字校验不动；仅候选 payload 增加锚点路径字段
- `Match.pageNodeId` 必须落在候选页面集合内的校验不变（[requirement.py:289](file:///D:/pythonProjects/UiCodeAnaylysis/MyUiCodeAnalysis/backend/app/contracts/requirement.py)）

## 测试

- 新建 `backend/tests/test_anchor.py`：归一化用例（UUID/纯数字/`{id}`/`${id}`/大小写/尾斜杠/query 折叠）；反向 BFS 深度上限（requests1/calls3/contains2/renders3）；置信度沿路径取最小 × 锚点系数；api 索引缓存失效；无 route 命中降级告警
- 适配 `backend/tests/test_requirement_recall.py`：夹具图补 api 节点 + requests 边；断言 api 命中页面得分 > 纯文案命中；fallback 词法路径仍有结果；`candidates_truncated` 等现有告警语义不变
- 回归 `test_requirement_locate*.py`/orchestrator 相关测试（judge payload 新字段的容错）

## 验证

1. `backend`: `.venv\Scripts\python.exe -m pytest tests -q`（全量回归，基线 333 passed）+ `.venv\Scripts\ruff.exe check .`
2. 契约再生成后确认 `backend/app/contracts/requirement.py` 与 `shared/contracts/src/requirement.ts` 同步
3. 手动：对测试项目分别提交「列表页支持 GET /api/xxx 筛选」型需求（应直击目标页面）与纯文案型需求（fallback 路径），对比 dry-run 候选排序与命中路径
