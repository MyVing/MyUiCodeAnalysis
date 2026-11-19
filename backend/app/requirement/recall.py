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
    IREdge,
    IRGraph,
    IRNode,
    Location,
    NodeKind,
    RequirementWarning,
    RuntimeTrace,
    Signal,
    SignalKind,
    Source,
)

# RequirementWarning 的 severity 用 requirement 契约自己的枚举：IR 契约里也有同名
# 的 Severity，但那是另一个类型，混用会让等值比较莫名其妙地失败。
from app.contracts.requirement import Severity
from app.ir.api_usage import ApiUsageHit, ApiUsageIndex, apply_runtime_backing
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
    trace: RuntimeTrace | None = None,
) -> RecallResult:
    """从图里召回页面候选，按得分降序，截断到 top_k。

    两段式（6.11 通用化）：先在全图上找「种子命中」（路由 / 组件名 / 文案 /
    接口 / hook / 函数名），每个命中点沿图边反向归属到页面——词法只负责
    「发现」，归属完全由图结构决定；没有任何种子命中时回退到页面级词法
    评分（`collect_page_evidences` + `_score_page`），并显式告警。`trace`
    是运行时采集产物（可为 None）：接口种子的命中页若同时出现在 trace 的
    请求与页面 URL 里，置信度抬到运行时档（0.95）。
    """
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

    seed_hits = _attribute_seeds(
        graph, terms, frozenset(page.id for page in pages), trace
    )
    if not seed_hits:
        warnings.append(
            RequirementWarning(
                code="fallback_lexical",
                severity=Severity.info,
                message=(
                    "没有图内种子命中（路由 / 组件名 / 文案 / 接口 / 函数名），"
                    "回退到页面级词法评分。"
                ),
            )
        )
        return _lexical_recall(
            graph, terms, pages, route_by_page, layout_ids, top_k, warnings
        )

    scored = _candidates_from_seed_hits(graph, pages, route_by_page, layout_ids, seed_hits)
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


def _lexical_recall(
    graph: IRGraph,
    terms: RequirementTerms,
    pages: list[IRNode],
    route_by_page: dict[str, str],
    layout_ids: set[str],
    top_k: int,
    warnings: list[RequirementWarning],
) -> RecallResult:
    """降级路径：页面级词法评分（无任何种子命中时兜底，保证不比旧实现差）。"""
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


# 种子系数（6.11）：命中种类决定可信度。接口是需求文档里最无歧义的信息，
# 但词法命中只能确认「URL 含这个词」（不是 method+path 全对上的锚点直连），
# 按 ApiUsageIndex 的后缀档 0.85 计；路由路径是页面身份的声明式来源，计 1.0。
API_SEED_COEFFICIENT = 0.85
SEED_COEFFICIENTS: dict[SignalKind, float] = {
    SignalKind.routePath: 1.0,
    SignalKind.routeTitle: 0.9,
    SignalKind.uiText: 0.7,
    SignalKind.componentName: 0.6,
    SignalKind.handlerName: 0.5,
    SignalKind.apiUrl: API_SEED_COEFFICIENT,
}


@dataclass(frozen=True)
class _SeedHit:
    """一条「种子命中 → 反向归属到页面」的结论：路径上每一环都可回图核对。"""

    page_node_id: str
    route_node_id: str | None
    kind: SignalKind
    text: str
    # 命中该种子的检索词权重之和（与 _score_page 同口径）
    term_weight: float
    # min(路径上各边置信度) × 种子系数；运行时双证抬到 0.95
    confidence: float
    # 完整归属路径：种子 → … → 页面（→ 路由）
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    runtime_backed: bool = False


def collect_seeds(graph: IRGraph) -> list[tuple[str, SignalKind, str]]:
    """全图收集可被检索词命中的种子文本，按 (node, kind, text) 去重。

    词法在这一步只负责「发现」——命中哪里算哪里，不负责归属；命中点随后沿
    图边反向归属到页面。种子覆盖五类声明式信息：路由路径 / 标题、组件名、
    界面文案（组件 uiText 与元素 viewText）、接口 URL、hook / 函数名。
    """
    seeds: list[tuple[str, SignalKind, str]] = []
    seen: set[tuple[str, SignalKind, str]] = set()

    def add(node_id: str, kind: SignalKind, text: str) -> None:
        if not text.strip():
            return
        key = (node_id, kind, text)
        if key not in seen:
            seen.add(key)
            seeds.append(key)

    for node in graph.nodes:
        if node.kind is NodeKind.route:
            add(node.id, SignalKind.routePath, node.name)
            title = node.attrs.get("title")
            if isinstance(title, str):
                add(node.id, SignalKind.routeTitle, title)
        elif node.kind in (NodeKind.component, NodeKind.server_component):
            if node.attrs.get("external") is True:
                continue
            add(node.id, SignalKind.componentName, node.name)
            ui_text = node.attrs.get("uiText")
            if isinstance(ui_text, dict):
                for entry in ui_text.get("texts") or []:
                    if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                        add(node.id, SignalKind.uiText, entry["text"])
        elif node.kind is NodeKind.element:
            view_text = node.attrs.get("viewText")
            if isinstance(view_text, str):
                add(node.id, SignalKind.uiText, view_text)
        elif node.kind is NodeKind.api:
            url = node.attrs.get("url") or node.attrs.get("path")
            if isinstance(url, str):
                add(node.id, SignalKind.apiUrl, url)
        elif node.kind in (NodeKind.hook, NodeKind.fn):
            add(node.id, SignalKind.handlerName, node.name)
    return seeds


def _attribute_seeds(
    graph: IRGraph,
    terms: RequirementTerms,
    page_ids: frozenset[str],
    trace: RuntimeTrace | None,
) -> list[_SeedHit]:
    """种子命中 → 反向归属到页面（→路由）。

    api 种子与锚点直连共用 ApiUsageIndex 的归一化、遍历与运行时双证；其余
    种子走同一实例的通用反向遍历（同一套边预算与置信度规则）。路由种子的
    页面就是该路由的渲染目标（正向一跳），不走反向遍历——route 节点的反向
    入边到不了它自己的页面。
    """
    index = ApiUsageIndex(graph)
    nodes_by_id = {node.id: node for node in graph.nodes}
    pages_by_route = route_target_pages(graph, nodes_by_id)
    shared = _shared_component_ids(graph, nodes_by_id)
    hits: list[_SeedHit] = []

    for (node_id, kind, text), term_weight in seeds_with_terms(collect_seeds(graph), terms):
        coefficient = SEED_COEFFICIENTS[kind]
        if kind is SignalKind.apiUrl:
            api_node = nodes_by_id[node_id]
            url = api_node.attrs.get("url") or api_node.attrs.get("path")
            for walk in index.walk_from(
                node_id, coefficient, page_ids=page_ids, suffix_match=True
            ):
                hits.append(
                    _api_seed_hit(
                        walk, kind, text, term_weight, api_node, url, trace, index
                    )
                )
        elif kind in (SignalKind.routePath, SignalKind.routeTitle):
            for page_id in pages_by_route.get(node_id, []):
                edge = _route_page_edge(graph, node_id, page_id)
                confidence = round(coefficient * max(
                    (item.confidence for item in edge.evidence), default=0.0
                ) if edge is not None else coefficient, 4)
                hits.append(
                    _SeedHit(
                        page_node_id=page_id,
                        route_node_id=node_id,
                        kind=kind,
                        text=text,
                        term_weight=term_weight,
                        confidence=confidence,
                        node_ids=(page_id, node_id),
                        edge_ids=(edge.id,) if edge is not None else (),
                    )
                )
        else:
            for walk in index.walk_from(node_id, coefficient, page_ids=page_ids):
                # 布局复用衰减：路径经过被多条路由共享的外壳组件时折半
                confidence = walk.confidence
                if any(node_id_item in shared for node_id_item in walk.path):
                    confidence = round(confidence * LAYOUT_REUSE_DECAY, 4)
                hits.append(
                    _SeedHit(
                        page_node_id=walk.page_node_id,
                        route_node_id=walk.route_node_id,
                        kind=kind,
                        text=text,
                        term_weight=term_weight,
                        confidence=confidence,
                        node_ids=walk.path,
                        edge_ids=walk.edge_path,
                    )
                )
    return _dedupe_hits(hits)


def _dedupe_hits(hits: list[_SeedHit]) -> list[_SeedHit]:
    """同一种子在同一个页面上的多条等价归属只保留置信度最高的一条。

    共享布局（route_home → FullLayout ← route_posts）会让 Header 的文案经
    两条路由各归属一次到同一个页面——那是同一次命中，不是两次证据；重复
    折扣只该作用于真正不同的种子出现（比如两个不同子组件都有这段文案）。
    """
    best: dict[tuple[str, SignalKind, str, str], _SeedHit] = {}
    order: list[tuple[str, SignalKind, str, str]] = []
    for hit in hits:
        key = (
            hit.page_node_id,
            hit.kind,
            normalize(hit.text),
            hit.node_ids[0] if hit.node_ids else "",
        )
        current = best.get(key)
        if current is None:
            best[key] = hit
            order.append(key)
        elif hit.confidence > current.confidence:
            best[key] = hit
    return [best[key] for key in order]


def seeds_with_terms(
    seeds: list[tuple[str, SignalKind, str]], terms: RequirementTerms
) -> list[tuple[tuple[str, SignalKind, str], float]]:
    """过滤出被检索词命中的种子，并带上权重之和（保持发现顺序）。"""
    matched: list[tuple[tuple[str, SignalKind, str], float]] = []
    for seed in seeds:
        term_hits = _match_terms(seed[2], terms.terms)
        if term_hits:
            matched.append((seed, sum(term.weight for term in term_hits)))
    return matched


def _api_seed_hit(
    walk: ApiUsageHit,
    kind: SignalKind,
    text: str,
    term_weight: float,
    api_node: IRNode,
    url: object,
    trace: RuntimeTrace | None,
    index: ApiUsageIndex,
) -> _SeedHit:
    """api 种子命中，附带运行时双证强化（trace 请求 + 页面 URL 同时命中）。"""
    confidence = walk.confidence
    runtime_backed = False
    if trace is not None and isinstance(url, str):
        method = api_node.attrs.get("method")
        route_paths = {walk.page_node_id: index.route_path_of(walk.route_node_id)}
        backed = apply_runtime_backing(
            (walk,),
            route_paths,
            method=method if isinstance(method, str) else None,
            url=url,
            trace=trace,
        )
        runtime_backed = backed[0].runtime_backed
        confidence = backed[0].confidence
    return _SeedHit(
        page_node_id=walk.page_node_id,
        route_node_id=walk.route_node_id,
        kind=kind,
        text=text,
        term_weight=term_weight,
        confidence=confidence,
        node_ids=walk.path,
        edge_ids=walk.edge_path,
        runtime_backed=runtime_backed,
    )


# 布局复用衰减：归属路径经过被 ≥2 条路由渲染的组件（全站外壳）时，
# 该命中折半计权——能召回，但不能压过页面自有文案。
LAYOUT_REUSE_DECAY = 0.5


def _shared_component_ids(
    graph: IRGraph, nodes_by_id: dict[str, IRNode]
) -> frozenset[str]:
    """被 ≥2 条路由渲染的组件（layout 语义）：它们身上的命中对每页等价。"""
    counts: dict[str, int] = defaultdict(int)
    for edge in graph.edges:
        if edge.kind is not EdgeKind.renders:
            continue
        source = nodes_by_id.get(edge.from_)
        if source is not None and source.kind is NodeKind.route:
            counts[edge.to] += 1
    return frozenset(node_id for node_id, count in counts.items() if count >= 2)


def _route_page_edge(graph: IRGraph, route_id: str, page_id: str) -> IREdge | None:
    return next(
        (
            edge
            for edge in graph.edges
            if edge.kind is EdgeKind.renders and edge.from_ == route_id and edge.to == page_id
        ),
        None,
    )


def _candidates_from_seed_hits(
    graph: IRGraph,
    pages: list[IRNode],
    route_by_page: dict[str, str],
    layout_ids: set[str],
    seed_hits: list[_SeedHit],
) -> list[CandidateDetail]:
    """按页面聚合种子命中，产出候选。

    页面得分 = 该页全部信号得分之和（契约语义：signals 之和即总分）；
    同一种子文本在同一页面重复命中按 _REPEAT_DECAY 逐次折扣。quote_corpus
    追加种子文本：判定阶段的引文校验必须能查到这些「归属依据」。
    """
    evidences = collect_page_evidences(graph, pages, layout_ids)
    by_page: dict[str, list[_SeedHit]] = defaultdict(list)
    for hit in seed_hits:
        by_page[hit.page_node_id].append(hit)

    details: list[CandidateDetail] = []
    for page in pages:
        page_hits = by_page.get(page.id)
        if not page_hits:
            continue
        base = _page_detail(page, evidences.get(page.id, []))
        signals: list[Signal] = []
        repeats: dict[tuple[SignalKind, str], int] = defaultdict(int)
        best = page_hits[0]
        for hit in page_hits:
            if hit.confidence > best.confidence:
                best = hit
            key = (hit.kind, normalize(hit.text))
            repeat = repeats[key]
            repeats[key] += 1
            score = hit.confidence * hit.term_weight * _REPEAT_DECAY**repeat
            signals.append(Signal(kind=hit.kind, value=hit.text, score=round(score, 4)))
        kept = _top_signals_per_kind(signals)
        candidate = Candidate(
            nodeId=page.id,
            routeId=best.route_node_id or route_by_page.get(page.id),
            name=page.name,
            file=page.file,
            score=round(sum(signal.score for signal in signals), 4),
            signals=kept,
            anchorNodeIds=list(best.node_ids) if len(best.node_ids) > 1 else None,
            anchorEdgeIds=list(best.edge_ids) if best.edge_ids else None,
        )
        seed_texts = {hit.text for hit in page_hits} - set(base.quote_corpus)
        detail = CandidateDetail(
            candidate=candidate,
            ui_texts=base.ui_texts,
            prop_names=base.prop_names,
            prop_descriptions=base.prop_descriptions,
            api_urls=base.api_urls,
            quote_corpus=_unique_preserving_order([*base.quote_corpus, *seed_texts]),
            evidence=base.evidence,
        )
        details.append(detail)
    return details


def _page_detail(page: IRNode, evidences: list[RecallEvidence]) -> CandidateDetail:
    """从页面证据构造候选的语料部分（不含得分——得分来自种子命中聚合）。"""
    return CandidateDetail(
        candidate=Candidate(
            nodeId=page.id,
            name=page.name,
            file=page.file,
            score=0.0,
            signals=[],
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
        evidence=list(evidences),
    )


def route_target_pages(
    graph: IRGraph, nodes_by_id: dict[str, IRNode]
) -> dict[str, list[str]]:
    """route → 渲染的页面组件（布局复用时一个路由可挂多个页面）。

    ast-worker 已经按 `<Outlet>` / 嵌套路由层级 / index 规则区分过 layout 与
    page（`attrs.pageComponentId` / `attrs.layoutComponentIds`），这里的顺序
    以显式标注为准——同一路径下 layout 先声明时，按边遍历顺序取会把布局组件
    当成页面。attrs 缺省（旧图）时退回全部渲染目标，行为不变。
    """
    by_route: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind is not EdgeKind.renders:
            continue
        source = nodes_by_id.get(edge.from_)
        target = nodes_by_id.get(edge.to)
        if (
            source is not None
            and source.kind is NodeKind.route
            and target is not None
            and target.kind in (NodeKind.component, NodeKind.server_component)
        ):
            by_route[source.id].append(target.id)

    for node in graph.nodes:
        if node.kind is not NodeKind.route:
            continue
        declared_layout = node.attrs.get("layoutComponentIds")
        layout_ids = (
            {item for item in declared_layout if isinstance(item, str)}
            if isinstance(declared_layout, list)
            else set()
        )
        declared_page = node.attrs.get("pageComponentId")
        if not layout_ids and not isinstance(declared_page, str):
            continue  # 旧图没有区分信息，保持原有顺序
        targets = by_route.get(node.id, [])
        if isinstance(declared_page, str):
            pages = [declared_page] + [
                item
                for item in targets
                if item != declared_page and item not in layout_ids
            ]
        else:
            pages = [item for item in targets if item not in layout_ids]
        if pages:
            by_route[node.id] = pages
        elif targets:
            # 全被标成 layout 的极端情况：保留原目标，不给空列表
            by_route[node.id] = targets
    return dict(by_route)


def page_nodes(graph: IRGraph) -> tuple[list[IRNode], dict[str, str], set[str]]:
    """确定页面候选集合、页面 → route 节点的映射，以及其中的布局组件。

    有 route 节点时，页面就是 route 渲染的目标；没有时降级为顶层组件
    （不被任何组件 renders 的组件）。降级必须显式告警——它改变了候选的含义。

    布局组件有两个来源：

    1. ast-worker 的显式标注（`route.attrs.layoutComponentIds`）：同一 path 下
       layout + page 混挂（父路由出 layout、index 出内容）时，ast-worker 已经按
       `<Outlet>` / 嵌套路由层级 / index 规则分好类，Next 则按 layout.tsx 目录
       约定归属。这是首选来源，不靠启发式猜。
    2. 被多条不同路径的 route 渲染（`FullLayout` 同时挂在 /、/posts 下）——
       旧图 / 未标注时的兜底启发式。

    它们在图上和普通页面一样是 route 的目标，但语义上是「每个页面共用的外壳」，
    其子组件文案需要特殊归属规则（见 `collect_page_evidences`）。
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
        layout_ids = _explicit_layout_ids(graph) | {
            page_id for page_id, paths in paths_by_page.items() if len(paths) > 1
        }
        return pages, route_by_page, layout_ids

    rendered = {edge.to for edge in graph.edges if edge.kind is EdgeKind.renders}
    component_ids = {
        node.id
        for node in graph.nodes
        if _is_component(node) and node.attrs.get("external") is not True
    }
    return [nodes[node_id] for node_id in sorted(component_ids - rendered)], {}, set()


def _explicit_layout_ids(graph: IRGraph) -> set[str]:
    """ast-worker 在 route.attrs.layoutComponentIds 里显式声明的布局组件。"""
    layout_ids: set[str] = set()
    for node in graph.nodes:
        if node.kind is not NodeKind.route:
            continue
        declared = node.attrs.get("layoutComponentIds")
        if isinstance(declared, list):
            layout_ids.update(item for item in declared if isinstance(item, str))
    return layout_ids


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
