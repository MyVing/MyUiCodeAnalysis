"""确定性召回：从图里粗筛出「页面候选」，每条候选都带可解释的命中信号。

核心判断：准确率的地板由这一步决定，模型只负责在候选里挑。因此这里的每一分
都必须能说出「哪个检索词命中了哪段原文」，不允许出现无法解释的分数。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from app.contracts import (
    Candidate,
    EdgeKind,
    Evidence,
    IRGraph,
    IRNode,
    Location,
    NodeKind,
    RequirementWarning,
    Signal,
    SignalKind,
    Source,
)

# RequirementWarning 的 severity 用 requirement 契约自己的枚举：IR 契约里也有同名
# 的 Severity，但那是另一个类型，混用会让等值比较莫名其妙地失败。
from app.contracts.requirement import Severity
from app.requirement.lexicon import RequirementTerms, SearchTerm
from app.requirement.text import ascii_words, contains_cjk, normalize

# 各信号的权重。排序依据：越贴近「这个页面在做什么」的信号权重越高。
#   route path / 组件名 —— 页面身份的声明式来源
#   路由标题            —— 菜单名 / handle.title，同为身份来源；但可能过时或与
#                          实际内容漂移（改了路由没改标题），比路径低半档
#   文案 / API url    —— 页面行为的直接证据
#   props 描述        —— 作者写的注释，最接近需求原文
#   props 字段名      —— 页面能力的间接证据
SIGNAL_WEIGHTS: dict[SignalKind, float] = {
    SignalKind.routePath: 1.0,
    SignalKind.routeTitle: 0.9,
    SignalKind.componentName: 1.0,
    SignalKind.uiText: 0.9,
    SignalKind.apiUrl: 0.8,
    SignalKind.propDescription: 0.7,
    SignalKind.propName: 0.6,
}

# 同一信号在同一页面里重复命中时逐次折扣，避免「同一批文案反复出现用户」
# 这种偶然重复压倒真正相关的信号。
_REPEAT_DECAY = 0.6

# 布局（框架）组件文案的权重折扣。
#
# 判据是「页面是否被多个路由复用」：`FullLayout` 同时挂在 /、/posts、/products
# 下，它渲染的 Header 写在页面源码之外，却出现在每一个页面上。用户说「页面上
# 的 GitHub 链接」时，证据只存在于 layout 的子组件里，不收就永远检索不到。
#
# 但这类文案不区分页面——所有路由下都有。因此它只用来「找得到」，不能像页面
# 自有文案那样「定得准」，按半权计入。
_LAYOUT_CHILD_TEXT_SCALE = 0.5

DEFAULT_TOP_K = 30
# 每个候选最多保留多少条信号 / 文案 / props，防止单候选撑爆模型上下文
MAX_ITEMS_PER_CANDIDATE = 10
# 每类信号最多保留几条：routePath / apiUrl 这类低频高价值的信号不被文案挤掉
MAX_SIGNALS_PER_KIND = 3


@dataclass(frozen=True)
class RecallEvidence:
    """可定位的召回原文及其 IR 来源。"""

    text: str
    kind: SignalKind
    node_id: str
    file: str | None
    loc: Location | None
    source: Source
    confidence: float
    # 权重折扣。布局组件的文案全站可见，能证明「存在」但不能证明「属于哪个页面」，
    # 因此按半权参与打分（见 `_LAYOUT_CHILD_TEXT_SCALE`）。
    scale: float = 1.0


@dataclass
class CandidateDetail:
    """候选的完整上下文：契约字段 + 下发给模型的结构化摘要。

    quote_corpus 是该页面全部可命中原文的并集（不截断）：判定阶段校验模型
    引文时逐字比对，语料缺一段就可能冤枉一条真实的匹配。
    """

    candidate: Candidate
    ui_texts: list[str] = field(default_factory=list)
    prop_names: list[str] = field(default_factory=list)
    prop_descriptions: list[str] = field(default_factory=list)
    api_urls: list[str] = field(default_factory=list)
    quote_corpus: list[str] = field(default_factory=list)
    evidence: list[RecallEvidence] = field(default_factory=list)


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
    pages, route_by_page, layout_ids = page_nodes(graph)
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

    evidences = collect_page_evidences(graph, pages, layout_ids)
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


def page_nodes(graph: IRGraph) -> tuple[list[IRNode], dict[str, str], set[str]]:
    """确定页面候选集合、页面 → route 节点的映射，以及其中的布局组件。

    有 route 节点时，页面就是 route 渲染的目标；没有时降级为顶层组件
    （不被任何组件 renders 的组件）。降级必须显式告警——它改变了候选的含义。

    布局组件：被**多条不同路径**的 route 渲染的页面组件（`FullLayout` 同时挂在
    `/`、`/posts` 下）。它们在图上和普通页面一样是 route 的目标，但语义上是
    「每个页面共用的外壳」，其子组件文案需要特殊归属规则（见 `collect_page_evidences`）。
    只被一条 route 渲染的组件按普通页面处理，行为不变。
    """
    nodes = {node.id: node for node in graph.nodes}
    route_by_page: dict[str, str] = {}
    paths_by_page: dict[str, set[str]] = defaultdict(set)

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
        # 用 route 节点名（即路径）判断是否被复用；同名路径的重复声明不算多条
        paths_by_page[target.id].add(source.name)

    if route_by_page:
        pages = sorted((nodes[node_id] for node_id in route_by_page), key=lambda node: node.id)
        layout_ids = {page_id for page_id, paths in paths_by_page.items() if len(paths) > 1}
        return pages, route_by_page, layout_ids

    rendered = {edge.to for edge in graph.edges if edge.kind is EdgeKind.renders}
    component_ids = {
        node.id
        for node in graph.nodes
        if _is_component(node) and node.attrs.get("external") is not True
    }
    return [nodes[node_id] for node_id in sorted(component_ids - rendered)], {}, set()


def _is_component(node: IRNode) -> bool:
    return node.kind in (NodeKind.component, NodeKind.server_component)


def collect_page_evidences(
    graph: IRGraph,
    pages: list[IRNode],
    layout_ids: set[str] | None = None,
) -> dict[str, list[RecallEvidence]]:
    """为每个页面收集可命中的原文。

    归属规则保守：只收页面自己在源码里写下的内容，以及它**直接**连接到的实体。
    刻意不递归收集子组件的文案——否则所有页面都会因共享同一批基础组件而拿到
    相同文案，候选之间失去区分度。

    例外是布局组件（`layout_ids`）：它被多条路由复用，渲染的子组件（导航栏、
    页脚）虽然在源码上属于子组件，却实实在在地出现在该页面上。用户描述「页面
    右上角的 GitHub 链接」时，证据只可能存在于那里。因此对布局页面额外收一层
    直接子组件的 uiText，并按 `_LAYOUT_CHILD_TEXT_SCALE` 折半计权——它证明
    「页面上有这个东西」，但不能像页面自有文案那样区分页面。
    """
    nodes = {node.id: node for node in graph.nodes}
    by_page: dict[str, list[RecallEvidence]] = {page.id: [] for page in pages}
    page_ids = set(by_page)
    layout_ids = layout_ids or set()

    # 先算一遍 url 可达性：api 节点通常挂在工具函数上，而不是直接连页面
    api_urls_by_node: dict[str, list[IRNode]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind is not EdgeKind.requests:
            continue
        api = nodes.get(edge.to)
        if api is not None and api.kind is NodeKind.api:
            url = api.attrs.get("url")
            if isinstance(url, str):
                api_urls_by_node[edge.from_].append(api)

    for page in pages:
        by_page[page.id].append(_node_evidence(page, SignalKind.componentName, page.name))
        by_page[page.id].extend(_text_evidences(page))
        by_page[page.id].extend(_prop_evidences(page))
        if page.id in layout_ids:
            by_page[page.id].extend(_layout_child_text_evidences(page, graph, nodes))

    for edge in graph.edges:
        # 只认与页面直接相连的节点；不做传递闭包
        if edge.from_ in page_ids:
            by_page[edge.from_].extend(_neighbor_evidences(edge.to, nodes, api_urls_by_node))
        if edge.to in page_ids:
            by_page[edge.to].extend(_neighbor_evidences(edge.from_, nodes, api_urls_by_node))

    return by_page


def _layout_child_text_evidences(
    page: IRNode,
    graph: IRGraph,
    nodes: dict[str, IRNode],
) -> list[RecallEvidence]:
    """布局页面直接渲染的子组件上的可见文案，按半权计。

    只走一层：`FullLayout → Header` 收，`FullLayout → Header → 某按钮` 不收。
    再往下就是各页面自己的内容了，收进来等于取消「不继承子组件文案」这条约束。
    """
    evidences: list[RecallEvidence] = []
    for edge in graph.edges:
        if edge.kind is not EdgeKind.renders or edge.from_ != page.id:
            continue
        child = nodes.get(edge.to)
        if child is None or not _is_component(child) or child.id == page.id:
            continue
        for evidence in _text_evidences(child):
            evidences.append(replace(evidence, scale=_LAYOUT_CHILD_TEXT_SCALE))
    return evidences


def _neighbor_evidences(
    node_id: str,
    nodes: dict[str, IRNode],
    api_urls_by_node: dict[str, list[IRNode]],
) -> list[RecallEvidence]:
    node = nodes.get(node_id)
    if node is None:
        return []
    if node.kind is NodeKind.route:
        evidences = [_node_evidence(node, SignalKind.routePath, node.name)]
        # 路由 / 菜单标题：需求写「订单管理页」时，路径对不上但标题对得上。
        # 标题由 ast-worker 只收字面量得来，这里不再做任何推断。
        title = node.attrs.get("title")
        if isinstance(title, str) and title.strip():
            evidences.append(_node_evidence(node, SignalKind.routeTitle, title))
        return evidences
    # 页面 calls 的工具函数背后可能还有接口：页面 → fn → api，只多走这一跳
    return [
        _node_evidence(api, SignalKind.apiUrl, api.attrs["url"])
        for api in api_urls_by_node.get(node_id, [])
        if isinstance(api.attrs.get("url"), str)
    ]


def _evidence_rank(item: Evidence) -> tuple[float, str, str, int, int]:
    """证据排序键：置信度高者优先，其余字段保证同一节点内结果稳定可复现。"""
    return (
        -item.confidence,
        item.source.value,
        item.file or "",
        item.loc.line if item.loc else 0,
        item.loc.col if item.loc else 0,
    )


def _node_evidence(
    node: IRNode,
    kind: SignalKind,
    text: str,
    *,
    loc: Location | None = None,
) -> RecallEvidence:
    source_evidence = sorted(node.evidence, key=_evidence_rank)[0]
    return RecallEvidence(
        kind=kind,
        text=text,
        node_id=node.id,
        file=node.file or source_evidence.file,
        loc=loc or node.loc or source_evidence.loc,
        source=source_evidence.source,
        confidence=source_evidence.confidence,
    )


def _text_evidences(page: IRNode) -> list[RecallEvidence]:
    ui_text = page.attrs.get("uiText")
    if not isinstance(ui_text, dict):
        return []
    evidences: list[RecallEvidence] = []
    for entry in ui_text.get("texts") or []:
        if isinstance(entry, dict) and isinstance(entry.get("text"), str):
            loc = _location(entry.get("loc")) or page.loc
            evidences.append(_node_evidence(page, SignalKind.uiText, entry["text"], loc=loc))
    return evidences


def _prop_evidences(page: IRNode) -> list[RecallEvidence]:
    props = page.attrs.get("props")
    if not isinstance(props, dict):
        return []
    evidences: list[RecallEvidence] = []
    for field_entry in props.get("fields") or []:
        if not isinstance(field_entry, dict):
            continue
        loc = _location(field_entry.get("loc")) or page.loc
        name = field_entry.get("name")
        if isinstance(name, str):
            evidences.append(_node_evidence(page, SignalKind.propName, name, loc=loc))
        description = field_entry.get("description")
        if isinstance(description, str) and description.strip():
            evidences.append(_node_evidence(page, SignalKind.propDescription, description, loc=loc))
    return evidences


def _location(value: object) -> Location | None:
    if not isinstance(value, dict):
        return None
    line, col = value.get("line"), value.get("col")
    if not isinstance(line, int) or isinstance(line, bool):
        return None
    if not isinstance(col, int) or isinstance(col, bool):
        return None
    return Location(line=line, col=col)


def _score_page(
    page: IRNode,
    evidences: list[RecallEvidence],
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
        # 布局子组件文案折半：能召回，但不该压过页面自己的文案
        score *= evidence.scale
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
        prop_descriptions=_unique_preserving_order(
            item.text for item in evidences if item.kind is SignalKind.propDescription
        )[:MAX_ITEMS_PER_CANDIDATE],
        api_urls=sorted({item.text for item in evidences if item.kind is SignalKind.apiUrl}),
        quote_corpus=_unique_preserving_order(item.text for item in evidences),
        # 元数据是判定阶段生成 verifiedEvidence 的唯一依据，原样保留不截断
        evidence=list(evidences),
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
