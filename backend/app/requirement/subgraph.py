"""匹配产物的局部子图：页面节点 + route + 一跳邻居。

Match.subgraph 的定位是「证据范围」而不是完整依赖闭包：它回答「这个页面的
结论是在多大范围内得出的」，供前端高亮与人工复核。一跳够了——再往外走
就是共享基础组件，只会把子图变成全图。
"""

from __future__ import annotations

from app.contracts import IRGraph, NodeKind
from app.contracts.requirement import Subgraph, SubgraphCounts

# 子图节点上限：真实项目里一个页面渲染几十个元素属正常，超过它说明
# 节点归属已不可信（比如把共享容器误当页面），截断比撑爆契约友好。
_MAX_SUBGRAPH_NODES = 80

# 契约的 counts 键固定 8 类；IR 没有独立的 event 节点（事件归在 element/fn 上），
# 该键恒为 0。
_COUNT_KEYS = ("component", "hook", "state", "api", "event", "fn", "route", "element")


def build_subgraph(graph: IRGraph, page_node_id: str, route_id: str | None) -> Subgraph:
    nodes = {node.id: node for node in graph.nodes}
    include: set[str] = set()
    if page_node_id in nodes:
        include.add(page_node_id)
    if route_id is not None and route_id in nodes:
        include.add(route_id)

    # 第一遍：页面的一跳邻居（两个方向），按 id 排序保证截断结果可复现
    neighbor_ids = sorted(
        {
            node_id
            for edge in graph.edges
            if edge.from_ == page_node_id or edge.to == page_node_id
            for node_id in (edge.from_, edge.to)
        }
        - include
    )
    for node_id in neighbor_ids:
        if len(include) >= _MAX_SUBGRAPH_NODES:
            break
        include.add(node_id)

    # 第二遍：只保留两端都在子图内的边
    edge_ids = [edge.id for edge in graph.edges if edge.from_ in include and edge.to in include]

    counts: dict[str, int] = dict.fromkeys(_COUNT_KEYS, 0)
    for node_id in include:
        kind = nodes[node_id].kind
        key = _COUNT_KEY_BY_KIND[kind]
        counts[key] += 1

    return Subgraph(
        nodeIds=sorted(include),
        edgeIds=edge_ids,
        counts=SubgraphCounts(
            component=counts["component"],
            hook=counts["hook"],
            state=counts["state"],
            api=counts["api"],
            event=counts["event"],
            fn=counts["fn"],
            route=counts["route"],
            element=counts["element"],
        ),
    )


_COUNT_KEY_BY_KIND: dict[NodeKind, str] = {
    NodeKind.component: "component",
    NodeKind.server_component: "component",
    NodeKind.server_action: "fn",
    NodeKind.element: "element",
    NodeKind.hook: "hook",
    NodeKind.state: "state",
    NodeKind.fn: "fn",
    NodeKind.api: "api",
    NodeKind.route: "route",
}
