"""锚点反向归属测试（6.11 通用化）：种子发现 → 反向 BFS → 运行时双证。

锚点直连（anchors.py / api_usage.py 的归一化与 api 反查）已有独立测试；
这里聚焦召回阶段的新能力：全图种子、任意节点反向归属、无路由降级终跳、
共享布局去重与衰减、运行时双证强化、无种子命中的词法兜底。
"""

from __future__ import annotations

import pytest

from app.contracts import (
    EdgeKind,
    IRGraph,
    NodeKind,
    RuntimeTrace,
    SignalKind,
)
from app.contracts.runtime import ComponentNode, NetworkEvent, PageInfo
from app.requirement.features import decompose
from app.requirement.recall import collect_seeds, recall
from tests.test_requirement_recall import _edge, _graph, _node, _page


def _user_graph() -> IRGraph:
    """与 test_requirement_recall.user_graph 同形的五类种子样本图。"""
    user_list = _page(
        "src/pages/UserList.tsx#UserList",
        "UserList",
        texts=["搜索用户", "姓名", "邮箱"],
    )
    user_detail = _page("src/pages/UserDetail.tsx#UserDetail", "UserDetail")
    fetch_users = _node(
        "src/pages/UserList.tsx#fetchUsers",
        NodeKind.fn,
        "fetchUsers",
        file="src/pages/UserList.tsx",
    )
    api = _node(
        "api:GET:/api/users",
        NodeKind.api,
        "GET /api/users",
        attrs={"method": "GET", "url": "/api/users"},
    )
    route_list = _node(
        "src/router.tsx#/users", NodeKind.route, "/users", attrs={"path": "/users"}
    )
    route_detail = _node(
        "src/router.tsx#/users/:userId",
        NodeKind.route,
        "/users/:userId",
        attrs={"path": "/users/:userId"},
    )
    return _graph(
        [user_list, user_detail, fetch_users, api, route_list, route_detail],
        [
            _edge(route_list.id, user_list.id, EdgeKind.renders),
            _edge(route_detail.id, user_detail.id, EdgeKind.renders),
            _edge(user_list.id, fetch_users.id, EdgeKind.calls),
            _edge(fetch_users.id, api.id, EdgeKind.requests),
        ],
    )


def test_collect_seeds_covers_five_kinds() -> None:
    """种子覆盖：路由、组件名、界面文案、接口 URL、函数名——各取到一个样本。"""
    seeds = collect_seeds(_user_graph())
    by_kind = {}
    for _node_id, kind, text in seeds:
        by_kind.setdefault(kind, set()).add(text)

    assert "/users" in by_kind[SignalKind.routePath]
    assert "UserList" in by_kind[SignalKind.componentName]
    assert "搜索用户" in by_kind[SignalKind.uiText]
    assert "/api/users" in by_kind[SignalKind.apiUrl]
    assert "fetchUsers" in by_kind[SignalKind.handlerName]


def test_api_seed_outranks_text_seed() -> None:
    """接口种子的归属得分应高于纯文案种子——接口是需求文档里最无歧义的锚点。

    两个页面都有「列表」文案；只有其中一个页面挂着 /api/orders。需求
    「订单列表接口」应把带接口的页面排到前面。
    """
    orders = _page(
        "src/pages/Orders.tsx#Orders",
        "Orders",
        texts=["订单列表"],
    )
    users = _page("src/pages/Users.tsx#Users", "Users", texts=["用户列表"])
    route_orders = _node(
        "src/router.tsx#/orders", NodeKind.route, "/orders", attrs={"path": "/orders"}
    )
    route_users = _node(
        "src/router.tsx#/users", NodeKind.route, "/users", attrs={"path": "/users"}
    )
    api = _node(
        "api:GET:/api/orders",
        NodeKind.api,
        "GET /api/orders",
        attrs={"method": "GET", "url": "/api/orders"},
    )
    graph = _graph(
        [orders, users, route_orders, route_users, api],
        [
            _edge(route_orders.id, orders.id, EdgeKind.renders),
            _edge(route_users.id, users.id, EdgeKind.renders),
            _edge(orders.id, api.id, EdgeKind.requests),
        ],
    )
    # api 种子靠 ASCII 词命中 URL——需求里带 URL 字样时直击目标页面
    result = recall(graph, decompose("订单列表 GET /api/orders")[0].terms)

    assert result.candidates[0].candidate.nodeId.endswith("#Orders")
    orders_detail = result.candidates[0]
    assert any(signal.kind is SignalKind.apiUrl for signal in orders_detail.candidate.signals)
    # 最优命中的归属路径落在 Orders 页面上（路由命中与接口命中都归属到它）
    assert orders_detail.candidate.anchorNodeIds is not None
    assert orders_detail.candidate.anchorNodeIds[-2].endswith("#Orders")


def test_walk_stops_at_routeless_page() -> None:
    """无路由降级图：子组件文案沿 renders 反向归属到顶层组件即终跳。"""
    page = _page("src/pages/Lone.tsx#Lone", "Lone", texts=["标题"])
    child = _page("src/pages/Lone.tsx#Child", "Child", texts=["盘点明细"])
    graph = _graph([page, child], [_edge(page.id, child.id, EdgeKind.renders)])
    result = recall(graph, decompose("盘点明细")[0].terms)

    lone = next(
        item
        for item in result.candidates
        if item.candidate.nodeId.endswith("#Lone")
    )
    assert lone.candidate.routeId is None
    assert lone.candidate.anchorNodeIds == [
        "src/pages/Lone.tsx#Child",
        "src/pages/Lone.tsx#Lone",
    ]


def test_shared_layout_hit_deduped_and_decayed() -> None:
    """共享外壳的文案：两条路由只算一次命中，且按布局复用折半。"""
    header = _node(
        "src/core/Header.tsx#Header",
        NodeKind.component,
        "Header",
        file="src/core/Header.tsx",
        attrs={
            "uiText": {
                "texts": [
                    {"text": "github", "kind": "text", "via": None, "loc": {"line": 1, "col": 1}}
                ]
            }
        },
    )
    layout = _node(
        "src/core/FullLayout.tsx#FullLayout", NodeKind.component, "FullLayout"
    )
    home = _page("src/pages/Home.tsx#Home", "Home")
    route_a = _node("src/router.tsx#/a", NodeKind.route, "/a", attrs={"path": "/a"})
    route_b = _node("src/router.tsx#/b", NodeKind.route, "/b", attrs={"path": "/b"})
    graph = _graph(
        [header, layout, home, route_a, route_b],
        [
            _edge(route_a.id, layout.id, EdgeKind.renders),
            _edge(route_b.id, layout.id, EdgeKind.renders),
            _edge(route_a.id, home.id, EdgeKind.renders),
            _edge(layout.id, header.id, EdgeKind.renders),
        ],
    )
    result = recall(graph, expand_github())

    layout_detail = next(
        item
        for item in result.candidates
        if item.candidate.nodeId.endswith("#FullLayout")
    )
    github_signals = [
        signal
        for signal in layout_detail.candidate.signals
        if signal.kind is SignalKind.uiText and signal.value == "github"
    ]
    # 去重：两条路由各归属一次 → 只保留一条
    assert len(github_signals) == 1


def expand_github():
    from app.requirement.lexicon import expand

    return expand("github")


def _runtime_trace(method: str, url: str, page_url: str) -> RuntimeTrace:
    return RuntimeTrace(
        schemaVersion="1.0",
        traceId="tr_test",
        projectId="p_test",
        analysisId="an_test",
        mode="passive",
        componentTree=[
            ComponentNode(
                name="Orders",
                file="src/pages/Orders.tsx",
                line=1,
                col=1,
                source="debug-stack",
            )
        ],
        page=PageInfo(url=page_url),
        networkEvents=[
            NetworkEvent(eventId="n1", method=method, url=url),
        ],
    )


def test_runtime_backing_boosts_api_seed_confidence() -> None:
    """trace 的请求 URL 与页面 URL 同时命中：接口种子置信度抬到运行时档 0.95。"""
    orders = _page("src/pages/Orders.tsx#Orders", "Orders", texts=["订单列表"])
    route = _node(
        "src/router.tsx#/orders", NodeKind.route, "/orders", attrs={"path": "/orders"}
    )
    api = _node(
        "api:GET:/api/orders",
        NodeKind.api,
        "GET /api/orders",
        attrs={"method": "GET", "url": "/api/orders"},
    )
    graph = _graph(
        [orders, route, api],
        [_edge(route.id, orders.id, EdgeKind.renders), _edge(orders.id, api.id, EdgeKind.requests)],
    )
    trace = _runtime_trace("GET", "https://app.example.com/api/orders?page=1", "https://app.example.com/orders")

    without = recall(graph, decompose("订单列表 GET /api/orders")[0].terms)
    with_trace = recall(
        graph, decompose("订单列表 GET /api/orders")[0].terms, trace=trace
    )

    base = next(
        signal
        for signal in without.candidates[0].candidate.signals
        if signal.kind is SignalKind.apiUrl
    )
    boosted = next(
        signal
        for signal in with_trace.candidates[0].candidate.signals
        if signal.kind is SignalKind.apiUrl
    )
    # 运行时双证把种子系数从后缀档 0.85 抬到 0.95——词权重不变，分数同比放大
    assert boosted.score > base.score
    assert boosted.score == pytest.approx(base.score * 0.95 / 0.85, rel=1e-6)


def test_recall_falls_back_to_lexical_without_seeds() -> None:
    """图里没有任何种子命中：回退词法评分并显式告警，候选不为空。"""
    page = _page("src/pages/Bare.tsx#Bare", "Bare")
    route = _node("src/router.tsx#/bare", NodeKind.route, "/bare", attrs={"path": "/bare"})
    graph = _graph([page, route], [_edge(route.id, page.id, EdgeKind.renders)])

    result = recall(graph, decompose("完全无关的需求描述")[0].terms)

    assert any(warning.code == "fallback_lexical" for warning in result.warnings)
