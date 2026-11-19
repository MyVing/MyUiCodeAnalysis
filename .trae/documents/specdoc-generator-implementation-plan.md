# 规格文档装配器（specdoc）实现计划

## Context

用户希望把「输入功能需求描述 → 输出前端交互视图规格文档（2.x.3 业务操作步骤 + 2.x.4 前端交互视图）」做成 MyUiCodeAnalysis 的对外功能。系统已完成前端组件解析，且已有需求定位链路（LangGraph：decompose→anchor→recall→locate→assemble，产物 `RequirementMap` 含 matches / interactionChains / subgraph），可作为定位产物的下游消费方复用。

生成方式分级：
- **确定性装配**（不调 LLM）：4.1 页面要素清单、4.2 事件清单、4.5 接口映射
- **LLM 推断**（置信度封顶 0.6、quote 逐字校验，失败进 dropped）：2.x.3 操作步骤、4.3 前端业务规则、4.4 字段映射
- **dry-run**：只做确定性部分，LLM 章节留空 + warning

## 模块结构

新增 `backend/app/specdoc/` 独立包（与 requirement/ 平级，复用其 llm/judge/contracts，不复制代码）：

```
backend/app/specdoc/
  __init__.py
  service.py        # SpecDocService：start/_execute，仿 RequirementLocateService
  orchestrator.py   # SpecDocOrchestrator：collect→deterministic→infer_steps→infer_rules→infer_mapping→assemble
  collect.py        # MaterialResolver：RequirementMap+IRGraph → 素材与语料、页面编号
  elements.py       # 确定性 4.1
  events.py         # 确定性 4.2
  apimap.py         # 确定性 4.5
  infer.py          # LLM 章节 2.x.3/4.3/4.4（prompt + quote 校验）
  render.py         # md 渲染
```

契约：`shared/schemas/specdoc.schema.json` → 跑 `shared/scripts/generate_contracts.py` 生成 `backend/app/contracts/specdoc.py`。

## 契约设计（SpecDocMap）

顶层：`schemaVersion / specDocId / projectId / requirementId（来源定位产物）/ analysisId / createdAt / dryRun / requirement（原文回显）`

| 章节 | 条目字段 |
|---|---|
| steps（2.x.3） | `{id:"step.f{featureNo}", featureId, precondition, actions[], evidence}` |
| elements（4.1） | `{id:"ui.{pageNo}.{n}", pageId, kind: data\|action_remote\|action_local\|display, tag, uiText, required?, desc, eventIds?, evidence}` |
| events（4.2） | `{id:"evt.{pageNo}.{n}", pageId, elementId, handler{name,file,loc}, httpCalls[{method,url}], source, confidence}` |
| rules（4.3） | `{id:"fr.{featureId}.{n}", pageId, category: validation\|display\|linkage\|session\|permission, condition, result, evidence, featureId}` |
| fieldMappings（4.4） | `{frontField, backendParam, api, evidence}` |
| apiMappings（4.5） | `{elementId, method, url, source, confidence}` |

- 证据契约复用 `contracts/requirement.py` 的 `VerifiedEvidence`（nodeId/quote/file/loc/via/source/confidence）：确定性条目 source=static、confidence 取 IR 节点 evidence；LLM 条目 source=ai、走 `judge.capped_confidence` 封顶 0.6
- dropped / warnings 复用 `RequirementWarning` 与 `Dropped`（reason 用 quote_not_found / invalid_shape）

## 确定性装配算法

输入：`Match.pageNodeId` + `match.chainId` 对应 `interactionChains`（ActionChain）+ `match.subgraph.nodeIds` 对应 IR 子图。

三分类判定（`elements.py:classify_element`）：
- `viewRole=text` 或 tag∈{span,p,h1-h6,label,link} → **display**
- 是某 ActionChain 的 element 且链含 role=api 段 → **action_remote**（发请求）
- 有事件边（element→handler）但链无 api 段 → **action_local**（本地动作）
- tag∈{input,select,textarea,password,radio} → **data**

事件（`events.py:build_events`）：ActionChain 的 steps 天然是 element→handler→api 三段；handler 取 `ChainStep.file/loc`（真实证据）；api 段 name（如 "GET /api/orders/export"）解析 method+url；`terminatedAt` 非空时只取到断点段并出 warning。

接口映射（`apimap.py`）：全链 api 段按 url 去重，source=static、confidence=0.95（对齐锚点直连分档）。

编号：`ui.{pageNo}.{序号}`、`evt.{pageNo}.{序号}`、`fr.{featureId}.{序号}`；页面序号按 matches 出现顺序分配（`collect.py:assign_page_numbers`）。

## LLM 推断节点（infer.py）

- **语料**（collect.py 产出，硬约束：页面画像不进语料）：每页面 = 被引链路的 `chain.corpus` + handler 源码文本（IR 节点 attrs.src 片段）+ state 节点名列表
- **system prompt**：前端规格分析师角色；只输出 JSON；quote 必须逐字复制自语料；禁止编造 nodeId/文件名/接口；每次调用聚焦单一章节
- **输出形状**：`{steps:[{featureId,precondition,actions,quote}], rules:[{category,condition,result,quote,featureId}], fieldMappings:[{frontField,backendParam,api,quote}]}`
- **校验**：复用 `judge.parse_json`（judge.py#L215-237）→ `capped_confidence`（0.6 封顶）→ quote ∈ 语料逐字校验，失败进 dropped（quote_not_found），category 越界进 invalid_shape
- 三个章节各一次调用；LLM 不可用或 dry_run 时章节置空 + warning（`llm_unavailable` / `dry_run_skipped_llm_sections`）

## API 设计

新增 **SpecDocTask**（不复用 RequirementTask）：同一 RequirementMap 可多次生成规格；字段形状对齐 RequirementTask（`id="stask_{hex}"`、status/dry_run/requirement/source_requirement_id/warning_count、element_count/event_count/rule_count）。

`schemas.py` 新增：`SpecDocStart(requirement_id: str 必填, dry_run: bool=False)`、`SpecDocTask`、`SpecDocTaskList`。

路由（`backend/app/api/v1/specdocs.py`，注册进 v1 router）：
- `POST /projects/{project_id}/specdocs`
- `GET /projects/{project_id}/specdocs`
- `GET /specdocs/{task_id}`（任务状态）
- `GET /specdocs/{task_id}/doc`（SpecDocMap JSON）
- `GET /specdocs/{task_id}/doc.md`（text/markdown，render.py）

service.py 前置校验：`repository.get_map(requirement_id)` 存在，且其 `analysisId` 与最新图一致，否则 `SpecDocPreconditionError("specdoc_map_stale")`；仓储新增 `save_spec_doc / get_spec_doc_by_task`（新表 spec_docs）。

## 实现步骤（依赖序）

1. `shared/schemas/specdoc.schema.json` + 跑 `shared/scripts/generate_contracts.py` → `contracts/specdoc.py`；`schemas.py` 加 SpecDocStart/SpecDocTask/SpecDocTaskList
2. `specdoc/collect.py`：素材解析 + 语料构造 + 页面编号
3. `specdoc/elements.py`、`events.py`、`apimap.py`：确定性装配
4. `specdoc/infer.py`：三章节 prompt + quote 校验（复用 judge.py）
5. `specdoc/orchestrator.py`：图 `collect→deterministic→infer_steps→infer_rules→infer_mapping→assemble`，SCHEMA_VERSION="1.0"
6. `specdoc/render.py`：md 渲染（章节标题含 2.x 编号）
7. `specdoc/service.py` + 仓储扩展（save/get spec_doc、新表 spec_docs）
8. `api/v1/specdocs.py` + router 注册

## 关键复用

- `backend/app/contracts/requirement.py`：RequirementMap / ActionChain / VerifiedEvidence / Dropped
- `backend/app/requirement/orchestrator.py`：LangGraph 编排与 assemble_node 惯例模板
- `backend/app/requirement/service.py`：任务状态机模板（start/_execute/状态流转）
- `backend/app/requirement/judge.py`：capped_confidence / parse_json / 0.6 封顶
- `backend/app/requirement/llm.py`：LLMClient.chat

## 验证

- 单测（mock LLM）：
  - `tests/test_specdoc_elements.py`：三分类判定、编号规则边界
  - `tests/test_specdoc_infer.py`：mock `LLMClient.chat` 返回含非法 quote 的 JSON → 断言 dropped 与 0.6 封顶；dry-run → 断言 LLM 章节为空 + warning
  - `tests/test_specdoc_api.py`：路由与前置校验（map 不存在 / map 过期）
- 手动端到端：POST /projects/{id}/requirements → 轮询 → GET /requirements/{taskId}/map 取 requirementId → POST /projects/{id}/specdocs → 轮询 → GET /specdocs/{taskId}/doc（JSON）与 /doc.md（markdown）
