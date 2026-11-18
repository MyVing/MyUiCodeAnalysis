"""锚点抽取与直连（技术设计 6.13 第二、三步）：先结构化，再语义。

需求文档里的路由、接口、组件名、字段名是最可靠的定位信息，不该被当成普通
中文词交给模型猜。本模块在拆解之后、召回之前做两件事：

1. **锚点抽取**（零模型、纯规则）：从功能点文本里按确定性规则抽出结构化锚点，
   每个锚点记录 kind / raw / normalized / 字符偏移。抽取宁缺毋滥——把文件路径
   误当路由、把普通英文单词误当字段，都会制造高置信度误匹配，因此：
   - 接口锚点要求显式 method（`GET /api/users`），裸 URL 不认；
   - 路由锚点只认引号 / 反引号包裹的 `/xxx`，且排除文件扩展名结尾；
   - 组件锚点只认 `<X />` 形态与引号内的 PascalCase；
   - 字段锚点只认引号内的 camelCase / snake_case，单个小写词（`id`）不认；
   - 界面词锚点认其余引号内容（至少两字），逐字命中链路元素的文案 / 列名才落。
2. **锚点直连**：精确命中图上的节点即产出 `Match`（source=static），
   置信度按 6.13 / 6.15 分档：精确 0.95、URL 后缀 0.85、精确界面词 0.90。
   界面词唯一命中某链路元素时落到**元素级**（`Match.chainId` + `targets` 指向
   「哪个按钮 / 哪一列」，差距表 #7）；同页多元素命中只落页面级，跨页命中
   记 `anchor_ambiguous`。找不到对应节点时记 `anchor_unresolved` 降级为普通
   文本，不硬凑。

直连结果进产物后，**被锚点覆盖的功能点不再进模型**（第四步语义残差）：模型
只能补充锚点未覆盖的功能点。这样大量带接口 / 页面路径的需求根本不需要模型。

置信度复用 `app.ir.api_usage` 的反向索引：接口锚点沿 `requests`/`calls`/… 反向
走到页面，置信度沿路径取最小值再乘锚点系数——跨节点路径不是语法事实，每多
一跳多一分符号推断成分，「静态恒为 1.0」的说法不适用于这条链路。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.contracts import (
    EdgeKind,
    IRGraph,
    IRNode,
    NodeKind,
    Source,
    VerifiedEvidence,
)
from app.contracts.requirement import Location, Match, RequirementWarning, Severity
from app.ir.api_usage import ApiUsageIndex, normalize_url
from app.requirement.chains import derive_chains
from app.requirement.features import DecomposedFeature
from app.requirement.recall import collect_page_evidences, page_nodes
from app.requirement.subgraph import build_subgraph

# 精确锚点（6.13 第三步）：method + path 都对上，或路由 / 组件 / 字段唯一命中
_ANCHOR_EXACT = 0.95

# 精确界面词（6.15 分档）：引文逐字命中某元素的 viewText / 列名。比字段锚点低半档
# ——界面词的语料面大（同文案常出现在多个页面），唯一命中才落元素级。
_ANCHOR_UITEXT = 0.90

# 锚点可信度依赖「图上的节点确实对得上」，静态证据恒为 1.0 的档位语义在这里
# 不适用：直连是跨节点结论，统一给 0.95 而不是 1.0。

# URL 归一化里占位段统一成 :param；路由锚点与接口锚点共用同一套规则，
# 与 ast-worker 的 normalizeUrlText 保持一致（见 api_usage.py 的说明）。

# 接口锚点：method + 紧随的路径或完整 URL。字符类排除空白、引号与中英文标点，
# 让 `GET /api/users，` 这类行尾标点不被吃进路径。lookbehind 只挡 ASCII 字母
# （「budget」里不含 GET），不挡中文——「调用GET /api/users」这种紧邻写法必须认。
_API_RE = re.compile(
    r"(?<![A-Za-z])(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+"
    r"([^\s`'\"“”「」『』，。；、！？：（）()]+)",
    re.IGNORECASE,
)

# 引号 / 反引号包裹的内容：路由、组件、文件、字段都只从这里认，裸文本不认
_QUOTED_RE = re.compile(r"`([^`\n]+)`|「([^」\n]+)」|“([^”\n]+)”|«([^»\n]+)»")

# JSX 组件：`<UserList ` / `<UserList>` / `<UserList />`
_JSX_RE = re.compile(r"<([A-Z][A-Za-z0-9]*)[\s/>]")

# 路由形态：/ 开头，只含路径常见字符；文件结尾（.tsx 等）排除
_ROUTE_SHAPE_RE = re.compile(r"^/[A-Za-z0-9_\-./:*$\[\]{}]+$")
_FILE_TAIL_RE = re.compile(r"\.[A-Za-z]{1,5}$")

# 文件锚点：以 .ts/.tsx/.js/.jsx 结尾的相对路径
_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:tsx|jsx|ts|js)$")

# 组件锚点：PascalCase 整词
_PASCAL_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")

# 字段锚点：camelCase（至少一个驼峰）或 snake_case（至少一个下划线）
_CAMEL_RE = re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]+)+$")
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


class AnchorKind(StrEnum):
    api = "api"
    route = "route"
    component = "component"
    file = "file"
    field = "field"
    uiText = "uiText"


@dataclass(frozen=True)
class Anchor:
    """一个结构化锚点。charRange 相对所属功能点文本，供证据回跳需求原文。"""

    kind: AnchorKind
    raw: str
    normalized: str
    start: int
    end: int


@dataclass
class AnchorOutcome:
    """直连结果：锚点产出的 matches + 告警 + 已覆盖的功能点 id。"""

    matches: list[Match]
    warnings: list[RequirementWarning]
    covered: frozenset[str]

    @classmethod
    def empty(cls) -> AnchorOutcome:
        return cls(matches=[], warnings=[], covered=frozenset())


def extract_anchors(text: str) -> list[Anchor]:
    """从功能点文本抽出确定性锚点，按 (kind, normalized) 去重、按出现位置排序。"""
    anchors: list[Anchor] = []
    seen: set[tuple[AnchorKind, str]] = set()

    def add(kind: AnchorKind, raw: str, normalized: str, start: int, end: int) -> None:
        key = (kind, normalized)
        if key in seen:
            return
        seen.add(key)
        anchors.append(Anchor(kind=kind, raw=raw, normalized=normalized, start=start, end=end))

    for match in _API_RE.finditer(text):
        method = match.group(1).upper()
        token = match.group(2).rstrip(".,;:")
        # 只认路径（/api/users）或带 origin 的完整 URL（https://x.com/api/users）；
        # `GET users` 这种缺斜杠的写法不是接口引用，不硬凑
        if not token.startswith("/") and "://" not in token:
            continue
        normalized = normalize_url(token)
        if normalized is None:
            continue
        raw = f"{method} {token}"
        add(AnchorKind.api, raw, f"{method} {normalized}", match.start(), match.start() + len(raw))

    for match in _QUOTED_RE.finditer(text):
        content = next((group for group in match.groups() if group is not None), None)
        if content is None:
            continue
        quoted = content.strip()
        start = match.start()
        if quoted.startswith("#"):
            quoted = quoted[1:].lstrip("/")
            quoted = f"/{quoted}" if quoted else ""
        if quoted.startswith("/"):
            # 排除文件名：`/src/pages/UserList.tsx` 是文件路径不是路由
            if _FILE_TAIL_RE.search(quoted) or not _ROUTE_SHAPE_RE.match(quoted):
                continue
            normalized = normalize_url(quoted)
            if normalized is not None:
                add(AnchorKind.route, quoted, normalized, start, match.end())
            continue
        if _FILE_RE.match(quoted):
            normalized = quoted.replace("\\", "/").lstrip("./")
            add(AnchorKind.file, quoted, normalized, start, match.end())
            continue
        if _PASCAL_RE.match(quoted):
            add(AnchorKind.component, quoted, quoted, start, match.end())
            continue
        if _CAMEL_RE.match(quoted) or _SNAKE_RE.match(quoted):
            add(AnchorKind.field, quoted, quoted, start, match.end())
            continue
        # 剩下的引号内容当作界面词锚点（差距表 #7）：需求说「导出」「状态」时，
        # 引号是用户手动标出的原文，比裸中文可靠得多。两类太泛的不认：单字符
        # （「增」「删」能命中几乎所有工具栏），以及两字母纯小写英文词（`id`
        # 这类被字段锚点规则明确拒绝的标识，换个通道收进来等于绕过校验）。
        if len(quoted) >= 2 and not (quoted.isascii() and quoted.isalpha() and len(quoted) == 2):
            add(AnchorKind.uiText, quoted, quoted, start, match.end())

    for match in _JSX_RE.finditer(text):
        name = match.group(1)
        add(AnchorKind.component, name, name, match.start(), match.end())

    anchors.sort(key=lambda anchor: (anchor.start, anchor.end))
    return anchors


def anchor_direct_connect(
    graph: IRGraph, features: Sequence[DecomposedFeature]
) -> AnchorOutcome:
    """把功能点里的锚点直连到图上节点，产出 source=static 的确定性匹配。

    纯函数、零模型：dry-run 与模型不可用时它照常工作——这正是 6.13「先结构化，
    再语义」的意义，锚点结论不依赖任何模型可用性。
    """
    anchors_by_feature = {feature.id: extract_anchors(feature.text) for feature in features}
    if not any(anchors_by_feature.values()):
        return AnchorOutcome.empty()

    index = ApiUsageIndex(graph)
    pages, route_by_page, layout_ids = page_nodes(graph)
    page_ids = {page.id for page in pages}
    nodes_by_id = {node.id: node for node in graph.nodes}

    routes_by_path = _route_index(graph, nodes_by_id)
    pages_by_route = _route_pages(graph, nodes_by_id)
    components_by_name, components_by_file = _component_index(graph)
    field_pages = _field_index(graph, pages, layout_ids)
    # 元素级落点（差距表 #7）：界面词锚点按链路元素的可见文案直连。
    # 链路限定在真页面内——布局兜底归属的元素不在 page_nodes 的页面集合里，
    # 直接引用会让 pageNodeId 落在候选页面集合之外。
    ui_texts = _element_text_index(graph, nodes_by_id, page_ids)

    matches: list[Match] = []
    warnings: list[RequirementWarning] = []

    def emit(feature_id: str, anchor: Anchor, page_id: str, route_id: str | None) -> None:
        matches.append(
            _match(
                graph,
                nodes_by_id,
                feature_id,
                anchor,
                page_id=page_id,
                route_id=route_id,
                confidence=_ANCHOR_EXACT,
            )
        )

    for feature in features:
        for anchor in anchors_by_feature.get(feature.id, []):
            if anchor.kind is AnchorKind.api:
                method, _, path = anchor.normalized.partition(" ")
                # 先精确（method + path 对上，0.95）；精确没有再试后缀（0.85 档，
                # 由 ApiUsageIndex 的锚点系数落到置信度里）
                lookup = index.lookup(method=method, url=path, match="exact")
                if not lookup.pages:
                    lookup = index.lookup(method=method, url=path, match="suffix")
                if not lookup.pages:
                    warnings.append(_unresolved(feature.id, anchor))
                    continue
                warnings.extend(lookup.warnings)
                for hit in lookup.pages:
                    matches.append(
                        _match(
                            graph,
                            nodes_by_id,
                            feature.id,
                            anchor,
                            page_id=hit.page_node_id,
                            route_id=hit.route_node_id,
                            # 置信度由反向索引给出：沿路径最小值 × 锚点系数
                            # （精确 0.95 / 后缀 0.85），不在这一层重复加工
                            confidence=hit.confidence,
                            path_node_ids=hit.path,
                        )
                    )
            elif anchor.kind is AnchorKind.route:
                route_id = routes_by_path.get(anchor.normalized)
                page_ids_of_route = pages_by_route.get(route_id or "", []) if route_id else []
                if not page_ids_of_route:
                    warnings.append(_unresolved(feature.id, anchor))
                    continue
                for page_id in page_ids_of_route:
                    emit(feature.id, anchor, page_id, route_id)
            elif anchor.kind in (AnchorKind.component, AnchorKind.file):
                node_id = (
                    components_by_file.get(anchor.normalized)
                    if anchor.kind is AnchorKind.file
                    else components_by_name.get(anchor.normalized)
                )
                if node_id is None or node_id not in page_ids:
                    warnings.append(_unresolved(feature.id, anchor))
                    continue
                emit(feature.id, anchor, node_id, route_by_page.get(node_id))
            elif anchor.kind is AnchorKind.field:
                hits = field_pages.get(anchor.normalized, set())
                if len(hits) > 1:
                    warnings.append(
                        RequirementWarning(
                            code="anchor_ambiguous",
                            severity=Severity.info,
                            message=(
                                f"字段 {anchor.normalized} 出现在 {len(hits)} 个页面的 "
                                f"props / 文案里，无法唯一归属，已降级为普通检索词"
                            ),
                            featureId=feature.id,
                        )
                    )
                elif hits:
                    page_id = next(iter(hits))
                    emit(feature.id, anchor, page_id, route_by_page.get(page_id))
                # 一个页面都没命中时不告警：字段是 best-effort 识别，
                # 未命中只能说明它不是 props / 文案层面的标识，保持沉默
            elif anchor.kind is AnchorKind.uiText:
                element_hits = ui_texts.get(anchor.raw, [])
                if not element_hits:
                    warnings.append(_unresolved(feature.id, anchor))
                    continue
                pages_hit = {page_id for _, _, page_id in element_hits}
                if len(pages_hit) > 1:
                    # 宁缺毋滥：同一句界面文案在多个页面出现（「暂无数据」这类
                    # 共通文案是常态），引文无法证明归属，降级为普通检索词
                    warnings.append(
                        RequirementWarning(
                            code="anchor_ambiguous",
                            severity=Severity.info,
                            message=(
                                f"界面文案「{anchor.raw}」出现在 {len(pages_hit)} 个页面的"
                                f"元素上，无法唯一归属，已降级为普通检索词"
                            ),
                            featureId=feature.id,
                        )
                    )
                    continue
                if len(element_hits) == 1:
                    # 唯一命中：落到元素级（差距表 #7）——chainId + targets 指向
                    # 「哪个按钮 / 哪一列」，前端据此高亮链路的具体段
                    chain_id, element_id, page_id = element_hits[0]
                    matches.append(
                        _match(
                            graph,
                            nodes_by_id,
                            feature.id,
                            anchor,
                            page_id=page_id,
                            route_id=route_by_page.get(page_id),
                            confidence=_ANCHOR_UITEXT,
                            chain_id=chain_id,
                            targets=[element_id],
                            path_node_ids=(element_id,),
                        )
                    )
                else:
                    # 同页多元素命中（如工具栏与行内都有「编辑」）：页面是确定的，
                    # 元素不唯一——落页面级 match，不猜具体元素
                    page_id = next(iter(pages_hit))
                    matches.append(
                        _match(
                            graph,
                            nodes_by_id,
                            feature.id,
                            anchor,
                            page_id=page_id,
                            route_id=route_by_page.get(page_id),
                            confidence=_ANCHOR_UITEXT,
                        )
                    )
                    warnings.append(
                        RequirementWarning(
                            code="anchor_element_ambiguous",
                            severity=Severity.info,
                            message=(
                                f"界面文案「{anchor.raw}」在该页面命中 "
                                f"{len(element_hits)} 个元素，只定位到页面，不猜具体交互"
                            ),
                            featureId=feature.id,
                        )
                    )

    deduped = _dedupe_matches(matches)
    covered = frozenset(match.featureId for match in deduped)
    return AnchorOutcome(matches=deduped, warnings=warnings, covered=covered)


def _element_text_index(
    graph: IRGraph,
    nodes_by_id: dict[str, IRNode],
    page_ids: set[str],
) -> dict[str, list[tuple[str, str, str]]]:
    """界面文案 → [(chainId, elementId, pageId)]。

    语料是链路起点元素的可见文案：viewText + attrs.columns 的字面量列名（列名由
    chains.py 的元素段语料同一规则收集，两处口径一致）。文案相等的判定用逐字
    匹配——锚点是用户用引号标出的原文，归一化放宽反而会制造高置信度误命中。
    每个元素恰好产出一条链路（derive_chains 逐元素建链），因此 (chain, element)
    不会重复。
    """
    chains = derive_chains(graph)
    index: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for chain in chains.by_id.values():
        if chain.page_id not in page_ids:
            continue
        element = nodes_by_id.get(chain.element.node_id)
        if element is None:
            continue
        for text in _element_texts(element):
            index[text].append((chain.id, element.id, chain.page_id))
    return dict(index)


def _element_texts(element: IRNode) -> list[str]:
    texts: list[str] = []
    view_text = element.attrs.get("viewText")
    if isinstance(view_text, str) and view_text.strip():
        texts.append(view_text)
    columns = element.attrs.get("columns")
    if isinstance(columns, list):
        for column in columns:
            if isinstance(column, dict):
                title = column.get("title")
                if isinstance(title, str) and title.strip():
                    texts.append(title)
    return texts


def _route_index(graph: IRGraph, nodes_by_id: dict[str, IRNode]) -> dict[str, str]:
    """归一化路径 → route 节点 id。attrs.path 缺省时退回节点名（即路径）。"""
    routes: dict[str, str] = {}
    for node in graph.nodes:
        if node.kind is not NodeKind.route:
            continue
        path = node.attrs.get("path") or node.name
        if isinstance(path, str) and (normalized := normalize_url(path)):
            routes.setdefault(normalized, node.id)
    return routes


def _route_pages(graph: IRGraph, nodes_by_id: dict[str, IRNode]) -> dict[str, list[str]]:
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


def _component_index(graph: IRGraph) -> tuple[dict[str, str], dict[str, str]]:
    """组件名 / 文件 → 组件节点 id。外部组件（第三方）不参与直连。"""
    by_name: dict[str, str] = {}
    by_file: dict[str, str] = {}
    for node in graph.nodes:
        if node.kind not in (NodeKind.component, NodeKind.server_component):
            continue
        if node.attrs.get("external") is True:
            continue
        by_name.setdefault(node.name, node.id)
        if node.file:
            by_file.setdefault(node.file.replace("\\", "/"), node.id)
    return by_name, by_file


def _field_index(
    graph: IRGraph, pages: Sequence[IRNode], layout_ids: set[str]
) -> dict[str, set[str]]:
    """字段名 → 出现该字段（props / 文案逐字相等）的页面集合。"""
    hits: dict[str, set[str]] = defaultdict(set)
    evidences = collect_page_evidences(graph, list(pages), layout_ids)
    for page_id, items in evidences.items():
        for item in items:
            if item.kind.value in ("propName", "uiText") and item.text:
                hits[item.text].add(page_id)
    return dict(hits)


def _unresolved(feature_id: str, anchor: Anchor) -> RequirementWarning:
    return RequirementWarning(
        code="anchor_unresolved",
        severity=Severity.info,
        message=(
            f"需求里的{anchor.kind.value}锚点「{anchor.raw}」在图中没有找到对应节点，"
            "已降级为普通检索词继续走语义定位"
        ),
        featureId=feature_id,
    )


def _match(
    graph: IRGraph,
    nodes_by_id: dict[str, IRNode],
    feature_id: str,
    anchor: Anchor,
    *,
    page_id: str,
    route_id: str | None,
    confidence: float,
    path_node_ids: Sequence[str] = (),
    chain_id: str | None = None,
    targets: list[str] | None = None,
) -> Match:
    reason = _REASONS[anchor.kind].format(raw=anchor.raw)
    evidence_ids = path_node_ids or (
        (route_id, page_id) if route_id else (page_id,)
    )
    evidence = [
        verified
        for node_id in evidence_ids[-_MAX_EVIDENCE_NODES:]
        if (verified := _node_verified(nodes_by_id.get(node_id), anchor.raw)) is not None
    ]
    return Match(
        featureId=feature_id,
        pageNodeId=page_id,
        chainId=chain_id,
        targets=targets,
        routeId=route_id,
        confidence=confidence,
        quote=anchor.raw,
        reason=reason,
        subgraph=build_subgraph(graph, page_id, route_id),
        verifiedEvidence=evidence or None,
        source=Source.static,
    )


_MAX_EVIDENCE_NODES = 4

_REASONS: dict[AnchorKind, str] = {
    AnchorKind.api: "锚点直连：需求提到接口 {raw}，沿图中调用链反查到该页面",
    AnchorKind.route: "锚点直连：需求提到路由 {raw}，图中该路由渲染此页面",
    AnchorKind.component: "锚点直连：需求提到组件 {raw}，图中它就是该页面",
    AnchorKind.file: "锚点直连：需求提到文件 {raw}，图中它定义了该页面",
    AnchorKind.field: "锚点直连：需求提到字段 {raw}，唯一出现在该页面的 props / 文案里",
    AnchorKind.uiText: "锚点直连：需求提到界面文案 {raw}，图中该页面存在带此文案的交互元素",
}


def _node_verified(node: IRNode | None, quote: str) -> VerifiedEvidence | None:
    """锚点命中节点的可复核坐标：文件、行列、来源都取自 IR，不取规则输出。"""
    if node is None:
        return None
    source = Source.static
    confidence = 1.0
    file = node.file
    loc = node.loc
    if node.evidence:
        best = sorted(
            node.evidence,
            key=lambda item: (-item.confidence, item.file or "", item.loc.line if item.loc else 0),
        )[0]
        source = best.source
        confidence = best.confidence
        file = file or best.file
        loc = loc or best.loc
    return VerifiedEvidence(
        nodeId=node.id,
        quote=quote,
        file=file,
        loc=Location(line=loc.line, col=loc.col) if loc is not None else None,
        via=_via_of(node),
        source=source,
        confidence=confidence,
    )


def _via_of(node: IRNode) -> str:
    if node.kind is NodeKind.element:
        return "uiText"
    if node.kind is NodeKind.api:
        return "apiUrl"
    return node.kind.value


def _dedupe_matches(matches: list[Match]) -> list[Match]:
    """同一 (功能点, 页面, 路由, 链路) 只留一条：置信度最高者优先，平手取先出现者。

    chainId 进 key：同一页面的页面级直连（字段锚点）与元素级直连（界面词锚点）
    是两条不同证据，不能互相吞掉。
    """
    best: dict[tuple[str, str, str | None, str | None], Match] = {}
    for match in matches:
        key = (match.featureId, match.pageNodeId, match.routeId, match.chainId)
        current = best.get(key)
        if current is None or match.confidence > current.confidence:
            best[key] = match
    return sorted(
        best.values(), key=lambda item: (item.featureId, -item.confidence, item.pageNodeId)
    )
