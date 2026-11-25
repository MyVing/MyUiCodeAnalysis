"""规格装配测试共用的图夹具：模仿《2.2 转账》样本的最小页面。

Transfer 页面：付款账户下拉（数据要素，change 触发余额查询）、可用余额
展示文案、转账按钮（点击发起 POST /api/transfer）、一个无交互容器。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.contracts import EdgeKind, IRGraph, NodeKind, Source
from app.contracts.requirement import (
    Feature,
    Match,
    RequirementMap,
    Subgraph,
    SubgraphCounts,
)
from app.requirement.chains import derive_chains
from tests.graph_fixture import edge, graph, node, page


def _element(
    element_id: str,
    tag: str,
    name: str,
    *,
    text: str | None = None,
    view_role: str = "control",
    attrs: dict | None = None,
):
    extra = dict(attrs or {})
    if text is not None:
        extra["viewText"] = text
    return node(
        element_id,
        NodeKind.element,
        name,
        file="src/pages/Transfer.tsx",
        attrs={"tag": tag, "viewRole": view_role, "target": "host", **extra},
    )


def spec_graph(analysis_id: str = "an_spec") -> IRGraph:
    transfer_page = page(
        "src/pages/Transfer.tsx#Transfer",
        "Transfer",
        texts=["转账", "请选择付款账户"],
    )
    acct_select = _element(
        "src/pages/Transfer.tsx#Transfer/jsx:select",
        "select",
        "<select> 付款账户",
        text="付款账户",
        attrs={
            "events": [
                {
                    "name": "onChange",
                    "handlerName": "handleAcctChange",
                    "handler": "function handleAcctChange(value) { queryBalance(value); }",
                    "kind": "inline",
                    "targets": [{"id": "src/pages/Transfer.tsx#handleAcctChange"}],
                    "injected": False,
                }
            ]
        },
    )
    handle_acct_change = node(
        "src/pages/Transfer.tsx#handleAcctChange",
        NodeKind.fn,
        "handleAcctChange",
        file="src/pages/Transfer.tsx",
    )
    balance_api = node(
        "api:GET:/api/account/queryBalance",
        NodeKind.api,
        "GET /api/account/queryBalance",
        attrs={"method": "GET", "url": "/api/account/queryBalance"},
    )
    balance_text = _element(
        "src/pages/Transfer.tsx#Transfer/jsx:span.balance",
        "span",
        "<span> 可用余额",
        text="可用余额",
        view_role="text",
    )
    transfer_btn = _element(
        "src/pages/Transfer.tsx#Transfer/jsx:button",
        "button",
        "<button> 转账",
        text="转账",
        attrs={
            "events": [
                {
                    "name": "onClick",
                    "handlerName": "handleTransfer",
                    "handler": "function handleTransfer(form) { transfer(form); }",
                    "kind": "inline",
                    "targets": [{"id": "src/pages/Transfer.tsx#handleTransfer"}],
                    "injected": False,
                }
            ]
        },
    )
    handle_transfer = node(
        "src/pages/Transfer.tsx#handleTransfer",
        NodeKind.fn,
        "handleTransfer",
        file="src/pages/Transfer.tsx",
    )
    transfer_api = node(
        "api:POST:/api/transfer",
        NodeKind.api,
        "POST /api/transfer",
        attrs={"method": "POST", "url": "/api/transfer"},
    )
    # 无交互容器：不进要素清单
    container = _element(
        "src/pages/Transfer.tsx#Transfer/jsx:div",
        "div",
        "<div>",
        view_role="container",
    )
    route = node(
        "src/router.tsx#/transfer",
        NodeKind.route,
        "/transfer",
        file="src/router.tsx",
        attrs={"path": "/transfer"},
    )

    return graph(
        [
            transfer_page,
            acct_select,
            handle_acct_change,
            balance_api,
            balance_text,
            transfer_btn,
            handle_transfer,
            transfer_api,
            container,
            route,
        ],
        [
            edge(route.id, transfer_page.id, EdgeKind.renders),
            edge(transfer_page.id, acct_select.id, EdgeKind.contains),
            edge(transfer_page.id, balance_text.id, EdgeKind.contains),
            edge(transfer_page.id, transfer_btn.id, EdgeKind.contains),
            edge(transfer_page.id, container.id, EdgeKind.contains),
            edge(acct_select.id, handle_acct_change.id, EdgeKind.calls),
            edge(handle_acct_change.id, balance_api.id, EdgeKind.requests),
            edge(transfer_btn.id, handle_transfer.id, EdgeKind.calls),
            edge(handle_transfer.id, transfer_api.id, EdgeKind.requests),
        ],
        analysis_id=analysis_id,
    )


def _subgraph(graph_: IRGraph, node_ids: list[str]) -> Subgraph:
    return Subgraph(
        nodeIds=node_ids,
        edgeIds=[
            item.id
            for item in graph_.edges
            if item.from_ in node_ids and item.to in node_ids
        ],
        counts=SubgraphCounts(
            component=1, hook=0, state=0, api=2, event=0, fn=2, route=1, element=3
        ),
    )


def spec_requirement_map(graph_: IRGraph, requirement_id: str = "req_spec") -> RequirementMap:
    """f1（转账）锚定到转账按钮的链路；f2（余额查询）页面级命中。"""
    index = derive_chains(graph_)
    button_chain = next(
        chain
        for chain in index.for_page("src/pages/Transfer.tsx#Transfer")
        if chain.element.node_id.endswith("jsx:button")
    )
    return RequirementMap(
        schemaVersion="1.2",
        requirementId=requirement_id,
        projectId="p_spec",
        analysisId=graph_.analysisId,
        requirement="实现转账功能",
        createdAt=datetime.now(UTC),
        dryRun=False,
        features=[
            Feature(id="f1", text="发起转账", keywords=["转账"]),
            Feature(id="f2", text="查询账户余额", keywords=["余额"]),
        ],
        matches=[
            Match(
                featureId="f1",
                pageNodeId="src/pages/Transfer.tsx#Transfer",
                chainId=button_chain.id,
                routeId="src/router.tsx#/transfer",
                confidence=0.95,
                quote="转账",
                subgraph=_subgraph(
                    graph_,
                    [
                        "src/pages/Transfer.tsx#Transfer",
                        "src/pages/Transfer.tsx#Transfer/jsx:button",
                        "src/pages/Transfer.tsx#handleTransfer",
                        "api:POST:/api/transfer",
                    ],
                ),
                source=Source.static,
            ),
            Match(
                featureId="f2",
                pageNodeId="src/pages/Transfer.tsx#Transfer",
                chainId=next(
                    chain.id for chain in index.for_page("src/pages/Transfer.tsx#Transfer")
                    if chain.element.node_id.endswith("jsx:select")
                ),
                routeId="src/router.tsx#/transfer",
                confidence=0.6,
                quote="付款账户",
                subgraph=_subgraph(graph_, ["src/pages/Transfer.tsx#Transfer"]),
                source=Source.ai,
            ),
        ],
    )
