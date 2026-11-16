"""确定性召回：从图里粗筛出「页面候选」，每条候选都带可解释的命中信号。

核心判断：准确率的地板由这一步决定，模型只负责在候选里挑。因此这里的每一分
都必须能说出「哪个检索词命中了哪段原文」，不允许出现无法解释的分数。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.contracts import (
    Candidate,
    EdgeKind,
    IRGraph,
    IRNode,
    NodeKind,
    RequirementWarning,
    Signal,
    SignalKind,
)

# RequirementWarning 的 severity 用 requirement 契约自己的枚举：IR 契约里也有同名
# 的 Severity，但那是另一个类型，混用会让等值比较莫名其妙地失败。
from app.contracts.requirement import Severity
from app.requirement.lexicon import RequirementTerms, SearchTerm
from app.requirement.text import ascii_words, contains_cjk, normalize

# 各信号的权重。排序依据：越贴近「这个页面在做什么」的信号权重越高。
#   route path / 组件名 —— 页面身份的声明式来源
#   文案 / API url    —— 页面行为的直接证据
#   props 描述        —— 作者写的注释，最接近需求原文
#   props 字段名      —— 页面能力的间接证据
SIGNAL_WEIGHTS: dict[SignalKind, float] = {
    SignalKind.routePath: 1.0,
    SignalKind.componentName: 1.0,
    SignalKind.uiText: 0.9,
    SignalKind.apiUrl: 0.8,
    SignalKind.propDescription: 0.7,
    SignalKind.propName: 0.6,
}

# 同一信号在同一页面里重复命中时逐次折扣，避免「同一批文案反复出现用户」
# 这种偶然重复压倒真正相关的信号。
_REPEAT_DECAY = 0.6

DEFAULT_TOP_K = 30
# 每个候选最多保留多少条信号 / 文案 / props，防止单候选撑爆模型上下文
MAX_ITEMS_PER_CANDIDATE = 10
# 每类信号最多保留几条：routePath / apiUrl 这类低频高价值的信号不被文案挤掉
MAX_SIGNALS_PER_KIND = 3


@dataclass
class CandidateDetail:
    """候选的完整上下文：契约字段 + 下发给模型的结构化摘要。"""

    candidate: Candidate
    ui_texts: list[str] = field(default_factory=list)
    prop_names: list[str] = field(default_factory=list)
    api_urls: list[str] = field(default_factory=list)


@dataclass
class RecallResult:
    candidates: list[CandidateDetail]
    warnings: list[RequirementWarning]
    # nodeId → 该页面对应的 route 节点 id（降级模式下为空）
    route_by_page: dict[str, str]


def recall(
    graph: IRGraph,
    terms: RequirementTerms,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> RecallResult:
    """从图里召回页面候选，按得分降序，截断到 top_k。"""
    if not terms.terms:
        return RecallResult(
            candidates=[],
            warnings=[
                RequirementWarning(
                    code="requirement_empty",
                    severity=Severity.warning,
                    message="需求中没有可用的检索词，无法召回候选页面。",
                )
            ],
            route_by_page={},
        )

    warnings: list[RequirementWarning] = []
    pages, route_by_page = _page_nodes(graph)
    if not route_by_page:
        warnings.append(
            RequirementWarning(
                code="no_route_nodes",
                severity=Severity.warning,
                message="图中没有 route 节点，已降级为「未被任何组件渲染的顶层组件」作为页面候选。",
            )
        )
    if not pages:
        warnings.append(
            RequirementWarning(
                code="no_page_candidates",
                severity=Severity.warning,
                message="图中没有可定位的页面组件（既无 route 节点，也无顶层组件）。",
            )
        )
        return RecallResult(candidates=[], warnings=warnings, route_by_page={})

    evidences = _collect_evidences(graph, pages)
    scored = [
        detail
        for page in pages
        if (detail := _score_page(page, evidences[page.id], terms, route_by_page.get(page.id)))
        is not None
    ]
    scored.sort(key=lambda item: (-item.candidate.score, item.candidate.nodeId))

    if len(scored) > top_k:
        warnings.append(
            RequirementWarning(
                code="candidates_truncated",
                severity=Severity.info,
                message=f"共召回 {len(scored)} 个候选，只保留得分前 {top_k} 个。",
            )
        )
    return RecallResult(
        candidates=scored[:top_k],
        warnings=warnings,
        route_by_page=route_by_page,
    )


@dataclass(frozen=True)
class _Evidence:
    """一条可命中的原文片段。"""

    kind: SignalKind
    text: str


def _page_nodes(graph: IRGraph) -> tuple[list[IRNode], dict[str, str]]:
    """确定页面候选集合，以及页面 → route 节点的映射。

    有 route 节点时，页面就是 route 渲染的目标；没有时降级为顶层组件
    （不被任何组件 renders 的组件）。降级必须显式告警——它改变了候选的含义。
    """
    nodes = {node.id: node for node in graph.nodes}
    route_by_page: dict[str, str] = {}

    for edge in graph.edges:
        if edge.kind is not EdgeKind.renders:
            continue
        source = nodes.get(edge.from_)
        target = nodes.get(edge.to)
        if source is None or source.kind is not NodeKind.route:
            continue
        if target is None or not _is_component(target):
            continue
        # 同一页面被多条路由渲染时保留先出现的那条：排序稳定，结果可复现
        route_by_page.setdefault(target.id, source.id)

    if route_by_page:
        pages = sorted((nodes[node_id] for node_id in route_by_page), key=lambda node: node.id)
        return pages, route_by_page

    rendered = {edge.to for edge in graph.edges if edge.kind is EdgeKind.renders}
    component_ids = {
        node.id
        for node in graph.nodes
        if _is_component(node) and node.attrs.get("external") is not True
    }
    return [nodes[node_id] for node_id in sorted(component_ids - rendered)], {}


def _is_component(node: IRNode) -> bool:
    return node.kind in (NodeKind.component, NodeKind.server_component)


def _collect_evidences(graph: IRGraph, pages: list[IRNode]) -> dict[str, list[_Evidence]]:
    """为每个页面收集可命中的原文。

    归属规则保守：只收页面自己在源码里写下的内容，以及它**直接**连接到的实体。
    刻意不递归收集子组件的文案——否则所有页面都会因共享同一批基础组件而拿到
    相同文案，候选之间失去区分度。
    """
    nodes = {node.id: node for node in graph.nodes}
    by_page: dict[str, list[_Evidence]] = {page.id: [] for page in pages}
    page_ids = set(by_page)

    # 先算一遍 url 可达性：api 节点通常挂在工具函数上，而不是直接连页面
    api_urls_by_node: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind is not EdgeKind.requests:
            continue
        api = nodes.get(edge.to)
        url = api.attrs.get("url") if api is not None else None
        if api is not None and api.kind is NodeKind.api and isinstance(url, str):
            api_urls_by_node[edge.from_].append(url)

    for page in pages:
        by_page[page.id].append(_Evidence(SignalKind.componentName, page.name))
        by_page[page.id].extend(_text_evidences(page))
        by_page[page.id].extend(_prop_evidences(page))

    for edge in graph.edges:
        # 只认与页面直接相连的节点；不做传递闭包
        if edge.from_ in page_ids:
            by_page[edge.from_].extend(_neighbor_evidences(edge.to, nodes, api_urls_by_node))
        if edge.to in page_ids:
            by_page[edge.to].extend(_neighbor_evidences(edge.from_, nodes, api_urls_by_node))

    return by_page


def _neighbor_evidences(
    node_id: str,
    nodes: dict[str, IRNode],
    api_urls_by_node: dict[str, list[str]],
) -> list[_Evidence]:
    node = nodes.get(node_id)
    if node is None:
        return []
    if node.kind is NodeKind.route:
        return [_Evidence(SignalKind.routePath, node.name)]
    # 页面 calls 的工具函数背后可能还有接口：页面 → fn → api，只多走这一跳
    return [_Evidence(SignalKind.apiUrl, url) for url in api_urls_by_node.get(node_id, [])]


def _text_evidences(page: IRNode) -> list[_Evidence]:
    ui_text = page.attrs.get("uiText")
    if not isinstance(ui_text, dict):
        return []
    evidences: list[_Evidence] = []
    for entry in ui_text.get("texts") or []:
        if isinstance(entry, dict) and isinstance(entry.get("text"), str):
            evidences.append(_Evidence(SignalKind.uiText, entry["text"]))
    return evidences


def _prop_evidences(page: IRNode) -> list[_Evidence]:
    props = page.attrs.get("props")
    if not isinstance(props, dict):
        return []
    evidences: list[_Evidence] = []
    for field_entry in props.get("fields") or []:
        if not isinstance(field_entry, dict):
            continue
        name = field_entry.get("name")
        if isinstance(name, str):
            evidences.append(_Evidence(SignalKind.propName, name))
        description = field_entry.get("description")
        if isinstance(description, str) and description.strip():
            evidences.append(_Evidence(SignalKind.propDescription, description))
    return evidences


def _score_page(
    page: IRNode,
    evidences: list[_Evidence],
    terms: RequirementTerms,
    route_id: str | None,
) -> CandidateDetail | None:
    """给单个页面打分；一个信号都没命中时返回 None（不进候选）。"""
    signals: list[Signal] = []
    repeats: dict[tuple[SignalKind, str], int] = defaultdict(int)

    for evidence in evidences:
        hits = _match_terms(evidence.text, terms.terms)
        if not hits:
            continue
        key = (evidence.kind, normalize(evidence.text))
        repeat = repeats[key]
        repeats[key] += 1
        score = SIGNAL_WEIGHTS[evidence.kind] * sum(term.weight for term in hits)
        score *= _REPEAT_DECAY**repeat
        signals.append(Signal(kind=evidence.kind, value=evidence.text, score=round(score, 4)))

    if not signals:
        return None

    # 信号按类型保留，每类内按分数截断。若单纯取总分前 N 条，routePath / apiUrl
    # 这类只有一两条、但最能解释「为什么是这个页面」的信号，会被一堆文案挤掉。
    kept = _top_signals_per_kind(signals)

    return CandidateDetail(
        candidate=Candidate(
            nodeId=page.id,
            routeId=route_id,
            name=page.name,
            file=page.file,
            score=round(sum(signal.score for signal in signals), 4),
            signals=kept,
        ),
        ui_texts=_unique_preserving_order(
            item.text for item in evidences if item.kind is SignalKind.uiText
        )[:MAX_ITEMS_PER_CANDIDATE],
        prop_names=_unique_preserving_order(
            item.text for item in evidences if item.kind is SignalKind.propName
        )[:MAX_ITEMS_PER_CANDIDATE],
        api_urls=sorted({item.text for item in evidences if item.kind is SignalKind.apiUrl}),
    )


def _top_signals_per_kind(signals: list[Signal]) -> list[Signal]:
    """按信号类型各自保留前几条，再按分数排序。

    单纯取总分前 N 条会让 routePath / apiUrl 这类只有一两条、却最能解释
    「为什么是这个页面」的信号，被一堆文案挤掉。
    """
    by_kind: dict[SignalKind, list[Signal]] = defaultdict(list)
    for signal in signals:
        by_kind[signal.kind].append(signal)

    kept: list[Signal] = []
    for group in by_kind.values():
        group.sort(key=lambda item: (-item.score, item.value))
        kept.extend(group[:MAX_SIGNALS_PER_KIND])
    kept.sort(key=lambda item: (-item.score, item.value, item.kind.value))
    return kept[:MAX_ITEMS_PER_CANDIDATE]


def _unique_preserving_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _match_terms(text: str, terms: tuple[SearchTerm, ...]) -> list[SearchTerm]:
    """返回该段原文命中的检索词。

    中文按子串匹配；英文按分词整词匹配，避免 `order` 命中 `border`。
    标识符额外按分词再试一遍，让 `user` 能命中 `users` / `userId`。
    """
    if not text.strip():
        return []
    hits = [term for term in terms if contains_cjk(text, term.surface)]
    if hits:
        return hits
    words = set(ascii_words(text))
    return [term for term in terms if term.surface in words]
