# AI 需求助手接入项目：对话式规格文档生成

## Context

现有 AI 需求助手（`requirement-chat`）只能澄清需求、产出草稿文本，**不关联项目代码**。用户在对话里问「点赞收藏文章的前端交互视图和逻辑」时，应基于已解析的项目 IR 图定位相关页面，并生成与《2.2 转账》模板同构的 markdown（业务操作步骤 + 前端交互视图）。基础设施已全部就绪：锚点反向召回（recall.py 新流水线）、specdoc 装配器（collect/elements/events/apimap/infer/render）、SpecDocOrchestrator——缺的只是对话入口到这条流水线的接线。

## 流程设计

```
用户消息 → 意图判定(LLM 1次) ─非文档意图→ 现有澄清流程（不变）
              │ doc_query + featureText
              ▼
   decompose(featureText) → recall(锚点反向召回) → top1 候选
              │                                  ├─ 无图 → 引导先做代码分析
              │                                  └─ 无候选 → 追问澄清
              ▼
   合成 RequirementMap（match.pageNodeId=top1）+ 合成 SpecDocTask
              ▼
   SpecDocOrchestrator.run（确定性章节 + 3 个推断章节，max_calls 沿用配置）
              ▼
   render_markdown(doc) → RequirementChatResult.docMarkdown
```

## 改动

### 1. 新模块 backend/app/requirement/chat_doc.py

- `ChatDocOutcome(reply, doc_markdown, page_name | None)`
- `IntentDecision`：LLM 一次调用，system prompt 输出 JSON `{intent: "doc_query"|"clarify", featureText}`；解析失败兜底 clarify
- `locate_feature(graph, feature_text) -> CandidateDetail | None`：`decompose` → `recall(graph, terms, top_k=3)` → 取 top1（score>0）；无候选返回 None
- `build_doc(project, task_id, feature_text, graph, llm, settings) -> str`：
  - 合成 `RequirementMap`（requirement=featureText，matches=[单条 match：pageNodeId=top1.nodeId，quote=候选最优锚点文本，evidence=candidate 信号证据，confidence=score 封顶 0.6]——字段形状对齐 contracts/requirement.py 的 Match，构造即通过）
  - 合成 `SpecDocTask`（id=`chat.{uuid}`，dryRun=False，requirementId=合成 id）
  - 复用 `SpecDocOrchestrator.run` → `render_markdown`
  - **不落库**（对话产物是即时的；正式产物仍走 requirement→specdoc 任务链）
- 导语 reply 由后端模板拼（省一次 LLM 调用）：「已定位到页面《X》（锚点归属：…），以下是该页面的业务操作步骤与前端交互视图。」

### 2. chat.py / schemas / api

- `RequirementChatService.chat` 增加可选 `graph: IRGraph | None` 与 specdoc 依赖；graph 非空时先走意图判定分流
- `RequirementChatResult` 增加 `docMarkdown: str | None = None`、`docPage: str | None = None` → 更新 shared/schemas/requirement.schema.json → 重新生成契约
- [requirements.py](file:///D:/pythonProjects/UiCodeAnaylysis/MyUiCodeAnalysis/backend/app/api/v1/requirements.py) 的 requirement-chat 端点注入 AnalysisRepository（`get_latest_graph(project)` 类方法，核对名）与 settings；图不存在时 graph=None（现有澄清行为 + 提示「请先完成代码分析」）

### 3. 前端

- [ChatPage.tsx](file:///D:/pythonProjects/UiCodeAnaylysis/MyUiCodeAnalysis/frontend/src/pages/ChatPage.tsx)：消息增加 docMarkdown 渲染——折叠块（`<details>`）+ 等宽字体展示原文 + 「下载 .md」按钮（Blob 下载，不引新依赖）；[client.ts](file:///D:/pythonProjects/UiCodeAnaylysis/MyUiCodeAnalysis/frontend/src/api/client.ts) 的类型加字段
- 未接入项目（无图）时的提示文案

### 4. 测试

- 新建 backend/tests/test_chat_doc.py：FakeLLM 意图判定 + 推断脚本 → 断言 docMarkdown 含「业务操作步骤」「前端交互视图」章节标题与要素编号；clarify 意图不走 specdoc；无图降级；无候选时 reply 提示
- 前端 vitest：ChatPage 渲染 docMarkdown 折叠块与下载按钮
- 回归现有 chat 测试（意图分流不破坏旧行为：无 graph 时行为完全等同现状）

## 验证

1. `backend`: `pytest tests -q`（基线 339 passed）+ `ruff check .`
2. `frontend`: `npx vitest run` + `tsc --noEmit`
3. 契约再生成后确认 requirement.ts 同步（前端类型含 docMarkdown）
4. 手动端到端：接入测试项目 → 完成分析 → 对话问「点赞收藏文章的前端交互视图」→ 收到含章节结构的 markdown 与命中页面名
