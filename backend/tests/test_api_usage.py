"""接口 → 页面反向检索（6.11）的行为锁定。

归一化必须与 ast-worker 的 normalizeUrlText 同规则（两边不共享代码，靠测试
对齐）；反查的每一环都必须是图上真实存在的边，置信度沿路径取最小值再乘锚点。
"""

from __future__ import annotations

import pytest

from app.contracts import EdgeKind, IRGraph, NodeKind, RuntimeTrace
from app.contracts.runtime import NetworkEvent, PageInfo
from app.ir.api_usage import ApiUsageIndex, apply_runtime_backing, normalize_url
from tests.graph_fixture import edge, graph, node, page

# ---------------------------------------------------------------------------
# URL 归一化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/api/orders", "/api/orders"),
        ("https://x.com/api/orders?a=1#/list", "/api/orders"),
        ("//api//orders/", "/api/orders"),
        ("/API/Orders", "/api/orders"),
        ("/users/123", "/users/:param"),
        ("/users/550e8400-e29b-41d4-a716-446655440000", "/users/:param"),
        ("/users/:id", "/users/:param"),
        ("/users/{id}", "/users/:param"),
        ("/users/${id}", "/users/:param"),
        ("/orders?status=1", "/orders"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "https://x.com", "https://x.com?a=1"])
def test_normalize_url_without_path_is_none(raw: str) -> None:
    assert normalize_url(raw) is None


# ---------------------------------------------------------------------------
# 反查
# ---------------------------------------------------------------------------


def _user_graph_with_hook() -> IRGraph:
    """在基础图上补一条 `页面 --uses--> hook --requests--> api` 的链路。"""
    base = _base_graph()
    hook = node(
        "src/hooks/useUsers.ts#useUsers",
        NodeKind.hook,
        "useUsers",
        file="src/hooks/useUsers.ts",
    )
    return graph(
        [*base.nodes, hook],
        [
            *base.edges,
            edge("src/pages/UserList.tsx#UserList", hook.id, EdgeKind.uses),
            edge(hook.id, "api:GET:/api/users", EdgeKind.requests),
        ],
    )


def _base_graph() -> IRGraph:
    """route --renders--> UserList --calls--> fetchUsers --requests--> GET /api/users。"""
    return graph(
        [
            page("src/pages/UserList.tsx#UserList", "UserList"),
            node(
                "src/pages/UserList.tsx#fetchUsers",
                NodeKind.fn,
                "fetchUsers",
                file="src/pages/UserList.tsx",
            ),
            node(
                "api:GET:/api/users",
                NodeKind.api,
                "GET /api/users",
                attrs={"method": "GET", "url": "/api/users"},
            ),
            node(
                "src/router.tsx#/users",
                NodeKind.route,
                "/users",
                file="src/router.tsx",
                attrs={"path": "/users"},
            ),
        ],
        [
            edge("src/router.tsx#/users", "src/pages/UserList.tsx#UserList", EdgeKind.renders),
            edge(
                "src/pages/UserList.tsx#UserList",
                "src/pages/UserList.tsx#fetchUsers",
                EdgeKind.calls,
            ),
            edge("src/pages/UserList.tsx#fetchUsers", "api:GET:/api/users", EdgeKind.requests),
        ],
    )


def test_lookup_walks_calls_to_page() -> None:
    result = ApiUsageIndex(_base_graph()).lookup(method="GET", url="/api/users", match="exact")

    assert not result.warnings
    assert len(result.pages) == 1
    hit = result.pages[0]
    assert hit.page_node_id == "src/pages/UserList.tsx#UserList"
    assert hit.route_node_id == "src/router.tsx#/users"
    # api → 调用者 → 页面 → 路由：每一环都是图上的边
    assert hit.path == (
        "api:GET:/api/users",
        "src/pages/UserList.tsx#fetchUsers",
        "src/pages/UserList.tsx#UserList",
        "src/router.tsx#/users",
    )
    assert hit.edge_path == (
        "src/pages/UserList.tsx#fetchUsers->api:GET:/api/users",
        "src/pages/UserList.tsx#UserList->src/pages/UserList.tsx#fetchUsers",
        "src/router.tsx#/users->src/pages/UserList.tsx#UserList",
    )
    assert hit.confidence == pytest.approx(0.95)
    assert hit.url_suffix_match is False


def test_lookup_also_reaches_page_through_hook_uses_edge() -> None:
    """hook 的请求归属修复后 `页面 --uses--> hook --requests--> api` 是真实链路。"""
    result = ApiUsageIndex(_user_graph_with_hook()).lookup(
        method="GET", url="/api/users", match="exact"
    )

    pages = {hit.page_node_id for hit in result.pages}
    assert pages == {"src/pages/UserList.tsx#UserList"}
    # 同一 (页面, 路由) 只留一条：calls 路径与 uses 路径合并，置信度取最高
    assert len(result.pages) == 1


def test_lookup_without_method_is_suffix_anchored() -> None:
    result = ApiUsageIndex(_base_graph()).lookup(method=None, url="/api/users", match="exact")

    assert result.pages
    assert all(hit.url_suffix_match for hit in result.pages)
    assert all(hit.confidence == pytest.approx(0.85) for hit in result.pages)


def test_lookup_method_mismatch_does_not_match() -> None:
    """GET /api/orders 与 POST /api/orders 永远是两个接口。"""
    result = ApiUsageIndex(_base_graph()).lookup(method="POST", url="/api/users", match="exact")

    assert result.pages == ()
    assert [warning.code for warning in result.warnings] == ["api_not_found"]


def test_lookup_suffix_matches_prefix_unresolved_url() -> None:
    """需求写 /api/users，代码里只有 /users（前缀常量没解析出来）→ 后缀命中并标注。"""
    api = node(
        "api:GET:/users",
        NodeKind.api,
        "GET /users",
        attrs={"method": "GET", "url": "/users"},
    )
    base = _base_graph()
    g = graph(
        [*base.nodes, api],
        [*base.edges, edge("src/pages/UserList.tsx#fetchUsers", api.id, EdgeKind.requests)],
    )

    result = ApiUsageIndex(g).lookup(method="GET", url="/api/users", match="suffix")

    assert not result.warnings
    assert {hit.page_node_id for hit in result.pages} == {"src/pages/UserList.tsx#UserList"}
    assert all(hit.url_suffix_match for hit in result.pages)
    assert all(hit.confidence == pytest.approx(0.85) for hit in result.pages)


def test_suffix_match_requires_segment_boundary() -> None:
    """/users 不能匹配 /myusers：后缀必须落在段边界上。"""
    api = node(
        "api:GET:/myusers",
        NodeKind.api,
        "GET /myusers",
        attrs={"method": "GET", "url": "/myusers"},
    )
    g = graph(
        [
            page("src/pages/UserList.tsx#UserList", "UserList"),
            node(
                "src/router.tsx#/users",
                NodeKind.route,
                "/users",
                file="src/router.tsx",
                attrs={"path": "/users"},
            ),
            api,
        ],
        [
            edge("src/router.tsx#/users", "src/pages/UserList.tsx#UserList", EdgeKind.renders),
            edge("src/pages/UserList.tsx#UserList", api.id, EdgeKind.requests),
        ],
    )

    result = ApiUsageIndex(g).lookup(method="GET", url="/api/users", match="suffix")

    assert result.pages == ()
    assert result.warnings[0].code == "api_not_found"


def test_dead_api_warns_when_no_page_reachable() -> None:
    """接口存在但没有页面可达 = 死接口线索，必须显式告警而不是返回空列表了事。"""
    api = node(
        "api:GET:/api/legacy",
        NodeKind.api,
        "GET /api/legacy",
        attrs={"method": "GET", "url": "/api/legacy"},
    )
    base = _base_graph()
    g = graph([*base.nodes, api], base.edges)

    result = ApiUsageIndex(g).lookup(method="GET", url="/api/legacy", match="exact")

    assert result.pages == ()
    assert [warning.code for warning in result.warnings] == ["api_no_page_reachable"]


def test_ambiguous_usage_warns_over_three_pages() -> None:
    """同一接口被 4 个页面调用：返回全部并记 api_usage_ambiguous，不自动聚焦。"""
    nodes: list = []
    edges: list = []
    for index in range(4):
        page_id = f"src/pages/Page{index}.tsx#Page{index}"
        route_id = f"src/router.tsx#/p{index}"
        nodes.append(page(page_id, f"Page{index}"))
        nodes.append(
            node(
                route_id,
                NodeKind.route,
                f"/p{index}",
                file="src/router.tsx",
                attrs={"path": f"/p{index}"},
            )
        )
        edges.append(edge(route_id, page_id, EdgeKind.renders))
        edges.append(edge(page_id, "api:GET:/api/common", EdgeKind.requests))
    nodes.append(
        node(
            "api:GET:/api/common",
            NodeKind.api,
            "GET /api/common",
            attrs={"method": "GET", "url": "/api/common"},
        )
    )

    result = ApiUsageIndex(graph(nodes, edges)).lookup(
        method="GET", url="/api/common", match="exact"
    )

    assert len(result.pages) == 4
    assert result.warnings[0].code == "api_usage_ambiguous"


# ---------------------------------------------------------------------------
# 运行时双证
# ---------------------------------------------------------------------------


def _trace(
    page_route: str,
    *,
    request_url: str = "http://localhost:3000/api/users",
) -> RuntimeTrace:
    return RuntimeTrace(
        schemaVersion="1.0",
        traceId="trc_test",
        projectId="p1",
        mode="passive",
        page=PageInfo(url=f"http://localhost:3000{page_route}", route=page_route),
        networkEvents=[
            NetworkEvent(eventId="e1", method="GET", url=request_url),
        ],
    )


def test_runtime_backing_bumps_confidence_and_flags() -> None:
    pages = ApiUsageIndex(_base_graph()).lookup(method="GET", url="/api/users", match="exact").pages
    route_paths = {"src/pages/UserList.tsx#UserList": "/users"}

    result = apply_runtime_backing(
        pages, route_paths, method="GET", url="/api/users", trace=_trace("/users")
    )

    assert result[0].runtime_backed is True
    assert result[0].confidence == pytest.approx(0.95)


def test_runtime_backing_ignores_unrelated_trace() -> None:
    pages = ApiUsageIndex(_base_graph()).lookup(method="GET", url="/api/users", match="exact").pages

    # trace 只在别的页面采集、没发过这个请求：不强化
    result = apply_runtime_backing(
        pages,
        {"src/pages/UserList.tsx#UserList": "/users"},
        method="GET",
        url="/api/users",
        trace=_trace("/orders"),
    )

    assert all(not hit.runtime_backed for hit in result)


def test_runtime_backing_matches_dynamic_route_via_normalization() -> None:
    """trace 页面 /users/123 与路由 /users/:userId 归一化后同键。"""
    detail = page("src/pages/UserDetail.tsx#UserDetail", "UserDetail")
    route = node(
        "src/router.tsx#/users/:userId",
        NodeKind.route,
        "/users/:userId",
        file="src/router.tsx",
        attrs={"path": "/users/:userId"},
    )
    api = node(
        "api:GET:/api/users/:id",
        NodeKind.api,
        "GET /api/users/:id",
        attrs={"method": "GET", "url": "/api/users/:id"},
    )
    g = graph(
        [detail, route, api],
        [
            edge(route.id, detail.id, EdgeKind.renders),
            edge(detail.id, api.id, EdgeKind.requests),
        ],
    )
    pages = ApiUsageIndex(g).lookup(method="GET", url="/api/users/123", match="exact").pages

    assert pages, "数字段归一化后 /api/users/123 与 /api/users/:id 同键"
    result = apply_runtime_backing(
        pages,
        {detail.id: "/users/:userId"},
        method="GET",
        url="/api/users/123",
        trace=_trace("/users/123", request_url="http://localhost:3000/api/users/123"),
    )
    assert result[0].runtime_backed is True
    assert result[0].page_node_id == detail.id


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def test_api_usage_endpoint_falls_back_to_sample_graph(client, project) -> None:
    """没有真实分析产物时回退示例图，但必须以 sample_graph 告警显式告知。"""
    response = client.get(
        f"/api/v1/projects/{project['id']}/api-usage",
        params={"method": "GET", "url": "/api/orders"},
    )

    assert response.status_code == 200
    body = response.json()
    assert any(
        item["pageNodeId"] == "app/orders/page.tsx#OrdersPage" for item in body["pages"]
    )
    assert any(item["code"] == "sample_graph" for item in body["warnings"])
    assert all(item["confidence"] > 0 for item in body["pages"])
    # 命中必须给出可回图的完整路径
    assert all(item["path"][0].startswith("api:") for item in body["pages"])


def test_api_usage_endpoint_requires_existing_project(client) -> None:
    response = client.get(
        "/api/v1/projects/nonexistent/api-usage", params={"url": "/api/orders"}
    )

    assert response.status_code == 404


def test_api_usage_endpoint_validates_url_param(client, project) -> None:
    response = client.get(f"/api/v1/projects/{project['id']}/api-usage")

    assert response.status_code == 422
