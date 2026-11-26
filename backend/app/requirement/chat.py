"""需求对话：把「一段憋出来的需求描述」变成「聊出来的需求描述」。

用户先说模糊想法，助手追问澄清（页面 / 操作 / 数据 / 规则边界），
信息足够时产出可直接交给需求定位的草稿文本。会话状态在前端持有，
本服务无状态：每次收全量历史，发一次模型调用，返回整段回复。

模型输出约定为 JSON `{"reply", "draftRequirement"}`（配合 json_object
模式），但兼容端点可能忽略该字段或带围栏——parse_json 兜底失败时把
整段返回当 reply，宁可少一个草稿按钮，也不让对话直接报错。
"""

from __future__ import annotations

from app.contracts import IRGraph
from app.core.config import Settings
from app.requirement.chat_doc import build_chat_doc, decide_intent
from app.requirement.judge import parse_json
from app.requirement.llm import LLMClient
from app.requirement.tools import REQUIREMENT_TOOLS, RequirementAnalysisTools
from app.schemas import Project
from app.specdoc.llm import OpenAICompatibleToolLLM

CHAT_SYSTEM_PROMPT = """你是前端需求澄清助手，帮用户把模糊的功能想法聊成一段清晰的功能需求描述。

规则：
1. 用中文回复，语气自然、一次最多追问 1~2 个关键问题，不要一次抛出一堆问题。
2. 追问围绕前端功能落地必需的信息：涉及哪个页面/入口、用户做什么操作、
   看到和提交什么数据、有哪些边界或校验规则。
3. 用户描述已经足够清晰时，停止追问，在 draftRequirement 里输出一段可直接
   交给需求定位系统使用的中文功能需求描述（具体、无寒暄、不使用第一人称），
   同时 reply 里简要说明草稿已生成。
4. 信息不足时 draftRequirement 输出 null，reply 里继续追问。

始终输出 JSON 对象：{"reply": string, "draftRequirement": string | null}。"""

_CHAT_TEMPERATURE = 0.7
_TOOL_CHAT_TEMPERATURE = 0.2
_MAX_REQUIREMENT_TOOL_CALLS = 8

BUSINESS_ANALYSIS_SYSTEM_PROMPT = (
    "你是当前项目代码的业务逻辑分析助手。你必须先使用提供的只读工具查找页面和交互，"
    "再根据工具结果回答。\n\n"
    "约束：\n"
    "1. 业务事实只能来自工具结果；不得编造节点、调用边、接口、状态变化或未读取的源码逻辑。\n"
    "2. 优先 search_pages → search_interactions → get_interaction_chain；\n"
    "   需要函数内部细节时再 read_source_range。\n"
    "3. 只有工具返回成功的源码片段才算读过；缺少可信 endLine、链路断点或工具错误要如实说明。\n"
    "4. 清楚区分代码已确认的信息与无法从当前分析结果确认的信息；证据不足时提出澄清问题。\n"
    "5. 用中文回答，尽可能列出页面、函数、接口以及 file:line-line 来源。\n"
)


class RequirementChatPreconditionError(Exception):
    """对话无法开始的前置条件失败（如模型未配置）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RequirementChatService:
    def __init__(
        self,
        llm: LLMClient | None,
        settings: Settings | None = None,
        *,
        specdoc_llm: OpenAICompatibleToolLLM | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings
        self._specdoc_llm = specdoc_llm

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        project: Project | None = None,
        graph: IRGraph | None = None,
        route_path: str | None = None,
    ) -> tuple[str, str | None, str | None, str | None]:
        """返回 (reply, draft_requirement, doc_markdown, doc_page)。

        带项目图时先做意图判定：用户在「问功能的前端交互视图」则走
        锚点定位 + specdoc 装配，直接在对话里返回文档；其余情况保持
        原有澄清流程（模型调用失败抛 LLMCallError）。
        """
        if self._llm is None:
            raise RequirementChatPreconditionError(
                "llm_unavailable",
                "模型未启用：需要配置 llm_enabled 与 llm_api_key 后才能使用 AI 对话",
            )
        if graph is not None and project is not None and self._settings is not None:
            latest_user_text = next(
                (item["content"] for item in reversed(messages) if item["role"] == "user"),
                "",
            )
            if route_path:
                outcome = await build_chat_doc(
                    project,
                    latest_user_text or route_path,
                    graph,
                    self._llm,
                    self._settings,
                    specdoc_llm=self._specdoc_llm,
                    route_path=route_path,
                )
                return outcome.reply, None, outcome.doc_markdown, outcome.page_name
            intent = await decide_intent(self._llm, messages)
            if intent.intent == "business_analysis" and intent.feature_text:
                if self._specdoc_llm is None:
                    raise RequirementChatPreconditionError(
                        "tool_calling_unavailable",
                        "当前模型客户端未启用工具调用，暂时无法分析项目业务逻辑",
                    )
                tools = RequirementAnalysisTools(project.root_path, graph)
                tool_messages = [*messages]
                if tool_messages and tool_messages[-1]["role"] == "user":
                    tool_messages[-1] = {
                        "role": "user",
                        "content": intent.feature_text,
                    }
                else:
                    tool_messages.append({"role": "user", "content": intent.feature_text})
                reply = await self._specdoc_llm.chat_with_tools(
                    BUSINESS_ANALYSIS_SYSTEM_PROMPT,
                    messages=tool_messages,
                    tools=REQUIREMENT_TOOLS,
                    executor=tools.execute,
                    max_tool_calls=_MAX_REQUIREMENT_TOOL_CALLS,
                    temperature=_TOOL_CHAT_TEMPERATURE,
                )
                return reply.strip(), None, None, None
            if intent.intent == "doc_query" and intent.feature_text:
                outcome = await build_chat_doc(
                    project,
                    intent.feature_text,
                    graph,
                    self._llm,
                    self._settings,
                    specdoc_llm=self._specdoc_llm,
                    fallback_text=latest_user_text,
                )
                return outcome.reply, None, outcome.doc_markdown, outcome.page_name
        content = await self._llm.chat_messages(
            CHAT_SYSTEM_PROMPT, messages, temperature=_CHAT_TEMPERATURE
        )
        reply, draft = self._interpret(content)
        return reply, draft, None, None

    def _interpret(self, content: str) -> tuple[str, str | None]:
        parsed = parse_json(content)
        if parsed is None:
            # 兜底：端点忽略了 json_object 约束——整段当 reply，只是没有草稿
            return content.strip(), None
        reply = parsed.get("reply")
        draft = parsed.get("draftRequirement")
        reply_text = reply.strip() if isinstance(reply, str) and reply.strip() else content.strip()
        draft_text = draft.strip() if isinstance(draft, str) and draft.strip() else None
        return reply_text, draft_text
