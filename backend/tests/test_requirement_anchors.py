"""锚点抽取与直连（技术设计 6.13 第二、三步）的测试。

抽取测试锁「宁缺毋滥」的边界：什么必须认、什么坚决不认；直连测试锁置信度
分档（精确 0.95 / 后缀 0.85）与 source=static；编排测试锁「锚点覆盖的功能点
不再进模型」这条语义残差规则在 dry-run 下的表现。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.contracts import EdgeKind, IREdge, IRGraph, IRNode, NodeKind
from app.core.config import Settings
from app.requirement.anchors import AnchorKind, anchor_direct_connect, extract_anchors
from app.requirement.features import decompose
from app.requirement.orchestrator import RequirementOrchestrator
from app.schemas import Project, RequirementTask
from tests.graph_fixture import edge, graph, node, page, user_graph

_PROJECT = Project(
    id="prj_anchor0001",
    name="锚点测试",
    root_path="D:/nonexistent",
    framework="react",
    created_at=datetime.now(UTC),
)


def _task(requirement: str, *, dry_run: bool = True) -> RequirementTask:
    return RequirementTask(
        id="req_task_anchor1",
        project_id=_PROJECT.id,
        requirement_id="req_anchor1",
        status="running",
        dry_run=dry_run,
        requirement=requirement,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------- 抽取


def test_api_anchor_method_and_path() -> None:
    anchors = extract_anchors("列表页调用 GET /api/users 加载数据")
    assert [(a.kind, a.raw, a.normalized) for a in anchors] == [
        (AnchorKind.api, "GET /api/users", "GET /api/users")
    ]


def test_api_anchor_allows_cjk_adjacency() -> None:
    # 中文与 method 紧邻（无空格）必须认；「budget」里的 get 不能认
    anchors = extract_anchors("调用GET /api/users 刷新")
    assert anchors and anchors[0].normalized == "GET /api/users"
    assert extract_anchors("预算budget /api/users 缺 method") == []


def test_api_anchor_strips_origin_and_query() -> None:
    anchors = extract_anchors("提交表单 POST https://x.com/api/orders?page=1。")
    assert [(a.kind, a.normalized) for a in anchors] == [
        (AnchorKind.api, "POST /api/orders")
    ]


def test_api_anchor_requires_slash() -> None:
    # `GET users` 不是接口引用；裸 URL 没有 method 也不认（宁缺毋滥）
    assert extract_anchors("GET users 拉取") == []
    assert extract_anchors("访问 https://x.com/docs 首页") == []


def test_route_anchor_only_from_quotes() -> None:
    anchors = extract_anchors("在 `/users` 页面新增筛选")
    assert [(a.kind, a.raw, a.normalized) for a in anchors] == [
        (AnchorKind.route, "/users", "/users")
    ]
    # 全角引号与 hash 路由同样认
    assert [a.normalized for a in extract_anchors("打开「#/users」页面")] == ["/users"]
    # 裸写不认：普通中文句子里出现 /users 可能是正文片段
    assert extract_anchors("跳转到 /users 页面") == []


def test_dynamic_route_normalizes_params() -> None:
    anchors = extract_anchors("查看 `/users/42` 详情")
    assert [a.normalized for a in anchors] == ["/users/:param"]


def test_file_path_is_not_route() -> None:
    # /src/pages/UserList.tsx 是文件路径，不当路由；无扩展名的才当路由
    assert extract_anchors("改造 `/src/pages/UserList.tsx` 入口") == []
    assert [a.normalized for a in extract_anchors("改造 `/src/pages` 目录")] == ["/src/pages"]


def test_component_anchors() -> None:
    assert [(a.kind, a.normalized) for a in extract_anchors("改造 `UserList` 组件")] == [
        (AnchorKind.component, "UserList")
    ]
    assert [(a.kind, a.normalized) for a in extract_anchors("在 <UserList /> 上加按钮")] == [
        (AnchorKind.component, "UserList")
    ]


def test_field_anchors() -> None:
    assert [(a.kind, a.normalized) for a in extract_anchors("展示 `showFilter` 开关")] == [
        (AnchorKind.field, "showFilter")
    ]
    assert [(a.kind, a.normalized) for a in extract_anchors("按 `user_id` 排序")] == [
        (AnchorKind.field, "user_id")
    ]
    # 单个小写词太泛（id / name），不认；全大写不是字段
    assert extract_anchors("按 `id` 排序") == []


def test_ui_text_anchors_from_leftover_quotes() -> None:
    """差距表 #7：引号里的界面词是用户手动标出的原文，收作 uiText 锚点。"""
    assert [(a.kind, a.raw) for a in extract_anchors('点击「导出」按钮')] == [
        (AnchorKind.uiText, "导出")
    ]
    # 其他锚点形态优先：路由 / 组件 / 字段不被当成界面词
    kinds = [a.kind for a in extract_anchors("`/users` `UserList` `showFilter` 「搜索」")]
    assert kinds == [
        AnchorKind.route,
        AnchorKind.component,
        AnchorKind.field,
        AnchorKind.uiText,
    ]
    # 单字符太泛（「增」「删」）与两字母小写词（`id`）不认
    assert extract_anchors("点「增」新增一行") == []
    assert extract_anchors("按 `id` 排序") == []


def test_anchors_dedupe_by_kind_and_normalized() -> None:
    anchors = extract_anchors("改造 `UserList` 组件，在 <UserList /> 上加按钮")
    assert len(anchors) == 1
    assert anchors[0].normalized == "UserList"


def test_anchors_carry_char_range() -> None:
    text = "在 `/users` 页面"
    anchors = extract_anchors(text)
    assert anchors[0].start == 2
    assert anchors[0].end == 10
    assert text[anchors[0].start : anchors[0].end] == "`/users`"


# ---------------------------------------------------------------- 直连


def test_api_anchor_connects_page_with_full_path() -> None:
    result = anchor_direct_connect(user_graph(), decompose("列表页调用 GET /api/users"))
    assert result.warnings == []
    assert result.covered == {"f1"}
    (match,) = result.matches
    assert match.pageNodeId == "src/pages/UserList.tsx#UserList"
    assert match.routeId == "src/router.tsx#/users"
    assert match.confidence == 0.95
    assert str(match.source) == "static"
    # 子图是一跳证据范围（6.13 / 6.14）：api 在 page→fn 的第二跳，恒缺席
    assert match.subgraph.counts.api == 0
    assert match.subgraph.counts.fn == 1
    assert match.verifiedEvidence is not None
    assert {item.nodeId for item in match.verifiedEvidence} >= {
        "api:GET:/api/users",
        "src/pages/UserList.tsx#UserList",
        "src/router.tsx#/users",
    }


def test_api_anchor_suffix_fallback_gets_lower_confidence() -> None:
    # 图里记的是 /users（baseURL 未静态求值），需求写全路径 /api/users：
    # 精确不中，后缀对段命中，按 0.85 档落库
    api = node(
        "api:GET:/users",
        NodeKind.api,
        "GET /users",
        attrs={"method": "GET", "url": "/users"},
    )
    fetch = node(
        "src/pages/UserList.tsx#fetchUsers",
        NodeKind.fn,
        "fetchUsers",
        file="src/pages/UserList.tsx",
    )
    user_list = page("src/pages/UserList.tsx#UserList", "UserList")
    route = node(
        "src/router.tsx#/users",
        NodeKind.route,
        "/users",
        file="src/router.tsx",
        attrs={"path": "/users"},
    )
    g = graph(
        [user_list, fetch, api, route],
        [
            edge(route.id, user_list.id, EdgeKind.renders),
            edge(user_list.id, fetch.id, EdgeKind.calls),
            edge(fetch.id, api.id, EdgeKind.requests),
        ],
    )
    result = anchor_direct_connect(g, decompose("列表页调用 GET /api/users"))
    (match,) = result.matches
    assert match.pageNodeId == "src/pages/UserList.tsx#UserList"
    assert match.confidence == 0.85


def test_route_anchor_connects_page() -> None:
    result = anchor_direct_connect(user_graph(), decompose("「/users」页面新增筛选"))
    assert result.covered == {"f1"}
    (match,) = result.matches
    assert match.pageNodeId == "src/pages/UserList.tsx#UserList"
    assert match.routeId == "src/router.tsx#/users"
    assert match.confidence == 0.95


def test_route_anchor_prefers_declared_page_over_layout() -> None:
    """差距表 #5：同一路径下 layout + page 混挂，布局组件不冒充页面。

    边按遍历顺序 layout 在前；没有 attrs 显式标注时，route 直连会把布局组件
    当成页面。ast-worker 已按 `<Outlet>` / 嵌套路由规则分好类（pageComponentId
    与 layoutComponentIds），这里的候选顺序以标注为准。
    """
    layout = page("src/layouts/FullLayout.tsx#FullLayout", "FullLayout", texts=["导航"])
    user_list = page("src/pages/UserList.tsx#UserList", "UserList", texts=["搜索用户"])
    route = node(
        "src/router.tsx#/users",
        NodeKind.route,
        "/users",
        file="src/router.tsx",
        attrs={
            "path": "/users",
            "pageComponentId": "src/pages/UserList.tsx#UserList",
            "layoutComponentIds": ["src/layouts/FullLayout.tsx#FullLayout"],
        },
    )
    g = graph(
        [layout, user_list, route],
        [
            edge(route.id, layout.id, EdgeKind.renders),
            edge(route.id, user_list.id, EdgeKind.renders),
        ],
    )

    result = anchor_direct_connect(g, decompose("「/users」页面新增筛选"))
    (match,) = result.matches
    assert match.pageNodeId == "src/pages/UserList.tsx#UserList"
    assert match.routeId == route.id


def test_dynamic_route_anchor_matches_param_route() -> None:
    result = anchor_direct_connect(user_graph(), decompose("查看 `/users/42` 详情"))
    assert result.covered == {"f1"}
    (match,) = result.matches
    assert match.pageNodeId == "src/pages/UserDetail.tsx#UserDetail"
    assert match.routeId == "src/router.tsx#/users/:userId"


def test_component_anchor_only_connects_page_components() -> None:
    # 页面组件：直连
    result = anchor_direct_connect(user_graph(), decompose("改造 `UserList` 组件"))
    assert result.warnings == []
    (match,) = result.matches
    assert match.pageNodeId == "src/pages/UserList.tsx#UserList"
    # 非页面组件（TableCard 被页面渲染，自己不是页面）：不硬凑，显式降级
    result = anchor_direct_connect(user_graph(), decompose("改造 `TableCard` 组件"))
    assert result.matches == []
    assert result.covered == frozenset()
    assert any(w.code == "anchor_unresolved" for w in result.warnings)


def test_unresolved_route_anchor_degrades_with_warning() -> None:
    result = anchor_direct_connect(user_graph(), decompose("「/orders」页面新增导出"))
    assert result.matches == []
    assert result.covered == frozenset()
    (warning,) = result.warnings
    assert warning.code == "anchor_unresolved"
    assert warning.featureId == "f1"


def test_field_anchor_connects_unique_page() -> None:
    result = anchor_direct_connect(user_graph(), decompose("展示 `showFilter` 开关"))
    assert result.covered == {"f1"}
    (match,) = result.matches
    assert match.pageNodeId == "src/pages/UserList.tsx#UserList"
    assert match.confidence == 0.95


def test_field_anchor_shared_by_pages_is_ambiguous() -> None:
    # 同一字段出现在两个页面的 props 里：不唯一归属，不硬凑
    first = page("src/pages/A.tsx#A", "A", props=[("statusCode", None)])
    second = page("src/pages/B.tsx#B", "B", props=[("statusCode", None)])
    g = graph([first, second], [])
    result = anchor_direct_connect(g, decompose("展示 `statusCode` 字段"))
    assert result.matches == []
    assert any(w.code == "anchor_ambiguous" for w in result.warnings)


# ------------------------------------------------------------- 元素级直连（#7）


def _button(
    element_id: str,
    *,
    view_text: str | None = "导出",
    attrs: dict | None = None,
) -> IRNode:
    base = {"events": ["onClick"]}
    if view_text is not None:
        base["viewText"] = view_text
    base.update(attrs or {})
    return node(
        element_id,
        NodeKind.element,
        f"<button> {view_text or ''}".strip(),
        file="src/pages/UserList.tsx",
        attrs=base,
    )


def _element_graph(
    elements: list[IRNode],
    *,
    props: list[tuple[str, str | None]] | None = None,
    extra_nodes: list[IRNode] | None = None,
    extra_edges: list[IREdge] | None = None,
) -> IRGraph:
    """UserList 页面 + 若干元素（contains 挂载）。"""
    user_list = page("src/pages/UserList.tsx#UserList", "UserList", props=props)
    route = node(
        "src/router.tsx#/users",
        NodeKind.route,
        "/users",
        file="src/router.tsx",
        attrs={"path": "/users"},
    )
    return graph(
        [user_list, route, *elements, *(extra_nodes or [])],
        [
            edge(route.id, user_list.id, EdgeKind.renders),
            *(edge(user_list.id, element.id, EdgeKind.contains) for element in elements),
            *(extra_edges or []),
        ],
    )


def test_ui_text_anchor_hits_element_chain() -> None:
    """「导出」唯一命中按钮元素：match 落到元素级（chainId + targets）。"""
    button = _button("src/pages/UserList.tsx#UserList/e-btn")
    handle = node(
        "src/pages/UserList.tsx#handleExport",
        NodeKind.fn,
        "handleExport",
        file="src/pages/UserList.tsx",
    )
    g = _element_graph(
        [button],
        extra_nodes=[handle],
        extra_edges=[edge(button.id, handle.id, EdgeKind.calls)],
    )
    result = anchor_direct_connect(g, decompose('点击「导出」按钮导出列表'))
    assert result.covered == {"f1"}
    (match,) = result.matches
    assert match.pageNodeId == "src/pages/UserList.tsx#UserList"
    assert match.routeId == "src/router.tsx#/users"
    # 元素级落点：链路 id + 被引文命中的元素段，前端据此高亮「哪个按钮」
    assert match.chainId == f"{button.id}::{button.id}|{handle.id}"
    assert match.targets == [button.id]
    assert match.confidence == 0.90
    assert str(match.source) == "static"
    assert match.verifiedEvidence is not None
    assert match.verifiedEvidence[0].nodeId == button.id


def test_ui_text_anchor_hits_column_title() -> None:
    """「哪一列」：列名（attrs.columns 的字面量 title）也是元素的可见文案。"""
    table = _button(
        "src/pages/UserList.tsx#UserList/e-table",
        view_text=None,
        attrs={
            "viewRole": "container",
            "events": ["onRowClick"],
            "columns": [{"title": "状态", "dataIndex": "status"}],
        },
    )
    g = _element_graph([table])
    result = anchor_direct_connect(g, decompose('按「状态」筛选列表'))
    assert result.covered == {"f1"}
    (match,) = result.matches
    assert match.chainId is not None
    assert match.targets == [table.id]
    assert match.confidence == 0.90


def test_ui_text_anchor_shared_by_pages_is_ambiguous() -> None:
    """同一文案出现在两个页面：不唯一归属，降级为普通检索词。"""
    first = _button("src/pages/A.tsx#A/e-btn")
    second = _button("src/pages/B.tsx#B/e-btn")
    page_a = page("src/pages/A.tsx#A", "A")
    page_b = page("src/pages/B.tsx#B", "B")
    route_a = node("src/router.tsx#/a", NodeKind.route, "/a", file="r.tsx", attrs={"path": "/a"})
    route_b = node("src/router.tsx#/b", NodeKind.route, "/b", file="r.tsx", attrs={"path": "/b"})
    g = graph(
        [page_a, page_b, route_a, route_b, first, second],
        [
            edge(route_a.id, page_a.id, EdgeKind.renders),
            edge(route_b.id, page_b.id, EdgeKind.renders),
            edge(page_a.id, first.id, EdgeKind.contains),
            edge(page_b.id, second.id, EdgeKind.contains),
        ],
    )
    result = anchor_direct_connect(g, decompose('点击「导出」按钮'))
    assert result.matches == []
    assert result.covered == frozenset()
    assert any(w.code == "anchor_ambiguous" for w in result.warnings)


def test_ui_text_anchor_multi_elements_same_page_stays_page_level() -> None:
    """同页多元素命中：页面确定、元素不唯一——落页面级，不猜具体交互。"""
    toolbar = _button("src/pages/UserList.tsx#UserList/e-tb")
    row = _button("src/pages/UserList.tsx#UserList/e-row")
    g = _element_graph([toolbar, row])
    result = anchor_direct_connect(g, decompose('点击「导出」按钮'))
    assert result.covered == {"f1"}
    (match,) = result.matches
    assert match.pageNodeId == "src/pages/UserList.tsx#UserList"
    assert match.chainId is None
    assert match.targets is None
    assert match.confidence == 0.90
    assert any(w.code == "anchor_element_ambiguous" for w in result.warnings)


def test_ui_text_anchor_unresolved_degrades_with_warning() -> None:
    g = _element_graph([])
    result = anchor_direct_connect(g, decompose('点击「导出」按钮'))
    assert result.matches == []
    assert result.covered == frozenset()
    (warning,) = result.warnings
    assert warning.code == "anchor_unresolved"
    assert warning.featureId == "f1"


def test_page_level_and_element_level_matches_both_kept() -> None:
    """字段锚点（页面级）与界面词锚点（元素级）同页并存：去重不互相吞掉。"""
    button = _button("src/pages/UserList.tsx#UserList/e-btn")
    g = _element_graph([button], props=[("showFilter", None)])
    result = anchor_direct_connect(g, decompose('展示 `showFilter` 开关「导出」'))
    assert len(result.matches) == 2
    assert {(match.chainId, tuple(match.targets or [])) for match in result.matches} == {
        (None, ()),
        (f"{button.id}::{button.id}", (button.id,)),
    }

def test_features_without_anchors_stay_uncovered() -> None:
    result = anchor_direct_connect(
        user_graph(), decompose("用户列表增加状态筛选，并支持导出")
    )
    assert result.matches == []
    assert result.warnings == []
    assert result.covered == frozenset()


def test_api_anchor_multiple_pages_all_kept() -> None:
    # 一个接口被两个页面调用：保留多个 target，由用户选择，不自动合并
    api = node(
        "api:GET:/api/users",
        NodeKind.api,
        "GET /api/users",
        attrs={"method": "GET", "url": "/api/users"},
    )
    first = page("src/pages/A.tsx#A", "A")
    second = page("src/pages/B.tsx#B", "B")
    route_a = node(
        "src/router.tsx#/a", NodeKind.route, "/a", file="src/router.tsx", attrs={"path": "/a"}
    )
    route_b = node(
        "src/router.tsx#/b", NodeKind.route, "/b", file="src/router.tsx", attrs={"path": "/b"}
    )
    g = graph(
        [api, first, second, route_a, route_b],
        [
            edge(route_a.id, first.id, EdgeKind.renders),
            edge(route_b.id, second.id, EdgeKind.renders),
            edge(first.id, api.id, EdgeKind.requests),
            edge(second.id, api.id, EdgeKind.requests),
        ],
    )
    result = anchor_direct_connect(g, decompose("调用 GET /api/users"))
    assert {match.pageNodeId for match in result.matches} == {
        "src/pages/A.tsx#A",
        "src/pages/B.tsx#B",
    }


# ---------------------------------------------------------------- 编排


async def test_dry_run_emits_anchor_matches_without_model() -> None:
    """dry-run 不调模型，但锚点直连是零模型的：照常产出 source=static 的结论。"""
    orchestrator = RequirementOrchestrator(Settings(_env_file=None), llm=None)
    requirement = "列表页调用 GET /api/users 加载；为列表页增加状态筛选"
    mapping = await orchestrator.run(_PROJECT, _task(requirement), user_graph())
    assert mapping.schemaVersion == "1.2"
    assert mapping.dryRun is True
    assert mapping.matches, "锚点直连的结论应在 dry-run 下照常产出"
    for match in mapping.matches:
        assert str(match.source) == "static"
        assert match.featureId == "f1"
    assert {match.pageNodeId for match in mapping.matches} == {
        "src/pages/UserList.tsx#UserList"
    }
    # 未被锚点覆盖的功能点（f2）在 dry-run 下没有模型结论，只落 candidates
    feature_ids = {match.featureId for match in mapping.matches}
    assert "f2" not in feature_ids
