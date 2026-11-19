# 需求输入 AI 对话功能实现计划

## Context

现在「需求定位」要求用户一次性写出完整的功能需求描述。用户希望先和 AI 对话：描述模糊想法 → AI 追问澄清 → 逐步形成结构化需求文本。已确认的设计决定：

- **入口**：独立对话页面（路由 `/projects/:projectId/chat`），工作台 header 加入口按钮
- **产出**：AI 在对话中给出「需求草稿」，用户点「填入需求定位输入框」回到工作台，检查后自己提交定位——保留人工确认，不自动触发
- **响应方式**：整段返回（非流式），与现有 `LLMClient` 能力一致
- **会话状态**：前端持有，后端无状态（不建仓储、不走任务状态机——单轮请求-响应足够）

## 后端改动

### 1. LLM 客户端支持多轮对话
文件：`backend/app/requirement/llm.py`

- `LLMClient` 协议新增方法 `chat_messages(system, messages: list[dict], *, temperature: float = 0) -> str`（`messages` 形如 `[{"role": "user"|"assistant", "content": str}]`）
- `OpenAICompatibleLLM.chat_messages`：payload 的 messages = system + 全量历史（现有 `chat` 是 system+单条 user，保持不动，requirement/specdoc 继续用它）；复用现有错误归一逻辑（超时/HTTP 状态码/LLMCallError）；`settings.llm_response_format` 为 True 时照旧带 `response_format=json_object`
- 对话用 temperature 0.7（聊天场景固定 0 会显得机械）

### 2. 请求/响应模型
文件：`backend/app/schemas.py`

- `ChatMessage`：`role: Literal["user", "assistant"]`、`content: str`（限长）
- `RequirementChatStart`：`messages: list[ChatMessage]`（上限 40 条，防滥用）
- `RequirementChatResult`：`reply: str`、`draftRequirement: str | None`

### 3. 对话服务
新建：`backend/app/requirement/chat.py`

- System prompt：需求澄清助手角色——一次只追问 1~2 个关键问题（面向前端功能：页面、操作、数据、规则边界）；信息足够时输出 `draftRequirement`（一段可直接交给需求定位使用的中文功能需求描述）；输出 JSON `{"reply": "...", "draftRequirement": "..." | null}`（配合 json_object 模式）
- `RequirementChatService`（注入 `LLMClient | None`）：
  - llm 为 None → `RequirementChatPreconditionError("llm_unavailable")`（仿 `SpecDocPreconditionError` 惯例）
  - 组装多轮消息调 `chat_messages`；解析 JSON，解析失败兜底把整段返回当 reply（`draftRequirement=None`）
  - `LLMCallError` 向上抛，由 API 层映射 HTTP

### 4. 依赖注入与路由
文件：`backend/app/api/deps.py`、`backend/app/api/v1/requirements.py`

- deps 新增 `get_requirement_chat_service`（requirement 仓储不需要——服务无状态，只注 `LLMClient`）
- `requirements.py` 新增同步端点 `POST /projects/{project_id}/requirement-chat`：
  - 校验项目存在（404）
  - `llm_unavailable` → 503；`LLMCallError` → 502（带 message）
  - 返回 `RequirementChatResult`

### 5. 测试假件
文件：`backend/tests/fakes.py`

- `FakeLLMClient` 补 `chat_messages`（记录调用供断言；`responses`/`error` 语义与 `chat` 一致）

## 前端改动

### 6. API 封装
文件：`frontend/src/api/types.ts`、`frontend/src/api/client.ts`

- types 加 `ChatMessage` / `RequirementChatStart` / `RequirementChatResult`（手写镜像后端模型，沿用现有惯例）
- client 加 `api.chatRequirement(projectId, payload)` → `POST /projects/{id}/requirement-chat`

### 7. 对话页面
新建：`frontend/src/pages/ChatPage.tsx`

- 消息列表（用户右侧/AI 左侧气泡，Tailwind，跟随现有暗色风格）+ 底部输入框 + 发送按钮
- 发送用 `useMutation` 调 `chatRequirement`；pending 时输入框禁用 + loading 指示
- 历史由 `useState` 持有（含用户与 AI 消息，全量随请求发送）
- `draftRequirement` 非空时顶部 banner：展示草稿内容 + 按钮「填入需求定位输入框」→ `navigate(`/projects/${projectId}`, { state: { draftRequirement } })`
- 错误处理：503 `llm_unavailable` 显示「模型未启用（需配置 llm_enabled 与 API Key）」；其他错误显示 message；提供「重试」
- 空态：欢迎语 + 示例开场（如「我想在用户列表页加一个状态筛选」）
- 「清空对话」按钮重置本地历史

### 8. 路由与入口
文件：`frontend/src/App.tsx`、`frontend/src/pages/WorkbenchPage.tsx`、`frontend/src/components/RequirementPanel.tsx`

- App.tsx 加 `<Route path="/projects/:projectId/chat" element={<ChatPage />} />`
- WorkbenchPage header（L275 附近）加「AI 需求助手」按钮（lucide `MessagesSquare` 图标）→ 链接到 chat 页
- 草稿回填：WorkbenchPage 读 `useLocation().state?.draftRequirement`，作为 `initialRequirement` prop 传给 RequirementPanel（L672），并在消费后 `navigate(replace)` 清掉 state 防刷新重复注入；RequirementPanel 用它初始化 textarea（`useState` 初值 + prop 变化时同步）

### 9. 前端测试
新建：`frontend/src/pages/ChatPage.test.tsx`（跟随现有 vitest + testing-library 组件测试模式）

- 发送消息后展示 AI 回复；出现草稿 banner 且点击后跳转携带 state；503 错误显示提示

## 后端测试

新建：`backend/tests/test_requirement_chat.py`（用 conftest `client` fixture + 依赖覆盖注入 FakeLLMClient）

- 成功：返回 reply + draftRequirement；断言请求体里含全部历史消息（FakeLLMClient 记录）
- JSON 兜底：模型返回非 JSON → 整段当 reply，draftRequirement 为 None
- 503：未注入 llm
- 502：`FakeLLMClient(error=LLMCallError(...))`
- 404：项目不存在

## 验证

1. `pytest tests -q`（backend 全量回归，当前基线 327 passed）
2. `cd frontend; npm run test; npm run typecheck`（vitest + tsc）
3. `.venv\Scripts\ruff.exe check .`
4. 手动端到端（可选）：起 backend + `cd frontend; npm run dev`，从工作台进对话页聊一轮，点「填入需求定位输入框」确认草稿回填后能提交定位
