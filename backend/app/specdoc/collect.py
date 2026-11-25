"""规格装配的素材解析：RequirementMap + IRGraph → 页面编号、元素清单、LLM 语料。

职责边界：本模块只做「取材」，不做判定——三分类、事件拆解、接口去重都在
下游模块。取材必须可复现：页面序号按 matches 出现顺序，元素按 node id 排序，
语料按固定顺序去重。

硬约束（与 requirement 链路同一条）：页面画像（业务描述）不进语料。语料里
只能是源码原文——元素文案、handler 名、接口 URL、路由路径——引文校验的
可信度建立在「语料 = 代码里真实存在的文本」之上。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.contracts import IRGraph, IRNode, NodeKind
from app.contracts.requirement import RequirementMap, RequirementWarning, Severity
from app.contracts.specdoc import FieldMapping, Location, Page, VerifiedEvidence
from app.contracts.specdoc import Source as DocSource
from app.requirement.chains import ActionChain, derive_chains


@dataclass(frozen=True)
class ElementSource:
    """页面上的一个元素节点 + 与它相关的链路。"""

    node: IRNode
    # 从该元素出发的交互链路（0~n 条；纯展示元素没有链路）
    chains: tuple[ActionChain, ...] = ()
    # 命中该元素链路的功能点 id（需求定位的 matches 给出）
    matched_features: frozenset[str] = frozenset()

    @property
    def has_api(self) -> bool:
        """任一链路含 api 段——action_remote 的判定依据。"""
        return any(
            any(step.role == "api" for step in chain.steps) for chain in self.chains
        )


@dataclass(frozen=True)
class PageMaterial:
    """一个页面的全部装配素材。"""

    page: Page
    node: IRNode
    # 该页面名下的全部元素（含纯展示元素），按 node id 排序
    elements: list[ElementSource] = field(default_factory=list)
    # 该页面的全部交互链路（derive_chains 确定性产出，按 id 排序）
    chains: list[ActionChain] = field(default_factory=list)
    # LLM 引文语料：链路原文 + 元素文案 + 页面文案，去重保序
    corpus: tuple[str, ...] = ()
    # 页面内 API 节点，供字段映射读取请求体包装事实
    api_nodes: dict[str, IRNode] = field(default_factory=dict)
    # 被需求定位命中的链路 id
    matched_chain_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Materials:
    """一次装配的全部素材。"""

    pages: list[PageMaterial] = field(default_factory=list)
    # 跨页面的链路索引：事件 / 接口装配按 chainId 取链
    chain_by_id: dict[str, ActionChain] = field(default_factory=dict)
    warnings: list[RequirementWarning] = field(default_factory=list)

    @property
    def has_pages(self) -> bool:
        return bool(self.pages)


def collect_materials(requirement_map: RequirementMap, graph: IRGraph) -> Materials:
    """从定位产物与图里取材。纯函数：同一输入必得同一输出。"""
    pages = _assign_page_numbers(requirement_map, graph)
    if not pages:
        return Materials(
            warnings=[
                RequirementWarning(
                    code="no_matched_pages",
                    severity=Severity.warning,
                    message=(
                        "来源定位产物没有任何命中页面（dry-run 定位的 matches 必为空），"
                        "无素材可装配。请先以 dryRun=false 重新定位。"
                    ),
                )
            ]
        )

    page_ids = [page.pageId for page in pages]
    chain_index = derive_chains(graph)
    owner_of = _owner_map(graph, set(page_ids))
    # 需求定位命中的链路 → 起点元素 → 功能点：元素的 featureIds 据此标注
    chain_features: dict[str, dict[str, list[str]]] = {}
    for match in requirement_map.matches:
        if match.chainId is None:
            continue
        chain = chain_index.by_id.get(match.chainId)
        if chain is None:
            continue
        by_element = chain_features.setdefault(match.chainId, {})
        if match.featureId not in by_element.setdefault(chain.element.node_id, []):
            by_element[chain.element.node_id].append(match.featureId)

    nodes = {node.id: node for node in graph.nodes}
    materials = Materials()
    for page in pages:
        page_id = page.pageId
        node = nodes.get(page_id)
        if node is None:
            # 定位产物引用了图上不存在的节点：图已更新而产物未同步。如实告警并跳过
            materials.warnings.append(
                RequirementWarning(
                    code="page_node_missing",
                    severity=Severity.warning,
                    message=f"页面节点 {page_id} 不在当前图中，已跳过该页面。",
                )
            )
            continue
        element_ids = {
            element_id for element_id, owner in owner_of.items() if owner == page_id
        }
        element_nodes = sorted(
            (nodes[element_id] for element_id in element_ids),
            key=lambda item: item.id,
        )
        chains = sorted(chain_index.for_page(page_id), key=lambda item: item.id)
        chain_by_element: dict[str, list[ActionChain]] = {}
        for chain in chains:
            chain_by_element.setdefault(chain.element.node_id, []).append(chain)
        matched_chain_ids = {
            match.chainId
            for match in requirement_map.matches
            if match.pageNodeId == page_id
            and match.chainId is not None
            and match.chainId in chain_index.by_id
        }

        element_sources = [
            ElementSource(
                node=element,
                chains=tuple(chain_by_element.get(element.id, ())),
                matched_features=frozenset(
                    feature_id
                    for chain in chain_by_element.get(element.id, ())
                    for feature_id in chain_features.get(chain.id, {}).get(element.id, [])
                ),
            )
            for element in element_nodes
        ]
        corpus = _page_corpus(node, element_nodes, chains)
        api_nodes = {
            item.id: item
            for item in nodes.values()
            if item.kind is NodeKind.api
            and any(step.node_id == item.id for chain in chains for step in chain.steps)
        }
        materials.pages.append(
            PageMaterial(
                page=page,
                node=node,
                elements=element_sources,
                chains=chains,
                corpus=corpus,
                api_nodes=api_nodes,
                matched_chain_ids=frozenset(matched_chain_ids),
            )
        )
    for material in materials.pages:
        for chain in material.chains:
            materials.chain_by_id[chain.id] = chain
    return materials


@dataclass(frozen=True)
class _PendingPage:
    """页面编号的中间形态：collect 主流程先算编号，再补素材。"""

    page: Page


def _assign_page_numbers(requirement_map: RequirementMap, graph: IRGraph) -> list[Page]:
    """按 matches 出现顺序给页面编号（1 起），重复页面合并 featureIds。

    match.routeId 取第一个非空值：同一页面的多次命中理应给出同一路由。
    """
    order: list[str] = []
    route_ids: dict[str, str | None] = {}
    features: dict[str, list[str]] = {}
    for match in requirement_map.matches:
        page_id = match.pageNodeId
        if page_id not in features:
            order.append(page_id)
            route_ids[page_id] = match.routeId
            features[page_id] = []
        if match.routeId and not route_ids[page_id]:
            route_ids[page_id] = match.routeId
        if match.featureId not in features[page_id]:
            features[page_id].append(match.featureId)

    nodes = {node.id: node for node in graph.nodes}
    pages: list[Page] = []
    for index, page_id in enumerate(order, start=1):
        node = nodes.get(page_id)
        pages.append(
            Page(
                pageId=page_id,
                pageNo=index,
                name=node.name if node is not None else page_id,
                file=node.file if node is not None else None,
                routeId=route_ids[page_id],
                featureIds=features[page_id],
            )
        )
    return pages


def _owner_map(graph: IRGraph, page_ids: set[str]) -> dict[str, str]:
    """元素 → 所属页面的映射。

    与 chains._owner_map 同一条规则：contains 边是归属的唯一来源，沿祖先链
    找到第一个属于页面集合的组件；布局组件沿 renders 边继续上溯。
    这里独立实现而不是复用私有函数：specdoc 只关心页面集合内的归属，
    语义保持一致即可，两处实现由各自的测试锁定。
    """
    from app.contracts import EdgeKind

    parent: dict[str, str] = {}
    render_parent: dict[str, str] = {}
    nodes = {node.id: node for node in graph.nodes}
    for item in graph.edges:
        if item.kind is EdgeKind.contains:
            parent.setdefault(item.to, item.from_)
        elif (
            item.kind is EdgeKind.renders
            and (source := nodes.get(item.from_)) is not None
            and source.kind is not NodeKind.route
        ):
            render_parent.setdefault(item.to, item.from_)

    owner: dict[str, str] = {}
    for node in graph.nodes:
        if node.kind is not NodeKind.element:
            continue
        current = parent.get(node.id)
        visited: set[str] = set()
        while current is not None and current not in visited:
            visited.add(current)
            node_at = nodes.get(current)
            if node_at is None:
                break
            if node_at.kind in (NodeKind.component, NodeKind.server_component):
                if current in page_ids:
                    owner[node.id] = current
                    break
                current = render_parent.get(current) or parent.get(current)
                continue
            current = parent.get(current)
    return owner


def _page_corpus(
    page_node: IRNode,
    element_nodes: list[IRNode],
    chains: list,
) -> tuple[str, ...]:
    """页面级引文语料。

    组成（按固定顺序去重）：
      1. 页面自身的 uiText（页面标题等页面级文案）
      2. 每个元素的可见文案（含纯展示元素——需求说「可用余额」时引用的
         往往是展示文案，而展示元素不产链路，链路语料里没有它）
      3. 每条链路的原文（元素文案 + href + handler 名 + 接口 URL + 路由）
      4. 元素事件绑定的 handler 源码片段（attrs.events[].handler，截断文本）

    页面画像不进语料：画像是一句话业务描述，不是代码原文。
    """
    texts: list[str] = []

    ui_text = page_node.attrs.get("uiText")
    if isinstance(ui_text, dict):
        entries = ui_text.get("texts")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                    texts.append(entry["text"])

    for element in element_nodes:
        view_text = element.attrs.get("viewText")
        if isinstance(view_text, str) and view_text.strip():
            texts.append(view_text)
        events = element.attrs.get("events")
        if isinstance(events, list):
            for binding in events:
                if not isinstance(binding, dict):
                    continue
                handler_name = binding.get("handlerName")
                if isinstance(handler_name, str) and handler_name.strip():
                    texts.append(handler_name)
                handler_source = binding.get("handler")
                # handler 是截断后的源码文本：给模型看上下文用，不参与逐字校验
                if isinstance(handler_source, str) and handler_source.strip():
                    texts.append(handler_source)

    for chain in chains:
        texts.extend(chain.corpus)

    return tuple(_unique(texts))


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item and item.strip()))


def deterministic_field_mappings(materials: Materials) -> list[FieldMapping]:
    """从 IR 的表单注册事实与请求体字段生成可审计字段映射。"""
    result: list[FieldMapping] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for material in materials.pages:
        request_apis = [
            (step.name, material.api_nodes.get(step.node_id))
            for chain in material.chains
            for step in chain.steps
            if step.role == "api" and material.api_nodes.get(step.node_id) is not None
        ]
        for source in material.elements:
            field = source.node.attrs.get("formField")
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            front_field = field["name"]
            for api_name, api_node in request_apis:
                if api_node is None:
                    continue
                body = api_node.attrs.get("requestBody")
                if not isinstance(body, dict):
                    continue
                parts = api_name.split(None, 1)
                method, url = (parts if len(parts) == 2 else ("GET", api_name))
                api = f"{method} {url}"
                backends = _request_body_backends(body, front_field)
                if not backends:
                    continue
                evidence = _field_mapping_evidence(source.node, field)
                if evidence is None:
                    continue
                for backend in backends:
                    key = (front_field, backend, api, material.page.pageId)
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append(
                        FieldMapping(
                            frontField=front_field,
                            backendParam=backend,
                            api=api,
                            pageId=material.page.pageId,
                            quote=evidence.quote,
                            evidence=[evidence],
                        )
                    )
    return result


def _request_body_backends(body: dict, front_field: str) -> list[str]:
    """Return statically supported backend paths for one registered form field.

    The legacy ``wrapper`` shape means the request body is known to be wrapped,
    while ``fields`` records the exact properties of a direct object payload.
    A direct payload is only associated when its property or source identifier
    matches the form registration name.
    """
    wrapper = body.get("wrapper")
    fields = body.get("fields")
    if isinstance(fields, list):
        result: list[str] = []
        for item in fields:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            source = item.get("source")
            if not isinstance(name, str) or not name:
                continue
            if name == front_field or source == front_field:
                result.append(f"{wrapper}.{name}" if isinstance(wrapper, str) and wrapper else name)
        return list(dict.fromkeys(result))
    if isinstance(wrapper, str) and wrapper:
        return [f"{wrapper}.{front_field}"]
    return []


def _field_mapping_evidence(node: IRNode, field: dict) -> VerifiedEvidence | None:
    if not node.evidence:
        return None
    best = max(node.evidence, key=lambda item: item.confidence)
    quote = field.get("quote")
    if not isinstance(quote, str) or not quote:
        return None
    field_loc = field.get("loc")
    if isinstance(field_loc, dict):
        line = field_loc.get("line")
        col = field_loc.get("col")
        loc = (
            Location(line=line, col=col)
            if isinstance(line, int) and line >= 1 and isinstance(col, int) and col >= 1
            else None
        )
    else:
        ir_loc = best.loc
        loc = Location(line=ir_loc.line, col=ir_loc.col) if ir_loc else None
    return VerifiedEvidence(
        nodeId=node.id,
        quote=quote,
        file=best.file,
        loc=loc,
        via="formField",
        source=DocSource(best.source.value),
        confidence=best.confidence,
    )
def element_evidence(node: IRNode) -> VerifiedEvidence | None:
    """确定性条目的证据：取 IR 节点置信度最高的一条证据 + 元素原文。

    quote 用 viewText（可见文案）或节点名——证据必须能在代码里原样找到。
    """
    if not node.evidence:
        return None
    best = sorted(
        node.evidence,
        key=lambda item: (-item.confidence, item.file or "", item.loc.line if item.loc else 0),
    )[0]
    view_text = node.attrs.get("viewText")
    quote = view_text if isinstance(view_text, str) and view_text.strip() else node.name
    return VerifiedEvidence(
        nodeId=node.id,
        quote=quote,
        file=best.file,
        loc=(
            Location(line=best.loc.line, col=best.loc.col)
            if best.loc is not None
            else None
        ),
        source=DocSource(best.source.value),
        confidence=best.confidence,
    )
