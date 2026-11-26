# AI 需求助手 Agent 与源码工具调用改造方案

## 1. 背景与目标

当前 AI 需求助手支持需求澄清和页面交互文档查询；项目侧已有 IR 图、页面召回、确定性交互链路推导和 LangGraph 需求定位编排。SpecDoc 专用 OpenAI-compatible client 已提供工具调用循环，本方案按“复用现有循环”接入需求助手，不再由 LangGraph 重复驱动同一轮工具调用。

本方案拟将需求助手扩展为一个受控的业务逻辑分析 Agent：模型根据用户问题选择只读工具；工具从当前项目 IR 图和项目源码读取事实；模型综合工具结果形成回答。第一版以**单次会话/请求内分析**为边界，不新增跨会话索引、向量库或 Agent 状态持久化。

核心原则：

1. **静态分析和源码是事实来源，模型负责选择工具、解释和表达。**不得让模型创建分析图中不存在的节点、边或调用路径。
2. **读取源码必须有可信的 `file`、`start_line` 和 `end_line`。**不再采用“给一个起始行再猜读多少行”的接口设计。
3. **工具只读且范围受限。**每次只返回用户问题需要的局部源码，不执行代码、不写文件、不访问项目目录之外的内容。
4. **证据不足要如实说明。**节点无结束行、链路中断、文件缺失或静态分析无法确定时，不猜测补全。
5. 保持现有需求澄清、文档生成和 `/requirement-chat` 接口行为兼容，分阶段接入 Agent。

## 2. 当前项目基础与限制

### 2.1 可复用能力

- `backend/app/requirement/chat.py`：`RequirementChatService.chat` 负责对话意图分流，当前支持文档查询和需求澄清，是 Agent 服务接入点。
- `backend/app/requirement/chat_doc.py`：包含按路由定位、页面召回、规格文档生成流程，可保留既有文档查询能力。
- `backend/app/requirement/orchestrator.py`：已有 LangGraph 工作流，可参考其状态组织、节点边界和模型调用预算。
- `backend/app/requirement/recall.py`：提供图上的页面和锚点召回能力，可供页面搜索工具复用。
- `backend/app/requirement/chains.py`：`derive_chains(graph)` 从 IR 图确定性推导交互链路；模型不能参与构造链路。
- `backend/app/contracts/ir.py`：IR 节点以 `file`、`loc` 保存源码位置，扩展元数据可放在 `attrs` 中。
- `backend/app/specdoc/llm.py`：现有 `OpenAICompatibleToolLLM.chat_with_tools` 提供工具调用循环；本次扩展其历史 messages 支持并保持旧 user 调用兼容。

### 2.2 `endLine` 的现状与必须确认的覆盖范围

当前 AST Worker 已在部分 IR 节点中通过 `attrs.endLine` 提供源码范围，例如：

- 组件：`workers/ast-worker/src/components.ts` 识别声明的结束行，`workers/ast-worker/src/ir-builder.ts` 将其放进组件 attrs。
- 部分事件处理函数：`workers/ast-worker/src/symbols.ts` 解析函数结束行，`workers/ast-worker/src/events.ts` 将它写入函数节点 attrs。
- 请求链函数：`workers/ast-worker/src/api.ts` 为部分函数节点写入 `attrs.endLine`。

`endLine` 不是 `IRNode` 必填字段，也不是所有节点都保证具备；页面元素、状态、接口、外部节点和未具名回调尤其不能假定有完整范围。因此实现前必须先做节点类型与样本图覆盖率盘点。

**源码读取工具只接受已存在且可信的范围，不由模型生成或推算 `end_line`。**如果被选节点没有有效 `endLine`，工具返回 `range_unavailable` 和节点信息；Agent 可改查其它有范围的证据，或明确告诉用户无法读取完整源码范围。后续若要扩大覆盖率，应由静态分析 Worker 对可识别声明补产结束行，并通过契约/fixture 测试保证数据质量。

## 3. 目标架构

```text
POST /projects/{project_id}/requirement-chat
  ├─ 校验项目，读取最新 IRGraph
  └─ RequirementChatService
       ├─ 无图 / 普通需求澄清 → 保持现有对话流程
       ├─ 已有的明确文档查询 → 可保留 chat_doc 既有路径
       └─ 业务逻辑分析问题 → RequirementAnalysisTools + 现有 `chat_with_tools` 循环
            ├─ agent_node：模型判断下一步并发起工具调用，或给最终答复
            ├─ tools_node：校验并执行白名单只读工具
            ├─ evidence_node：限制、整理、去重工具证据
            └─ END：输出回答和可选 citations
```

Agent 状态只保存本次请求的消息、工具调用次数、工具结果和证据摘要。`Project`、`IRGraph`、文件读取器等非序列化依赖由服务闭包或依赖注入提供，不放入 checkpoint。第一版不需要持久化 checkpointer；若未来需要跨请求恢复长任务，再单独评估。

工具 client 负责 `assistant tool_calls → executor → tool result → assistant` 往返；设置最大工具调用次数，达到预算后返回受控错误，不继续循环。

## 4. 第一版工具清单

优先提供少量能回答“在哪个页面、做了什么、后续调用什么”的工具；工具内部优先复用现有 Python 逻辑。

### 4.1 `search_pages`

```json
{
  "query": "订单发货",
  "top_k": 5
}
```

返回当前图中的页面候选：`page_id`、页面名称、路由、召回信号和分数。限制候选数，并明确分数表示召回相关性而非业务结论置信度。

### 4.2 `search_interactions`

```json
{
  "page_id": "当前项目中的页面节点 ID",
  "query": "确认发货",
  "top_k": 5
}
```

在 `derive_chains(graph)` 的结果中检索页面交互链路，返回匹配链路 ID、元素文案、处理函数/API 摘要、文件行位置及断点信息。`page_id` 必须属于本次项目图。

### 4.3 `get_interaction_chain`

```json
{
  "chain_id": "当前图中真实存在的 ActionChain ID"
}
```

返回完整链路步骤、节点/边引用、每一步的 `file`、`loc.line`、可用的 `attrs.endLine` 和 `terminatedAt`。工具应从当前图现场推导或读取链路，不能接受模型提交任意节点序列来拼造路径。

### 4.4 `read_source_range`

```json
{
  "node_id": "当前项目图中的源码节点 ID",
  "file": "src/pages/OrderPage.tsx",
  "start_line": 18,
  "end_line": 64
}
```

语义上要求输入范围完整（必须有 `end_line`）；但为避免模型伪造范围，推荐**实际工具 schema 只暴露 `node_id`**，由后端从当前图节点读取 `file`、`loc.line` 和 `attrs.endLine`。内部仍以严格的 `start_line/end_line` 范围读取器执行。这样满足“必须有 end_line 才读”的要求，同时避免让模型猜结束行或传入其它文件路径。

如果工具框架要求把范围字段暴露给模型，则所有字段仍必填，后端必须逐项与 IR 节点的可信范围比对；不得因模型传入更宽范围而扩大读取。

成功返回：

```json
{
  "status": "ok",
  "node_id": "...",
  "file": "src/pages/OrderPage.tsx",
  "start_line": 18,
  "end_line": 64,
  "content": "...",
  "truncated": false
}
```

失败返回明确状态，而不是空内容伪装成功：`range_unavailable`、`file_not_found`、`range_invalid`、`range_too_large`、`file_outside_project`、`read_error`。错误消息不泄露绝对路径或服务器敏感信息。

源码内容只在当前请求中作为工具结果返回模型，不写入会话存储或日志。限制源码总行数、字符数和模型上下文预算。若一个完整声明超过上限，不自动静默截断为“完整函数”；返回 `range_too_large`，由 Agent 说明范围过大，或未来设计经用户/策略确认的分段读取。

## 5. 源码读取安全与范围校验

实现 `read_source_range(project_root, node)` 的纯服务函数，并保证：

1. `node_id` 必须能在本次 `IRGraph` 中查到；只允许受支持的项目内源码节点类型。
2. 文件路径只能取 IR 节点上的相对 `file`，拒绝绝对路径、空路径、`..` 路径段和非普通文件。
3. 将路径解析为真实路径后，确认它仍位于 `project.root_path` 内，防止符号链接逃逸。
4. `loc.line`、`attrs.endLine` 都是有效正整数，且 `endLine >= loc.line`；缺少任何一项就不读取。
5. `endLine - startLine + 1` 不超过配置上限；返回内容另设字符数/字节数上限。
6. 读取时使用 UTF-8（必要时按项目可接受的编码策略处理），保留实际起止行并区分文件换行；文件行数不足或内容变化导致范围无效时如实返回错误。
7. 工具结果不执行源码，不将源码写入日志；日志只记录工具名、节点 ID、行数、耗时和状态。
8. 输入的 `file/start_line/end_line` 不得覆盖 IR 中已经确认的节点路径与范围。

参数上限建议先设成配置常量，例如单节点最多 120 行、最多 20,000 字符、单次对话累计源码字符预算 40,000；上线前根据目标模型上下文和实际项目函数分布调整。超过上限时失败并报告，不悄悄裁剪后声称读取了完整逻辑。

## 6. Function calling 与模型适配

现有 `OpenAICompatibleLLM` 使用 `/chat/completions`，请求中没有 `tools`，响应解析也只读 `choices[0].message.content`。实现 Agent 前需验证实际配置端点支持的工具调用协议。

推荐增加独立的结构化调用接口，而不是破坏现有 `chat` / `chat_messages`：

```python
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]

@dataclass
class AgentTurn:
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str | None

async def chat_with_tools(..., tools: list[ToolDefinition], messages: ...) -> AgentTurn:
    ...
```

要求：

- 请求带工具 JSON Schema；模型返回工具调用时解析 `tool_calls` 与参数。
- 参数通过 Pydantic/JSON Schema 校验，未知工具名、非法 JSON、缺字段、越界字段拒绝执行。
- 将工具执行结果以端点要求的 tool message 结构回传，再调用模型。
- 复用 client 内建工具循环执行单轮请求、工具执行和预算；业务工具由需求助手 executor 提供。
- 保留现有超时、HTTP 错误和 `LLMCallError` 归一化语义；工具调用模式不支持时返回可诊断错误，或按配置禁用 Agent 并回退到现有澄清逻辑。
- 对真实配置的 OpenAI-compatible 服务做协议验证；“接口兼容”不等于支持相同的 tools 参数与响应行为。

可以采用 LangChain `bind_tools` 适配器，但当前项目已有自建 LLM 客户端，首版优先保持统一配置与错误处理；不因为 Agent 接入迁移全部普通 LLM 调用。LangGraph 只承担状态流转，不要求替换现有 `LLMClient`。

## 7. 对话策略与最终回答

System prompt 必须要求：

- 先使用页面/交互工具定位，再按需读取源码；禁止无依据直接回答具体实现细节。
- 只引用工具返回的事实；不得编造图边、调用顺序、状态变化、接口或源码。
- 源码工具只可读取工具选中的图节点范围；不能要求工具读取任意路径或任意行段。
- `terminatedAt` 表示静态链路断点，必须向用户披露，不能当作“没有后续行为”。
- 区分“静态图确认”“源码片段确认”“未找到/未覆盖”；源码被截断或未读取完整时不得声称看过完整函数。
- 证据不足时可调用其它只读工具；仍不足则明确说明并提出一个最有帮助的澄清问题。

最终响应可以继续返回现有 `reply`、`draft_requirement`、`doc_markdown`、`doc_page`；可选新增 `citations`，每条包含 `node_id`、`file`、`start_line`、`end_line`、证据类型和简短引用。若前端暂不展示 citations，API 新字段应可选，以保持兼容。

## 8. API 与服务集成

保持 `POST /projects/{project_id}/requirement-chat` 路径不变。服务层根据项目分析图、用户意图和请求模式决定走：

1. 现有澄清对话；
2. 现有明确文档查询路径；
3. 新增业务逻辑 Agent。

具体分流可先用 `decide_intent` 扩充意图类型（如 `business_analysis`），或采用明确请求字段控制 Agent；推荐第一阶段不改变现有必填字段，仅在服务层通过意图判定分流，并保留可确定的 `route_path` 快速路径。

没有分析图时维持现有纯澄清降级。Agent 模型调用失败按现有 `LLMCallError` 映射 502；工具参数/文件范围错误应作为工具结果供 Agent 合理解释，不应吞掉安全拒绝。对话历史和会话存储行为维持现状；源码不额外持久化。

## 9. 配置、预算与可观测性

为 Agent 设置清晰上限，初始值作为实现默认值并可配置：

- 每轮最大 Agent 模型决策次数：4。
- 每次用户请求最大工具调用数：8。
- 同一工具重复参数调用去重或限制为 2 次。
- 页面候选数、链路候选数各设上限（例如 5 / 8）。
- 单次源码范围最多 120 行/20,000 字符；单轮请求源码总量例如 40,000 字符。
- 每次模型调用和工具调用均有超时；整体请求沿用 API 的请求超时约束。
- 记录 trace/request ID、工具名、参数摘要、状态、耗时、结果大小和 LLM 调用次数；不记录 API key，不默认记录完整源码正文。

预算耗尽时停止新增工具调用，基于已有证据输出部分结论及限制，或请求用户缩小范围；不能无限重试。

## 10. 文件改造范围

### 必要改造

- `backend/app/requirement/chat.py`：在现有服务内分流到业务逻辑 Agent，保留原对话与文档路径。
- 新建 `backend/app/requirement/agent.py`：定义 Agent 状态、LangGraph、路由和工具循环。
- 新建 `backend/app/requirement/tools.py`（或拆分 `source_reader.py`）：实现只读业务工具和源码范围校验。
- `backend/app/requirement/llm.py`：增加可选的工具调用请求/响应类型与端点适配，不破坏普通聊天接口。
- `backend/app/api/v1/requirements.py`、`backend/app/schemas.py`：按需增加 citations 或 Agent 运行选项，保持现有字段兼容。
- `backend/app/api/deps.py`：装配 Agent 和工具依赖，传入当前项目根目录与分析图。
- `backend/tests/test_requirement_chat.py` 及新增 Agent/工具测试：覆盖分流、工具循环和安全边界。

### 覆盖率检查或后续改造

- `backend/app/contracts/ir.py`：维持 `attrs.endLine` 为扩展元数据，不必把 `endLine` 加进所有节点必填契约；如果要强类型化，可增加可选字段，但需同步共享 schema、生成类型和旧图兼容策略。
- `workers/ast-worker/src/ir-builder.ts`、`components.ts`、`events.ts`、`api.ts`：盘点并补齐需要支持的项目内声明范围；只对 AST 能确定边界的声明产出 `endLine`。
- `workers/ast-worker/tests/*`、`backend/tests/*`：增加 endLine 行为及历史图缺字段的兼容测试。
- `frontend/src/api/types.ts`、`frontend/src/pages/ChatPage.tsx`：只有需要展示引用来源/工具进度时再增加前端改动；第一版可只显示引用在文本答复中。

## 11. 测试矩阵与验收标准

### 源码读取器

- 有效节点读取的首尾行与内容准确，行号 1-based 且包含结束行。
- 缺少 `endLine`、无 `loc`、`endLine < loc.line` 时返回明确不可读状态，不尝试估算。
- 绝对路径、`..` 路径穿越、越界符号链接、目录、缺失文件均拒绝。
- 范围超行数/字符预算拒绝；不返回伪称完整的静默截断内容。
- 节点不存在、属于其他项目图、外部节点无项目文件时拒绝。
- 源码工具只读，不改变文件；日志不含源码正文和密钥。

### Agent 与 function calling

- 模型选择工具、参数校验、工具结果回传和最终回答完整走通。
- 未知工具、非法参数、伪造 node ID/chain ID 不执行。
- Agent 能先搜索页面、再搜链路、最后读取源码；工具结果不足时不编造。
- 遇到 `terminatedAt`、无 `endLine`、超范围、文件缺失时答复保留证据边界。
- 调用上限/轮数上限可终止循环；LLM 超时/HTTP 错误按现有错误映射返回。
- 端点不支持 tools 时有确定降级路径，不会把协议错误解释为“没有业务逻辑”。

### 回归与验收

- 现有普通需求澄清、路由直达文档查询、自然语言文档查询、会话读写保持原行为。
- API 响应已有字段不变；可选 citations 缺省时旧前端仍正常。
- 在真实项目样本上验证代表性问题：入口查询、按钮后续行为、接口调用、状态变更、静态链路断点、没有证据的需求。
- 评估回答中每个可核对的业务事实都能关联到 IR 节点或源码行范围；未证实事实明确标注为未知。

## 12. 分阶段实施建议

### 阶段 A：盘点范围数据

统计真实 IR 图中各节点类型 `file + loc + attrs.endLine` 的覆盖率，确认哪些交互链路 handler 可读取完整源码范围。确定读取上限、编码策略和失败契约。若覆盖率不足，只补必要的 AST 声明类型，不追求每种 IR 节点都带结束行。

### 阶段 B：实现只读工具

实现页面搜索、交互搜索、链路读取和以 IR 节点为入口的源码范围读取；先在后端单测和 API 集成测试验证安全边界。此阶段不要求 LLM 自主调用工具。

### 阶段 C：工具调用协议适配

针对项目实际配置的 OpenAI-compatible endpoint 验证 tools 协议，增加模型单轮工具调用解析、错误映射和 Fake 测试；普通 chat 路径保持不变。

### 阶段 D：LangGraph Agent 与接入

实现有预算上限的 `agent → tools → agent` 图，将业务逻辑分析意图接入现有聊天服务；保留澄清和规格文档分支，支持关闭 Agent 的回退开关。

### 阶段 E：证据展示与质量评估

补充 citations 或前端引用展示，收集一组业务问题与人工核验答案，评估工具选择正确率、事实引用率、无证据拒答质量、单轮模型调用数和响应延迟，再决定是否扩大源码范围覆盖或增加更多工具。

## 13. 方案结论

可以将“按文件范围读取后返回内容”做成单会话工具，整体开销和状态复杂度都可控。与固定上下文窗口相比，强制基于 `end_line` 读取更适合获取完整函数/组件；关键是 `end_line` 必须来自静态分析产物并由后端校验，不能让模型凭空决定。推荐工具面向模型只接收当前图中的 `node_id`，再由后端解析可信的 `file + loc + endLine` 执行读取；范围缺失或超限时明确失败。