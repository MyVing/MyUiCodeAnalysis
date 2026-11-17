"""需求定位测试共用的图夹具：两个页面 + 两个路由，模仿真实样本仓的形状。"""

from __future__ import annotations

from app.contracts import EdgeKind, Evidence, IREdge, IRGraph, IRNode, Location, NodeKind, Source

_STATIC = Source.static


def evidence(file: str, line: int = 1) -> Evidence:
    return Evidence(source=_STATIC, confidence=1.0, file=file, loc=Location(line=line, col=1))


def node(
    node_id: str,
    kind: NodeKind,
    name: str,
    *,
    file: str | None = None,
    attrs: dict | None = None,
) -> IRNode:
    return IRNode(
        id=node_id,
        kind=kind,
        name=name,
        file=file,
        loc=Location(line=1, col=1) if file else None,
        attrs=attrs or {},
        evidence=[evidence(file or "src/unknown.tsx")],
    )


def edge(source: str, target: str, kind: EdgeKind) -> IREdge:
    return IREdge(
        id=f"{source}->{target}",
        from_=source,
        to=target,
        kind=kind,
        evidence=[evidence("src/x.tsx")],
    )


def page(
    node_id: str,
    name: str,
    *,
    texts: list[str] | None = None,
    props: list[tuple[str, str | None]] | None = None,
) -> IRNode:
    attrs: dict = {}
    if texts is not None:
        attrs["uiText"] = {
            "texts": [
                {"text": text, "kind": "text", "via": None, "loc": {"line": 1, "col": 1}}
                for text in texts
            ]
        }
    if props is not None:
        attrs["props"] = {
            "typeName": None,
            "typeFile": None,
            "generic": False,
            "fields": [
                {
                    "name": prop,
                    "type": "string",
                    "optional": False,
                    "defaultValue": None,
                    "description": description,
                    "file": "src/types.ts",
                    "loc": {"line": 1, "col": 1},
                }
                for prop, description in props
            ],
        }
    return node(node_id, NodeKind.component, name, file=f"src/pages/{name}.tsx", attrs=attrs)


def graph(nodes: list[IRNode], edges: list[IREdge], analysis_id: str = "an_test") -> IRGraph:
    return IRGraph(version="1.1", analysisId=analysis_id, nodes=nodes, edges=edges)


def user_graph(analysis_id: str = "an_test") -> IRGraph:
    """UserList（/users）与 UserDetail（/users/:userId）两张页面。"""
    user_list = page(
        "src/pages/UserList.tsx#UserList",
        "UserList",
        texts=["加载中…", "搜索用户", "姓名", "邮箱"],
        props=[
            ("title", "列表标题"),
            ("showFilter", "是否显示筛选栏"),
            ("onSelect", "选中回调，由页面注入"),
        ],
    )
    user_detail = page(
        "src/pages/UserDetail.tsx#UserDetail",
        "UserDetail",
        texts=["用户详情", "编号", "状态", "待审核"],
        props=[("userId", None)],
    )
    table_card = page("src/components/TableCard.tsx#TableCard", "TableCard", texts=["查看"])
    fetch_users = node(
        "src/pages/UserList.tsx#fetchUsers",
        NodeKind.fn,
        "fetchUsers",
        file="src/pages/UserList.tsx",
    )
    api = node(
        "api:GET:/api/users",
        NodeKind.api,
        "GET /api/users",
        attrs={"method": "GET", "url": "/api/users"},
    )
    route_list = node(
        "src/router.tsx#/users",
        NodeKind.route,
        "/users",
        file="src/router.tsx",
        attrs={"path": "/users"},
    )
    route_detail = node(
        "src/router.tsx#/users/:userId",
        NodeKind.route,
        "/users/:userId",
        file="src/router.tsx",
        attrs={"path": "/users/:userId", "params": ["userId"]},
    )

    return graph(
        [user_list, user_detail, table_card, fetch_users, api, route_list, route_detail],
        [
            edge(route_list.id, user_list.id, EdgeKind.renders),
            edge(route_detail.id, user_detail.id, EdgeKind.renders),
            edge(user_list.id, table_card.id, EdgeKind.renders),
            edge(user_list.id, fetch_users.id, EdgeKind.calls),
            edge(fetch_users.id, api.id, EdgeKind.requests),
        ],
        analysis_id=analysis_id,
    )
