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

from app.contracts import EdgeKind, IRGraph, NodeKind, RequirementMap
from app.contracts.requirement import (
    Candidate,
    Feature,
    Match,
    Signal,
    SignalKind,
    Source,
    Subgraph,
    SubgraphCounts,
)
from app.core.config import Settings
from app.requirement.features import decompose
from app.requirement.judge import parse_json
from app.requirement.llm import LLMClient
from app.requirement.recall import (
    CandidateDetail,
    collect_page_evidences,
    page_nodes,
    recall,
    route_target_pages,
)
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
_NO_ROUTE_REPLY = (
    "图中没有找到路由「{route}」。请确认路由填写的是代码中的实际路径，"
    "并确保项目已完成最新一次代码分析。"
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


def locate_route_feature(graph: IRGraph, route_path: str) -> CandidateDetail | None:
    """按图中的 route 节点精确定位页面，不经过词表或模型。"""
    route = _normalize_route(route_path)
    nodes = {node.id: node for node in graph.nodes}
    pages, _route_by_page, _layout_ids = page_nodes(graph)
    page_ids = {page.id for page in pages}
    route_nodes = [
        node
        for node in graph.nodes
        if node.kind is NodeKind.route and _normalize_route(node.name) == route
    ]
    if not route_nodes:
        return None
    targets = route_target_pages(graph, nodes)
    page_ids_for_route = [
        page_id for node in route_nodes for page_id in targets.get(node.id, [])
    ]
    page_ids_for_route = list(
        dict.fromkeys(page_id for page_id in page_ids_for_route if page_id in page_ids)
    )
    if not page_ids_for_route:
        return None
    page = nodes.get(page_ids_for_route[0])
    if page is None:
        return None
    evidence = collect_page_evidences(graph, [page], set()).get(page.id, [])
    route_node = route_nodes[0]
    candidate = Candidate(
        nodeId=page.id,
        routeId=route_node.id,
        name=page.name,
        file=page.file,
        score=1.0,
        signals=[Signal(kind=SignalKind.routePath, value=route, score=1.0)],
        anchorNodeIds=[page.id, route_node.id],
        anchorEdgeIds=[
            edge.id
            for edge in graph.edges
            if edge.kind is EdgeKind.renders
            and edge.from_ == route_node.id
            and edge.to == page.id
        ]
        or None,
    )
    return CandidateDetail(
        candidate=candidate,
        ui_texts=[item.text for item in evidence if item.kind is SignalKind.uiText],
        prop_names=[item.text for item in evidence if item.kind is SignalKind.propName],
        prop_descriptions=[
            item.text for item in evidence if item.kind is SignalKind.propDescription
        ],
        api_urls=[item.text for item in evidence if item.kind is SignalKind.apiUrl],
        quote_corpus=[item.text for item in evidence],
        evidence=evidence,
    )


def _normalize_route(value: str) -> str:
    route = value.strip().replace("\\\\", "/")
    if not route.startswith("/"):
        route = "/" + route
    return route.rstrip("/") or "/" if route != "/" else "/"


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
    *,
    route_path: str | None = None,
    fallback_text: str | None = None,
) -> ChatDocOutcome:
    """定位 + 装配 + 渲染。路由直达优先，旧流程支持原文回退。"""
    best = locate_route_feature(graph, route_path) if route_path else locate_feature(graph, feature_text)
    if best is None and fallback_text and not route_path:
        best = locate_feature(graph, fallback_text)
    if best is None:
        if route_path:
            return ChatDocOutcome(reply=_NO_ROUTE_REPLY.format(route=_normalize_route(route_path)))
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
