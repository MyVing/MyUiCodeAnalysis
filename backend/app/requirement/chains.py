"""交互链路推导：把「一次用户动作」从图里确定性拼出来。

## 为什么需要这一层

需求定位原本只能回答「哪个页面」。但需求原文几乎从不以页面为单位：

> 「订单列表页按状态筛选，并支持导出。」

落点是两个具体的交互：筛选下拉框、导出按钮。页面上百个元素，只说「在 OrderList 里」
等于让用户自己去找。

链路把「元素 → 处理函数 → 状态 / 接口 / 跳转」串成一个整体。相邻两段之间的关系
（`<button>` calls `handleExport`、`handleExport` requests `GET /api/orders/export`）
本来就是带 `file` / `loc` 的证据边。

## 最重要的一条约束：AI 不参与构造

链路只能由确定性图遍历产出，理由与「静态分析管事实、AI 管语义」是同一条：

- 链路的每一段都是已存在的 IR 边，拼装过程**不引入任何新事实**。
- 若让模型生成链路，它就能拼出一条「每个节点都真实存在、但 A 调用 B 这件事是编的」
  的假路径。节点集合校验拦得住「节点不存在」，拦不住「边不存在」。

因此本模块只做图遍历，零模型调用。模型的作用在后面的定位阶段：从这些链路里
选「哪条对应这句需求」，并给出逐字可校验的引文。

## 断点如实标注

链路在两种情况下会断开：

- 外链 `<a href="https://...">`：目标不在路由表里，没有 route 节点可连。
- 处理函数由父组件注入（`onClick={props.onSelect}`）：函数体不在本组件内。

两种都**不编造**下游节点，而是记进 `terminated_at`。用户看到「链路在此中断：
外部链接」比看到一条半截链路却不说明要好。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.contracts import EdgeKind, IREdge, IRGraph, IRNode, NodeKind
from app.contracts.chain import ActionChain as ChainContract
from app.contracts.chain import ChainPath, ChainRole
from app.contracts.chain import ChainStep as StepContract

# 链路起点只可能是元素：需求描述的是「页面上看得见、点得到的东西」。
# 组件 / hook / state 作为起点会让候选退化成「整个程序结构」。
_START_KINDS = frozenset({NodeKind.element})

# element → fn → api 的遍历深度上限。真实项目里处理函数极少超过两层；
# 不设上限会在共享工具函数处把整棵调用树拖进来。
_MAX_HANDLER_DEPTH = 2

# 一条链路最多收几个下游效应。防止某个 handler 扇出到十几个接口时
# 单条链路撑爆上下文（与 _MAX_SUBGRAPH_NODES 是同一种保护）。
_MAX_EFFECTS = 8

# 每个页面最多保留多少条链路。一个中型页面有几十个带事件的元素，全量展开会让
# 链路列表变噪音、让 prompt 撑爆。截断不是「链路不存在」，而是「这次不展示」，
# 因此必须告警（见 ChainIndex.truncated）。
MAX_CHAINS_PER_PAGE = 40

# 断点原因。写成常量而不是内联字符串：前端要按它决定展示文案。
TERMINATED_EXTERNAL = "外部链接"
TERMINATED_INJECTED_HANDLER = "处理逻辑由父组件注入"
TERMINATED_NO_OWNER = "未找到所属页面"


@dataclass(frozen=True)
class ChainStep:
    """链路中的一段：节点 + 来源坐标 + 该段的语义角色。"""

    node_id: str
    # element / handler / state / hook / api / route
    role: str
    name: str
    file: str | None
    line: int | None
    col: int | None


@dataclass(frozen=True)
class ActionChain:
    """一次用户动作的完整路径。

    `id` 必须稳定：同一份图重复推导要得到同一个 id，否则前端的选中态、
    历史记录、增量 diff 全部会抖。
    """

    id: str
    page_id: str
    # 起点元素（一定存在）
    element: ChainStep
    # 逐段路径，从 element 开始，按遍历顺序
    steps: tuple[ChainStep, ...]
    # 与 steps 同序的节点 id，以及推导时实际走过的边 id
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    # 可见文案，如「导出」；动态文案时为 None
    text: str | None
    # 元素角色：control / link / container / text
    view_role: str | None
    # 外链地址（仅 <a href> 且为字面量时）
    href: str | None
    # 非空表示链路在此中断，值为原因
    terminated_at: str | None
    # 这条链路能用来召回的全部原文（文案 + href + 各段名字）
    corpus: tuple[str, ...] = field(default_factory=tuple)
    # 同上，但按段归属：(nodeId, 该段能命中的原文)。
    #
    # 总 corpus 只能回答「这句话出现在这条链里」，回答不了「是链的哪一步」。
    # 校验阶段要按引文高亮具体段（Match.targets），所以归属必须单独保留。
    step_corpus: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)

    def to_contract(self) -> ChainContract:
        """转成共享契约。校验阶段与前端消费的都是契约类型。"""
        steps = [_step_contract(step) for step in self.steps]
        return ChainContract(
            id=self.id,
            pageId=self.page_id,
            element=steps[0],
            steps=steps,
            path=ChainPath(nodeIds=list(self.node_ids), edgeIds=list(self.edge_ids)),
            text=self.text,
            viewRole=self.view_role,
            href=self.href,
            terminatedAt=self.terminated_at,
            corpus=list(self.corpus) or None,
        )


def _step_contract(step: ChainStep) -> StepContract:
    return StepContract(
        nodeId=step.node_id,
        role=ChainRole(step.role),
        name=step.name or step.node_id,
        file=step.file,
        loc=_loc_contract(step),
    )


def _loc_contract(step: ChainStep) -> dict[str, int] | None:
    if step.line is None:
        return None
    # 契约的 Location 要求 col ≥ 1；行号有而列号缺失时按 1 记，不编造位置但也不丢行号
    return {"line": step.line, "col": step.col or 1}


@dataclass(frozen=True)
class TerminatedChain:
    """被判定为「无归属」而丢弃的交互元素，供诊断用。"""

    element_id: str
    file: str | None
    reason: str


@dataclass
class ChainIndex:
    """全图的链路索引：按页面分组，并保留 id → 链路 的映射供校验。"""

    by_page: dict[str, list[ActionChain]]
    by_id: dict[str, ActionChain]
    # 因超出 MAX_CHAINS_PER_PAGE 被截断的页面 → 丢弃条数
    truncated: dict[str, int] = field(default_factory=dict)

    def for_page(self, page_id: str) -> list[ActionChain]:
        return self.by_page.get(page_id, [])

    def contracts_for(self, chain_ids: list[str] | tuple[str, ...]) -> list[ChainContract]:
        """按给定 id 顺序导出契约，未知 id 直接跳过（由调用方的校验负责报错）。"""
        result: list[ChainContract] = []
        for chain_id in chain_ids:
            chain = self.by_id.get(chain_id)
            if chain is not None:
                result.append(chain.to_contract())
        return result

    def page_of(self, chain_id: str) -> str | None:
        chain = self.by_id.get(chain_id)
        return chain.page_id if chain is not None else None

    def corpus_of(self, chain_id: str) -> tuple[str, ...]:
        chain = self.by_id.get(chain_id)
        return chain.corpus if chain is not None else ()

    def page_corpus(self, page_id: str) -> tuple[str, ...]:
        """该页面下全部链路的可命中原文并集。

        模型只定位到页面（没给 chainId）时，引文校验退回到页面级语料——
        此时证据范围仍是「这个页面上真实存在的交互」，而不是整个项目的文案。
        """
        texts: list[str] = []
        for chain in self.by_page.get(page_id, []):
            texts.extend(chain.corpus)
        return tuple(_unique(texts))

    def targets_of(self, chain_id: str, quote: str) -> list[str]:
        """引文落在链路的哪几段。

        一个引文可能同时是某段的文案与另一段的接口地址（`/api/orders` 既是
        handler 名的一部分也可能是路由），所以返回列表而不是单值；按链路顺序。
        """
        chain = self.by_id.get(chain_id)
        if chain is None:
            return []
        normalized = quote.strip()
        return [
            node_id
            for node_id, texts in chain.step_corpus
            if any(text.strip() == normalized for text in texts)
        ]

    def chains_for_pages(self, page_ids: list[str] | tuple[str, ...]) -> list[ActionChain]:
        """给定页面集合下的全部链路，按 (页面, 链路 id) 排序。"""
        result: list[ActionChain] = []
        for page_id in page_ids:
            result.extend(self.by_page.get(page_id, []))
        return result


def derive_chains(graph: IRGraph) -> ChainIndex:
    """从图里确定性推导全部交互链路。

    顺序保证：节点按 id 排序遍历，链路按 (页面, 元素位置) 排序，
    因此同一份图重复调用得到完全相同的输出。
    """
    nodes = {node.id: node for node in graph.nodes}
    outgoing: dict[str, list[IREdge]] = {}
    for edge in graph.edges:
        outgoing.setdefault(edge.from_, []).append(edge)

    # 页面集合：被 route 渲染的组件。没有 route 时退化为「全部组件」，
    # 这样没有路由表的项目（也可被分析）仍能产出链路。
    page_ids = _page_ids(graph, nodes)

    # 元素 → 所属页面。走 contains 反向；中途的布局组件继续沿 renders 上溯。
    owner_of = _owner_map(graph, nodes, page_ids)

    chains: list[ActionChain] = []
    for element in sorted(
        (node for node in graph.nodes if node.kind in _START_KINDS),
        key=lambda node: node.id,
    ):
        page_id = owner_of.get(element.id)
        if page_id is None or page_id not in page_ids:
            continue
        chain = _build_chain(element, page_id, nodes, outgoing)
        if chain is not None:
            chains.append(chain)

    by_page: dict[str, list[ActionChain]] = {}
    by_id: dict[str, ActionChain] = {}
    truncated: dict[str, int] = {}
    for chain in chains:
        by_page.setdefault(chain.page_id, []).append(chain)
    for page_id, group in by_page.items():
        group.sort(key=lambda item: item.id)
        if len(group) > MAX_CHAINS_PER_PAGE:
            truncated[page_id] = len(group) - MAX_CHAINS_PER_PAGE
            by_page[page_id] = group[:MAX_CHAINS_PER_PAGE]
    for group in by_page.values():
        for chain in group:
            by_id[chain.id] = chain

    return ChainIndex(by_page=by_page, by_id=by_id, truncated=truncated)


def _page_ids(graph: IRGraph, nodes: dict[str, IRNode]) -> set[str]:
    rendered: set[str] = set()
    for edge in graph.edges:
        if edge.kind is not EdgeKind.renders:
            continue
        source = nodes.get(edge.from_)
        target = nodes.get(edge.to)
        if source is None or source.kind is not NodeKind.route:
            continue
        if target is not None and target.kind in (NodeKind.component, NodeKind.server_component):
            rendered.add(target.id)
    if rendered:
        return rendered
    return {
        node.id
        for node in graph.nodes
        if node.kind in (NodeKind.component, NodeKind.server_component)
        and node.attrs.get("external") is not True
    }


def _owner_map(graph: IRGraph, nodes: dict[str, IRNode], page_ids: set[str]) -> dict[str, str]:
    """元素 → 所属页面的映射。

    视图层的 `contains` 边是元素归属的唯一来源。元素可能挂在另一个元素下
    （`<li>` 里的 `<a>`），所以要顺着 contains 往上走到组件。

    ## 为什么不能停在「最近的那个组件」

    `FullLayout → Header → <a>`：最近组件是 Header，但 Header 不是 route 目标，
    「GitHub 链接在哪个页面」的正确答案是 FullLayout。停在 Header 会让布局
    （导航栏、页脚）里的全部交互失去归属，用户描述「页面右上角的 GitHub 链接」
    就永远定位不到。

    因此沿祖先链找到**第一个属于 page_ids 的组件**，中途经过的组件只是渲染
    路径上的一环。找不到（元素属于某个未挂路由的独立组件）才回落到最近的组件。
    """
    parent: dict[str, str] = {}
    for edge in graph.edges:
        if edge.kind is not EdgeKind.contains:
            continue
        # 保留先出现的那条，保证归属稳定
        parent.setdefault(edge.to, edge.from_)

    # renders 边让孩子组件也能往上找到它被谁渲染
    render_parent: dict[str, str] = {}
    for edge in graph.edges:
        if edge.kind is not EdgeKind.renders:
            continue
        source = nodes.get(edge.from_)
        if source is None or source.kind is NodeKind.route:
            continue
        render_parent.setdefault(edge.to, edge.from_)

    owner: dict[str, str] = {}
    for node in graph.nodes:
        if node.kind is not NodeKind.element:
            continue
        current = parent.get(node.id)
        visited: set[str] = set()
        nearest_component: str | None = None
        while current is not None and current not in visited:
            visited.add(current)
            node_at = nodes.get(current)
            if node_at is None:
                break
            if node_at.kind in (NodeKind.component, NodeKind.server_component):
                if nearest_component is None:
                    nearest_component = current
                if current in page_ids:
                    owner[node.id] = current
                    break
                # 继续沿 renders 往上：布局组件在渲染路径上，页面才是落点
                current = render_parent.get(current) or parent.get(current)
                continue
            current = parent.get(current)
        else:
            if nearest_component is not None:
                owner[node.id] = nearest_component
    return owner


def _build_chain(
    element: IRNode,
    page_id: str,
    nodes: dict[str, IRNode],
    outgoing: dict[str, list[IREdge]],
) -> ActionChain | None:
    """从单个元素出发拼链路。没有任何交互的元素不产链路。

    判据：它至少「能点」——有 calls / writes / navigates 边，或是带 href 的链接。
    纯文本元素（`<h1>标题</h1>`）是展示内容，不是交互，归 hint 层而不是链路层。
    """
    text = _view_text(element)
    href = element.attrs.get("href")
    href_value = href if isinstance(href, str) and href else None

    steps: list[ChainStep] = [_step_of(element, "element")]
    edge_ids: list[str] = []
    # 按段收集原文：顺序与 steps 一致（dict 保持插入顺序）
    step_texts: dict[str, list[str]] = {element.id: []}
    if text:
        step_texts[element.id].append(text)
    if href_value:
        step_texts[element.id].append(href_value)

    _walk(
        element,
        nodes,
        outgoing,
        depth=0,
        steps=steps,
        edge_ids=edge_ids,
        step_texts=step_texts,
        visited={element.id},
    )

    # 「能点」不等于「有下游节点」：父组件注入的 handler 在静态图上没有函数体，
    # 但它确实是一个交互（用户能点它）。这类元素要产链路并标断点，
    # 而不是因为「找不到下游」就当作纯展示元素丢掉。
    terminated = _terminate_reason(element, href_value, steps, edge_ids)
    has_interaction = bool(len(steps) > 1 or href_value or terminated)
    if not has_interaction:
        return None

    node_path = "|".join(step.node_id for step in steps)

    return ActionChain(
        id=f"{element.id}::{node_path}",
        page_id=page_id,
        element=steps[0],
        steps=tuple(steps),
        node_ids=tuple(step.node_id for step in steps),
        edge_ids=tuple(edge_ids),
        text=text,
        view_role=_view_role(element),
        href=href_value,
        terminated_at=terminated,
        corpus=tuple(_unique(part for texts in step_texts.values() for part in texts)),
        step_corpus=tuple(
            (node_id, tuple(_unique(texts))) for node_id, texts in step_texts.items() if texts
        ),
    )


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(part for part in items if part))


def _walk(
    source: IRNode,
    nodes: dict[str, IRNode],
    outgoing: dict[str, list[IREdge]],
    *,
    depth: int,
    steps: list[ChainStep],
    edge_ids: list[str],
    step_texts: dict[str, list[str]],
    visited: set[str],
) -> None:
    """沿 calls / writes / navigates / requests 向下走，边收集边记录走过的边。

    顺序按 (edge.kind, edge.id) 排序：同一份图必须给出同一条路径，
    否则链路的 id、前端的选中态、增量 diff 都会抖。
    """
    if depth > _MAX_HANDLER_DEPTH:
        return

    emitted = {step.node_id for step in steps}
    for edge in sorted(outgoing.get(source.id, []), key=lambda item: (item.kind.value, item.id)):
        target = nodes.get(edge.to)
        if target is None or target.id in emitted:
            continue

        if edge.kind is EdgeKind.calls:
            if len(steps) > _MAX_EFFECTS + 1:
                continue
            steps.append(_step_of(target, "handler"))
            edge_ids.append(edge.id)
            step_texts.setdefault(target.id, []).append(target.name)
            emitted.add(target.id)
            if target.id not in visited:
                visited.add(target.id)
                _walk(
                    target,
                    nodes,
                    outgoing,
                    depth=depth + 1,
                    steps=steps,
                    edge_ids=edge_ids,
                    step_texts=step_texts,
                    visited=visited,
                )
        elif edge.kind is EdgeKind.writes:
            if len(steps) > _MAX_EFFECTS + 1:
                continue
            steps.append(_step_of(target, target.kind.value))
            edge_ids.append(edge.id)
            step_texts.setdefault(target.id, []).append(target.name)
            emitted.add(target.id)
        elif edge.kind is EdgeKind.navigates:
            if target.kind is not NodeKind.route or len(steps) > _MAX_EFFECTS + 1:
                continue
            steps.append(_step_of(target, "route"))
            edge_ids.append(edge.id)
            step_texts.setdefault(target.id, []).append(target.name)
            emitted.add(target.id)
        elif edge.kind is EdgeKind.requests:
            # element/handler → api 的直接请求（handler 之外的形态）
            if target.kind is not NodeKind.api or len(steps) > _MAX_EFFECTS + 1:
                continue
            steps.append(_step_of(target, "api"))
            edge_ids.append(edge.id)
            texts = step_texts.setdefault(target.id, [])
            url = target.attrs.get("url")
            if isinstance(url, str):
                texts.append(url)
            texts.append(target.name)
            emitted.add(target.id)


def _terminate_reason(
    element: IRNode,
    href: str | None,
    steps: list[ChainStep],
    edge_ids: list[str],
) -> str | None:
    """链路是否在这里断了，以及为什么。

    只标注**确定**的断点，不猜：
      - 外链型 `<a href>`：目标不在路由表内，静态图上没有下一段
      - 带事件但没有可解析的下游：函数体由父组件注入（或内联到不可解析的形态）

    有下游段说明链路是完整的，不标断点。
    """
    has_downstream = len(steps) > 1 or bool(edge_ids)
    if href is not None and not has_downstream and element.attrs.get("external") is True:
        return TERMINATED_EXTERNAL
    if not has_downstream and not href:
        # 有 onClick 属性但 handler 名字解析不到（内联箭头函数或父组件注入）
        events = element.attrs.get("events")
        if isinstance(events, list) and events:
            return TERMINATED_INJECTED_HANDLER
    return None


def _view_text(node: IRNode) -> str | None:
    value = node.attrs.get("viewText")
    return value if isinstance(value, str) and value.strip() else None


def _view_role(node: IRNode) -> str | None:
    value = node.attrs.get("viewRole")
    return value if isinstance(value, str) else None


def _step_of(node: IRNode, role: str) -> ChainStep:
    loc = node.loc
    if loc is None and node.evidence:
        ranked = sorted(
            node.evidence,
            key=lambda item: (-item.confidence, item.file or "", item.loc.line if item.loc else 0),
        )
        loc = ranked[0].loc
        file = ranked[0].file
    else:
        file = node.file
    return ChainStep(
        node_id=node.id,
        role=role,
        name=node.name,
        file=node.file or file,
        line=loc.line if loc else None,
        col=loc.col if loc else None,
    )
