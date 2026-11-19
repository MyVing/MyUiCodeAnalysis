"""对话式规格文档：把「问功能」变成「对话里直接拿到前端交互视图」。

用户在 AI 需求助手里问「点赞收藏文章的前端交互视图和逻辑」，本模块负责
把这句话接到项目代码上：

1. 意图判定（一次模型调用）：区分「查文档」与「澄清需求」；
2. 定位：decompose + 锚点反向召回（与需求定位同一套机制，命中带完整
   归属路径），取得分最高的页面候选；
3. 生成：合成 RequirementMap / SpecDocTask，复用 SpecDocOrchestrator 装配
   与 render_markdown 渲染——文档结构与《2.2 转账》模板同构。

对话产物是即时的、不落库；正式产物仍走 requirement → specdoc 任务链。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.contracts import IRGraph, RequirementMap
from app.contracts.requirement import Feature, Match, Source, Subgraph, SubgraphCounts
from app.core.config import Settings
from app.requirement.features import decompose
from app.requirement.judge import parse_json
from app.requirement.llm import LLMClient
from app.requirement.recall import CandidateDetail, recall
from app.schemas import Project, SpecDocTask
from app.specdoc.orchestrator import SpecDocOrchestrator
from app.specdoc.render import render_markdown

INTENT_SYSTEM_PROMPT = """你是前端需求助手的意图判定器。根据对话历史与最新用户消息，\
判断用户是否在询问某个功能的「前端交互视图 / 交互逻辑 / 业务操作步骤」类文档。

规则：
1. intent=doc_query：用户想看某个已实现（或规划中）功能在页面上的交互细节，
   featureText 输出该功能的中文描述（去掉寒暄与指代，保留页面 / 操作 / 数据信息）。
2. intent=clarify：其他一切情况——需求澄清、闲聊、与文档无关的问题。
   featureText 输出 null。
3. 只做判定，不要回答用户的问题。

始终输出 JSON 对象：{"intent": "doc_query" | "clarify", "featureText": string | null}。"""

_INTENT_TEMPERATURE = 0.2

_NO_MATCH_REPLY = (
    "在项目代码里没有定位到与这个描述相关的页面。可以换一种说法："
    "带上页面名称、界面文案或接口地址（如 GET /api/xxx）会更容易命中；"
    "也可以在「需求定位」里提交完整需求查看完整候选。"
)
_NO_GRAPH_REPLY = (
    "当前项目还没有可用的代码解析产物。请先在项目里完成一次代码分析"
    "（并确保分析成功），之后我就能基于真实代码回答交互视图与逻辑的问题了。"
)


@dataclass(frozen=True)
class ChatDocOutcome:
    """一次「查文档」对话的产物：导语 + markdown 文档 + 命中页面名。"""

    reply: str
    doc_markdown: str | None = None
    page_name: str | None = None


@dataclass(frozen=True)
class _Intent:
    intent: str
    feature_text: str | None


async def decide_intent(llm: LLMClient, messages: list[dict[str, str]]) -> _Intent:
    """意图判定：doc_query 走文档生成，clarify 走原有澄清流程。

    解析失败兜底为 clarify——宁可少一次文档生成，不能让对话报错。
    """
    content = await llm.chat_messages(
        INTENT_SYSTEM_PROMPT, messages, temperature=_INTENT_TEMPERATURE
    )
    parsed = parse_json(content)
    if parsed is None:
        return _Intent(intent="clarify", feature_text=None)
    intent = parsed.get("intent")
    feature_text = parsed.get("featureText")
    return _Intent(
        intent=intent if intent == "doc_query" else "clarify",
        feature_text=(
            feature_text.strip() if isinstance(feature_text, str) and feature_text.strip() else None
        ),
    )


def locate_feature(
    graph: IRGraph, feature_text: str, top_k: int = 3
) -> CandidateDetail | None:
    """用锚点反向召回定位页面，返回得分最高的候选（无命中返回 None）。"""
    candidates = []
    for feature in decompose(feature_text):
        result = recall(graph, feature.terms, top_k=top_k)
        candidates.extend(result.candidates)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.candidate.score)


async def build_chat_doc(
    project: Project,
    feature_text: str,
    graph: IRGraph,
    llm: LLMClient,
    settings: Settings,
) -> ChatDocOutcome:
    """定位 + 装配 + 渲染。对话产物即时返回、不落库。"""
    best = locate_feature(graph, feature_text)
    if best is None:
        return ChatDocOutcome(reply=_NO_MATCH_REPLY)

    requirement_map = _synthetic_requirement_map(project, best, feature_text, graph)
    task = SpecDocTask(
        id=f"chat-{requirement_map.requirementId}",
        project_id=project.id,
        requirement_id=requirement_map.requirementId,
        status="running",
        dry_run=False,
        created_at=datetime.now(UTC),
    )
    orchestrator = SpecDocOrchestrator(settings, llm)
    doc = await orchestrator.run(project, task, requirement_map, graph)
    markdown = render_markdown(doc)
    page = best.candidate.name or best.candidate.nodeId
    return ChatDocOutcome(
        reply=(
            f"已定位到页面《{page}》（基于项目代码的锚点反向归属），"
            "以下是该页面相关的业务操作步骤与前端交互视图。"
        ),
        doc_markdown=markdown,
        page_name=page,
    )


def _synthetic_requirement_map(
    project: Project, best: CandidateDetail, feature_text: str, graph: IRGraph
) -> RequirementMap:
    """把召回候选包装成 specdoc 装配器可消费的最小 RequirementMap。

    引文取候选的最高分信号文本（锚点反向归属的依据，天然可回图核对）；
    置信度按 AI 推断惯例封顶 0.6——对话定位没有经过 judge 全量校验，
    不能冒充实测证据的 0.95。
    """
    candidate = best.candidate
    requirement_id = f"chat-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    signals = sorted(candidate.signals, key=lambda item: -item.score)
    quote = signals[0].value if signals else candidate.name
    node_ids = list(candidate.anchorNodeIds or [candidate.nodeId])
    # 召回候选分数>0（种子命中才有候选），封顶后即对话定位的结论置信度
    confidence = min(candidate.score, 0.6)
    subgraph = Subgraph(
        nodeIds=node_ids,
        edgeIds=list(candidate.anchorEdgeIds or []),
        counts=SubgraphCounts(
            component=0, hook=0, state=0, api=0, event=0, fn=0, route=0, element=0
        ),
    )
    return RequirementMap(
        schemaVersion="1.0",
        requirementId=requirement_id,
        projectId=project.id,
        analysisId=graph.analysisId,
        requirement=feature_text,
        createdAt=datetime.now(UTC),
        dryRun=False,
        features=[Feature(id="f1", text=feature_text)],
        matches=[
            Match(
                featureId="f1",
                pageNodeId=candidate.nodeId,
                routeId=candidate.routeId,
                confidence=confidence,
                # 候选得分是多个信号之和，可能 >1；契约要求 [0,1]，归一回原比例
                modelConfidence=min(candidate.score, 1.0),
                quote=quote,
                reason="对话助手：锚点反向召回得分最高候选",
                subgraph=subgraph,
                source=Source.ai,
            )
        ],
    )
