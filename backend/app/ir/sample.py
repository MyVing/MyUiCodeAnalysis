"""示例 IR 图：用于打通前端图视图，明确标记为 sample，不冒充真实分析结果。"""

from datetime import UTC, datetime

from app.contracts import (
    EdgeKind,
    Evidence,
    Framework,
    GraphMeta,
    IREdge,
    IRGraph,
    IRNode,
    Location,
    NodeKind,
    Source,
)
from app.schemas import Project

SCHEMA_VERSION = "1.1"
SAMPLE_ANALYSIS_ID = "an_sample"
PAGE_FILE = "app/orders/page.tsx"
TABLE_FILE = "src/components/OrderTable.tsx"
HOOK_FILE = "src/hooks/useOrderFilter.ts"
API_FILE = "src/services/orderApi.ts"
ACTION_FILE = "app/orders/actions.ts"


def _static(file: str, line: int, confidence: float = 1.0) -> Evidence:
    return Evidence(
        source=Source.static,
        confidence=confidence,
        file=file,
        loc=Location(line=line, col=1),
    )


def _runtime(trace_id: str) -> Evidence:
    return Evidence(source=Source.runtime, confidence=1.0, traceId=trace_id)


def _ai(confidence: float = 0.55) -> Evidence:
    return Evidence(source=Source.ai, confidence=confidence)


def build_sample_graph(project: Project) -> IRGraph:
    is_next = project.framework == "next"
    page_kind = NodeKind.server_component if is_next else NodeKind.component

    orders_page = IRNode(
        id=f"{PAGE_FILE}#OrdersPage",
        kind=page_kind,
        name="OrdersPage",
        file=PAGE_FILE,
        loc=Location(line=12, col=1),
        attrs={
            "directive": "server" if is_next else "client",
            "props": [],
        },
        evidence=[_static(PAGE_FILE, 12)],
    )
    order_table = IRNode(
        id=f"{TABLE_FILE}#OrderTable",
        kind=NodeKind.component,
        name="OrderTable",
        file=TABLE_FILE,
        loc=Location(line=12, col=1),
        attrs={"directive": "use client", "props": ["orders", "onSelect"]},
        evidence=[_static(TABLE_FILE, 12)],
    )
    order_row = IRNode(
        id=f"{TABLE_FILE}#OrderRow",
        kind=NodeKind.component,
        name="OrderRow",
        file=TABLE_FILE,
        loc=Location(line=48, col=1),
        attrs={"props": ["order", "onSelect"]},
        evidence=[_static(TABLE_FILE, 48)],
    )
    selected_order_id = IRNode(
        id=f"{TABLE_FILE}#selectedOrderId",
        kind=NodeKind.state,
        name="selectedOrderId",
        file=TABLE_FILE,
        loc=Location(line=16, col=1),
        attrs={"hook": "useState", "initial": None},
        evidence=[_static(TABLE_FILE, 16)],
    )
    order_filter = IRNode(
        id=f"{HOOK_FILE}#useOrderFilter",
        kind=NodeKind.hook,
        name="useOrderFilter",
        file=HOOK_FILE,
        loc=Location(line=5, col=1),
        attrs={"returns": ["keyword", "setKeyword"]},
        evidence=[_static(HOOK_FILE, 5)],
    )
    fetch_orders = IRNode(
        id=f"{API_FILE}#fetchOrders",
        kind=NodeKind.fn,
        name="fetchOrders",
        file=API_FILE,
        loc=Location(line=9, col=1),
        attrs={"method": "GET", "url": "/api/orders"},
        evidence=[_static(API_FILE, 9)],
    )
    api_node = IRNode(
        id="api:GET /api/orders",
        kind=NodeKind.api,
        name="GET /api/orders",
        # api 节点是对外接口，不对应单一源码位置；file 缺省，证据里保留调用点
        attrs={"method": "GET", "path": "/api/orders"},
        evidence=[
            _static(API_FILE, 12, confidence=0.9),
            _runtime("trc_orders_20261116"),
        ],
    )

    nodes = [
        orders_page,
        order_table,
        order_row,
        selected_order_id,
        order_filter,
        fetch_orders,
        api_node,
    ]
    edges = [
        IREdge(
            id=f"{TABLE_FILE}#OrderTable->{TABLE_FILE}#OrderRow",
            from_=order_table.id,
            to=order_row.id,
            kind=EdgeKind.renders,
            evidence=[_static(TABLE_FILE, 24)],
        ),
        IREdge(
            id=f"{TABLE_FILE}#OrderTable->{TABLE_FILE}#selectedOrderId",
            from_=order_table.id,
            to=selected_order_id.id,
            kind=EdgeKind.writes,
            evidence=[_static(TABLE_FILE, 18, confidence=0.95)],
        ),
        IREdge(
            id=f"{TABLE_FILE}#OrderTable->{HOOK_FILE}#useOrderFilter",
            from_=order_table.id,
            to=order_filter.id,
            kind=EdgeKind.uses,
            evidence=[_static(TABLE_FILE, 14)],
        ),
        IREdge(
            id=f"{PAGE_FILE}#OrdersPage->{API_FILE}#fetchOrders",
            from_=orders_page.id,
            to=fetch_orders.id,
            kind=EdgeKind.calls,
            evidence=[_static(PAGE_FILE, 20, confidence=0.9)],
        ),
        IREdge(
            id=f"{API_FILE}#fetchOrders->api:GET /api/orders",
            from_=fetch_orders.id,
            to=api_node.id,
            kind=EdgeKind.requests,
            evidence=[_runtime("trc_orders_20261116"), _static(API_FILE, 10, confidence=0.9)],
        ),
    ]

    if is_next:
        route = IRNode(
            id=f"{PAGE_FILE}#/orders",
            kind=NodeKind.route,
            name="/orders",
            file=PAGE_FILE,
            loc=Location(line=1, col=1),
            attrs={"path": "/orders"},
            evidence=[_static(PAGE_FILE, 1)],
        )
        cancel_order = IRNode(
            id=f"{ACTION_FILE}#cancelOrder",
            kind=NodeKind.server_action,
            name="cancelOrder",
            file=ACTION_FILE,
            loc=Location(line=6, col=1),
            attrs={"formAction": True},
            evidence=[_static(ACTION_FILE, 6)],
        )
        nodes = [route, *nodes, cancel_order]
        edges = [
            IREdge(
                id=f"{PAGE_FILE}#/orders->{PAGE_FILE}#OrdersPage",
                from_=route.id,
                to=orders_page.id,
                kind=EdgeKind.renders,
                evidence=[_static(PAGE_FILE, 12)],
            ),
            *edges,
            IREdge(
                id=f"{PAGE_FILE}#OrdersPage->{TABLE_FILE}#OrderTable",
                from_=orders_page.id,
                to=order_table.id,
                kind=EdgeKind.boundary,
                evidence=[_static(PAGE_FILE, 28), _static(TABLE_FILE, 1)],
            ),
            IREdge(
                id=f"{TABLE_FILE}#OrderTable->{ACTION_FILE}#cancelOrder",
                from_=order_table.id,
                to=cancel_order.id,
                kind=EdgeKind.calls,
                evidence=[_ai(0.55)],
            ),
        ]
    else:
        edges = [
            IREdge(
                id=f"{PAGE_FILE}#OrdersPage->{TABLE_FILE}#OrderTable",
                from_=orders_page.id,
                to=order_table.id,
                kind=EdgeKind.renders,
                evidence=[_static(PAGE_FILE, 22)],
            ),
            *edges,
        ]

    return IRGraph(
        version=SCHEMA_VERSION,
        analysisId=SAMPLE_ANALYSIS_ID,
        nodes=nodes,
        edges=edges,
        warnings=[],
        meta=GraphMeta(
            projectId=project.id,
            framework=Framework(project.framework),
            sample=True,
            generatedAt=datetime.now(UTC),
            note="示例数据：用于验证前端图视图，不代表真实源码分析结果。",
        ),
    )
