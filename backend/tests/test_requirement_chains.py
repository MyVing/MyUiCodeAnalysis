"""链路推导测试：确定性、可复现、断点如实标注。

这里不碰模型——链路必须由确定性图遍历产出（见 `chains.py` 模块文档）。
验证重点：起点只能是元素、三段（元素/处理函数/效应）能被串起来、
外链断点被标出来、同一份图重复推导结果一致。
"""

from __future__ import annotations

from app.contracts import EdgeKind, NodeKind
from app.requirement.chains import (
    MAX_CHAINS_PER_PAGE,
    TERMINATED_EXTERNAL,
    TERMINATED_INJECTED_HANDLER,
    derive_chains,
)
from tests.graph_fixture import edge, graph, node, page


def _element(
    element_id: str,
    tag: str,
    *,
    file: str = "src/pages/OrderList.tsx",
    text: str | None = None,
    view_role: str = "control",
    attrs: dict | None = None,
) -> object:
    extra = dict(attrs or {})
    if text is not None:
        extra["viewText"] = text
    return node(
        element_id,
        NodeKind.element,
        f"<{tag}> {text}" if text else f"<{tag}>",
        file=file,
        attrs={"tag": tag, "viewRole": view_role, "target": "host", **extra},
    )


def _route(route_id: str, path: str) -> object:
    return node(route_id, NodeKind.route, path, file="src/router.tsx", attrs={"path": path})


def _order_graph():
    """OrderList 页面：一个导出按钮（→ handler → api）+ 一个外链。"""
    order_list = page("src/pages/OrderList.tsx#OrderList", "OrderList", texts=["订单列表"])
    export_btn = _element(
        "src/pages/OrderList.tsx#OrderList/jsx:button",
        "button",
        text="导出",
    )
    handle_export = node(
        "src/pages/OrderList.tsx#handleExport",
        NodeKind.fn,
        "handleExport",
        file="src/pages/OrderList.tsx",
    )
    export_api = node(
        "api:GET:/api/orders/export",
        NodeKind.api,
        "GET /api/orders/export",
        attrs={"method": "GET", "url": "/api/orders/export"},
    )
    github_link = _element(
        "src/pages/OrderList.tsx#OrderList/jsx:a",
        "a",
        text="GitHub",
        view_role="link",
        attrs={"href": "https://github.com/x/y", "external": True},
    )
    route = _route("src/router.tsx#/orders", "/orders")

    return graph(
        [order_list, export_btn, handle_export, export_api, github_link, route],
        [
            edge(route.id, order_list.id, EdgeKind.renders),
            edge(order_list.id, export_btn.id, EdgeKind.contains),
            edge(order_list.id, github_link.id, EdgeKind.contains),
            edge(export_btn.id, handle_export.id, EdgeKind.calls),
            edge(handle_export.id, export_api.id, EdgeKind.requests),
        ],
    )


def test_chain_links_element_handler_and_api() -> None:
    """三段必须串成一条：按钮 → handleExport → GET /api/orders/export。"""
    index = derive_chains(_order_graph())
    chains = index.for_page("src/pages/OrderList.tsx#OrderList")

    button_chain = next(
        chain for chain in chains if chain.element.node_id.endswith("jsx:button")
    )
    roles = [step.role for step in button_chain.steps]
    assert roles == ["element", "handler", "api"]

    node_ids = [step.node_id for step in button_chain.steps]
    assert node_ids[0].endswith("jsx:button")
    assert node_ids[1] == "src/pages/OrderList.tsx#handleExport"
    assert node_ids[2] == "api:GET:/api/orders/export"


def test_chain_corpus_contains_every_locatable_text() -> None:
    """链路语料是判定阶段逐字校验引文的依据，缺一段就会冤枉真实匹配。"""
    index = derive_chains(_order_graph())
    chain = next(
        chain
        for chain in index.for_page("src/pages/OrderList.tsx#OrderList")
        if chain.element.node_id.endswith("jsx:button")
    )

    assert "导出" in chain.corpus
    assert "handleExport" in chain.corpus
    assert "/api/orders/export" in chain.corpus


def test_chain_records_source_location_for_every_step() -> None:
    """链路的每一段都要能跳回源码——这是「可复核」的底线。"""
    index = derive_chains(_order_graph())
    chain = next(
        chain
        for chain in index.for_page("src/pages/OrderList.tsx#OrderList")
        if chain.element.node_id.endswith("jsx:button")
    )

    for step in chain.steps:
        assert step.file, f"{step.node_id} 缺少文件"
        assert step.line is not None, f"{step.node_id} 缺少行号"


def test_external_link_chain_is_terminated_with_reason() -> None:
    """外链没有 route 节点可连：如实标注断点，不编造站内目标。"""
    index = derive_chains(_order_graph())
    chain = next(
        chain
        for chain in index.for_page("src/pages/OrderList.tsx#OrderList")
        if chain.element.node_id.endswith("jsx:a")
    )

    assert chain.href == "https://github.com/x/y"
    assert chain.terminated_at == TERMINATED_EXTERNAL
    assert [step.role for step in chain.steps] == ["element"]
    # href 必须进语料：需求说「GitHub 链接」时要靠它命中
    assert "https://github.com/x/y" in chain.corpus
    assert "GitHub" in chain.corpus


def test_injected_handler_is_terminated_not_invented() -> None:
    """onSelect 由父组件注入时，链路在此中断——不往父组件反向展开。"""
    page_node = page("src/pages/Table.tsx#Table", "Table", texts=["表格"])
    row = _element(
        "src/pages/Table.tsx#Table/jsx:RowItem",
        "RowItem",
        file="src/pages/Table.tsx",
        view_role="control",
        attrs={
            "events": [
                {
                    "name": "onSelect",
                    "handler": "props.onSelect",
                    "kind": "other",
                    "handlerName": None,
                    "targets": [],
                }
            ]
        },
    )
    route = _route("src/router.tsx#/table", "/table")

    index = derive_chains(
        graph(
            [page_node, row, route],
            [
                edge(route.id, page_node.id, EdgeKind.renders),
                edge(page_node.id, row.id, EdgeKind.contains),
            ],
        )
    )

    chain = index.for_page("src/pages/Table.tsx#Table")[0]
    assert chain.terminated_at == TERMINATED_INJECTED_HANDLER
    assert [step.role for step in chain.steps] == ["element"]


def test_non_interactive_elements_do_not_become_chains() -> None:
    """`<h1>标题</h1>` 是展示内容，不是交互——不产链路。"""
    page_node = page("src/pages/Home.tsx#Home", "Home", texts=["首页"])
    heading = _element(
        "src/pages/Home.tsx#Home/jsx:h1",
        "h1",
        file="src/pages/Home.tsx",
        text="欢迎",
        view_role="text",
    )
    route = _route("src/router.tsx#/", "/")

    index = derive_chains(
        graph(
            [page_node, heading, route],
            [
                edge(route.id, page_node.id, EdgeKind.renders),
                edge(page_node.id, heading.id, EdgeKind.contains),
            ],
        )
    )

    assert index.for_page("src/pages/Home.tsx#Home") == []
    assert index.by_id == {}


def test_element_nested_in_element_attributes_to_its_page() -> None:
    """`<li>` 里的 `<a>` 要顺着 contains 找到真正的页面，而不是孤儿。"""
    page_node = page("src/core/Header.tsx#Header", "Header", texts=[])
    item = _element(
        "src/core/Header.tsx#Header/jsx:li",
        "li",
        file="src/core/Header.tsx",
        view_role="container",
        attrs={"tag": "li"},
    )
    link = _element(
        "src/core/Header.tsx#Header/jsx:a",
        "a",
        file="src/core/Header.tsx",
        text="Posts",
        view_role="link",
        attrs={"href": "/posts", "external": False},
    )
    route = _route("src/router.tsx#/", "/")

    index = derive_chains(
        graph(
            [page_node, item, link, route],
            [
                edge(route.id, page_node.id, EdgeKind.renders),
                edge(page_node.id, item.id, EdgeKind.contains),
                edge(item.id, link.id, EdgeKind.contains),
            ],
        )
    )

    chains = index.for_page("src/core/Header.tsx#Header")
    assert [chain.element.node_id for chain in chains] == [link.id]


def test_layout_child_elements_attribute_to_the_page_not_the_layout() -> None:
    """`FullLayout → Header → <a>`：归属是 FullLayout，不是 Header。

    Header 不是 route 目标。若停在最近组件，布局里的全部交互都会失去归属，
    用户说「页面右上角的 GitHub 链接」就永远定位不到。这与召回层
    `_collect_evidences` 对布局页面的例外是同一条判断。
    """
    full_layout = page("src/core/FullLayout.tsx#FullLayout", "FullLayout", texts=[])
    header = page("src/core/Header.tsx#Header", "Header", texts=["GitHub"])
    github = _element(
        "src/core/Header.tsx#Header/jsx:a",
        "a",
        file="src/core/Header.tsx",
        text="GitHub",
        view_role="link",
        attrs={"href": "https://github.com/x/y", "external": True},
    )
    route_home = _route("src/router.tsx#/", "/")
    route_posts = _route("src/router.tsx#/posts", "/posts")

    index = derive_chains(
        graph(
            [full_layout, header, github, route_home, route_posts],
            [
                # 同一个 layout 挂在两条路由下
                edge(route_home.id, full_layout.id, EdgeKind.renders),
                edge(route_posts.id, full_layout.id, EdgeKind.renders),
                edge(full_layout.id, header.id, EdgeKind.renders),
                edge(header.id, github.id, EdgeKind.contains),
            ],
        )
    )

    chains = index.for_page(full_layout.id)
    assert [chain.element.node_id for chain in chains] == [github.id]
    assert chains[0].terminated_at == TERMINATED_EXTERNAL


def test_chain_derivation_is_deterministic() -> None:
    """同一份图重复推导必须给出完全相同的 id 序列，否则前端选中态会抖。"""
    source = _order_graph()
    first = derive_chains(source)
    second = derive_chains(source)

    first_ids = {page_id: [c.id for c in group] for page_id, group in first.by_page.items()}
    second_ids = {page_id: [c.id for c in group] for page_id, group in second.by_page.items()}
    assert first_ids == second_ids
    assert list(first.by_id) == list(second.by_id)


def test_chain_index_allows_lookup_by_id() -> None:
    """定位阶段要按 chainId 校验模型输出，索引必须支持 O(1) 查找。"""
    index = derive_chains(_order_graph())
    some_chain = next(iter(index.by_id.values()))

    assert index.by_id[some_chain.id] is some_chain


def test_page_without_elements_yields_no_chain() -> None:
    """只有路由、没有交互元素的页面（比如纯重定向）不产链路。"""
    empty_page = page("src/pages/Redirect.tsx#Redirect", "Redirect", texts=[])
    route = _route("src/router.tsx#/old", "/old")

    index = derive_chains(
        graph([empty_page, route], [edge(route.id, empty_page.id, EdgeKind.renders)])
    )

    assert index.for_page("src/pages/Redirect.tsx#Redirect") == []


def test_chain_exports_contract_with_ordered_path() -> None:
    """契约里 nodeIds 与 steps 同序，edgeIds 是真正走过的那几条边。"""
    index = derive_chains(_order_graph())
    chain = next(
        chain
        for chain in index.for_page("src/pages/OrderList.tsx#OrderList")
        if chain.element.node_id.endswith("jsx:button")
    )

    contract = chain.to_contract()

    assert [step.nodeId for step in contract.steps] == list(chain.node_ids)
    assert contract.path.nodeIds == list(chain.node_ids)
    assert contract.element.nodeId == contract.steps[0].nodeId
    assert contract.path.edgeIds == list(chain.edge_ids)
    # 三段链路走过两条边：element→handler、handler→api
    assert len(contract.path.edgeIds) == 2
    assert contract.terminatedAt is None
    # 每一段都要有坐标，否则前端点不开
    assert all(step.file and step.loc for step in contract.steps)


def test_chain_contract_serializes_loc_for_frontend() -> None:
    """loc 是契约字段，序列化后必须是 {line, col}，列号缺省按 1 记。"""
    index = derive_chains(_order_graph())
    contract = index.contracts_for(list(index.by_id))[0]

    dumped = contract.model_dump()
    first_loc = dumped["steps"][0]["loc"]
    assert first_loc["line"] >= 1
    assert first_loc["col"] >= 1


def test_quote_attribution_points_at_the_matching_step() -> None:
    """引文落在哪一段要能算出来：前端据此高亮「需求说的是这条链的哪一步」。

    总语料只能回答「这句话在这条链里」，答不了「是哪一步」——两段都带
    `orders` 时尤其明显：`/api/orders/export` 属于接口段，不是按钮文案。
    """
    index = derive_chains(_order_graph())
    chain = next(
        chain
        for chain in index.for_page("src/pages/OrderList.tsx#OrderList")
        if chain.element.node_id.endswith("jsx:button")
    )

    # 按钮文案 → 只有起点段
    assert index.targets_of(chain.id, "导出") == [chain.element.node_id]
    # handler 名 → 只有处理函数段
    assert index.targets_of(chain.id, "handleExport") == ["src/pages/OrderList.tsx#handleExport"]
    # 接口地址 → 只有接口段
    assert index.targets_of(chain.id, "/api/orders/export") == ["api:GET:/api/orders/export"]
    # 链路里不存在的引文 → 空（调用方据此判为校验失败）
    assert index.targets_of(chain.id, "不存在的文案") == []


def test_page_corpus_is_union_of_its_chains() -> None:
    """页面级引文校验必须退到「该页面链路语料」，而不是整张图。"""
    index = derive_chains(_order_graph())
    page_id = "src/pages/OrderList.tsx#OrderList"

    corpus = index.page_corpus(page_id)
    assert "导出" in corpus
    assert "GitHub" in corpus
    assert "/api/orders/export" in corpus
    # 页面自己的组件名不是交互语料：它由召回层负责，不进链路校验范围
    assert "OrderList" not in corpus


def test_page_of_rejects_unknown_chain() -> None:
    """伪造的 chainId 取不到页面——校验阶段靠这一点判 chain_not_in_candidates。"""
    index = derive_chains(_order_graph())

    assert index.page_of("fake::chain") is None
    assert index.corpus_of("fake::chain") == ()


def test_chain_limit_is_reported_not_hidden() -> None:
    """单页链路超限时截断必须显式记录，否则用户以为这一页只有 40 个交互。"""
    page_node = page("src/pages/Busy.tsx#Busy", "Busy", texts=["繁忙页"])
    route = _route("src/router.tsx#/busy", "/busy")
    elements = [
        _element(
            f"src/pages/Busy.tsx#Busy/jsx:a#{i}", "a", file="src/pages/Busy.tsx", text=f"链接{i}"
        )
        for i in range(MAX_CHAINS_PER_PAGE + 5)
    ]
    # 每个元素带上不同的字面量 href，才各自成为一条独立链路
    for i, element in enumerate(elements):
        element.attrs["href"] = f"https://example.com/{i}"
        element.attrs["external"] = True

    index = derive_chains(
        graph(
            [page_node, route, *elements],
            [
                edge(route.id, page_node.id, EdgeKind.renders),
                *(edge(page_node.id, element.id, EdgeKind.contains) for element in elements),
            ],
        )
    )

    assert len(index.for_page(page_node.id)) == MAX_CHAINS_PER_PAGE
    assert index.truncated[page_node.id] == 5
    # 被截掉的链路不进 by_id：校验阶段不可能放行一条看不到的链路
    assert len(index.by_id) == MAX_CHAINS_PER_PAGE
