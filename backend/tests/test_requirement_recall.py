"""召回层测试：确定性、可解释、可复现。

这里刻意不碰模型与 HTTP。召回质量是整个功能的准确率地板，必须能独立验证。
"""

from __future__ import annotations

import pytest

from app.contracts import (
    EdgeKind,
    Evidence,
    IREdge,
    IRGraph,
    IRNode,
    Location,
    NodeKind,
    SignalKind,
    Source,
)

# Severity 必须取自 requirement 契约：app.contracts 顶层同名导出是 IR 契约的另一个
# 枚举，混用会让 `warning.severity is Severity.info` 莫名其妙地失败。
from app.contracts.requirement import Severity
from app.requirement.features import DecomposeResult, decompose, decompose_with_report
from app.requirement.lexicon import BIGRAM_TERM_WEIGHT, expand
from app.requirement.recall import DEFAULT_TOP_K, page_nodes, recall
from app.requirement.text import ascii_words, contains_cjk, normalize

_STATIC = Source.static


def _evidence(file: str, line: int = 1) -> Evidence:
    return Evidence(source=_STATIC, confidence=1.0, file=file, loc=Location(line=line, col=1))


def _node(
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
        evidence=[_evidence(file or "src/unknown.tsx")],
    )


def _edge(source: str, target: str, kind: EdgeKind) -> IREdge:
    return IREdge(
        id=f"{source}->{target}",
        from_=source,
        to=target,
        kind=kind,
        evidence=[_evidence("src/x.tsx")],
    )


def _page(
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
    return _node(node_id, NodeKind.component, name, file=f"src/pages/{name}.tsx", attrs=attrs)


def _graph(nodes: list[IRNode], edges: list[IREdge]) -> IRGraph:
    return IRGraph(version="1.1", analysisId="an_test", nodes=nodes, edges=edges)


@pytest.fixture
def user_graph() -> IRGraph:
    """两个页面 + 两个路由，模仿真实样本仓的形状（含子组件与接口链路）。"""
    user_list = _page(
        "src/pages/UserList.tsx#UserList",
        "UserList",
        texts=["加载中…", "搜索用户", "姓名", "邮箱"],
        props=[
            ("title", "列表标题"),
            ("showFilter", "是否显示筛选栏"),
            ("onSelect", "选中回调，由页面注入"),
        ],
    )
    user_detail = _page(
        "src/pages/UserDetail.tsx#UserDetail",
        "UserDetail",
        texts=["用户详情", "编号", "状态", "待审核"],
        props=[("userId", None)],
    )
    table_card = _page("src/components/TableCard.tsx#TableCard", "TableCard", texts=["查看"])
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
        "src/router.tsx#/users",
        NodeKind.route,
        "/users",
        file="src/router.tsx",
        attrs={"path": "/users"},
    )
    route_detail = _node(
        "src/router.tsx#/users/:userId",
        NodeKind.route,
        "/users/:userId",
        file="src/router.tsx",
        attrs={"path": "/users/:userId", "params": ["userId"]},
    )

    return _graph(
        [user_list, user_detail, table_card, fetch_users, api, route_list, route_detail],
        [
            _edge(route_list.id, user_list.id, EdgeKind.renders),
            _edge(route_detail.id, user_detail.id, EdgeKind.renders),
            _edge(user_list.id, table_card.id, EdgeKind.renders),
            _edge(user_list.id, fetch_users.id, EdgeKind.calls),
            _edge(fetch_users.id, api.id, EdgeKind.requests),
        ],
    )


# --- 文本层 ---------------------------------------------------------------


def test_normalize_folds_fullwidth_and_case() -> None:
    assert normalize("  搜索　用户（筛选）  ") == "搜索 用户(筛选)"
    assert normalize("ShowFilter") == "showfilter"


def test_ascii_words_splits_camel_before_lowercasing() -> None:
    # 先切再转小写；反过来会让 showFilter 变成一个词
    assert ascii_words("showFilter") == ["show", "filter"]
    assert ascii_words("/api/users/:userId") == ["api", "users", "user", "id"]


def test_contains_cjk_matches_ascii_by_whole_word_only() -> None:
    # order 不应命中 border：整词匹配而非子串
    assert contains_cjk("border-color", "order") is False
    assert contains_cjk("createOrder", "order") is True
    assert contains_cjk("用户列表", "列表") is True


# --- 词表层 ---------------------------------------------------------------


def test_expand_maps_chinese_concept_to_english_identifiers() -> None:
    terms = expand("增加状态筛选")
    surfaces = terms.surfaces()
    assert "筛选" in surfaces
    assert "filter" in surfaces  # 中文需求命中 showFilter 的前提
    assert "status" in surfaces
    assert {"filter", "status"}.issubset(set(terms.concepts))


def test_expand_weights_literal_terms_above_expanded_synonyms() -> None:
    terms = expand("筛选")
    by_surface = {term.surface: term.weight for term in terms.terms}
    assert by_surface["筛选"] > by_surface["filter"]


def test_expand_drops_stopwords_and_keeps_unknown_bigrams_weak() -> None:
    terms = expand("增加一个状态筛选")
    surfaces = terms.surfaces()
    assert "一个" not in surfaces  # 虚词不进检索词
    unknown = [term for term in terms.terms if term.concept is None]
    assert all(term.weight == BIGRAM_TERM_WEIGHT for term in unknown)


# --- 召回层 ---------------------------------------------------------------


def test_recall_ranks_the_page_whose_text_matches_first(user_graph: IRGraph) -> None:
    result = recall(user_graph, decompose("用户列表增加状态筛选")[0].terms)

    assert result.candidates, "应当召回候选"
    top = result.candidates[0].candidate
    assert top.nodeId == "src/pages/UserList.tsx#UserList"
    assert top.routeId == "src/router.tsx#/users"


def test_recall_separates_two_pages_sharing_the_same_entity(user_graph: IRGraph) -> None:
    """两个页面都提到「用户」，靠「列表 / 详情」区分——这是召回必须做到的。"""
    list_result = recall(user_graph, decompose("用户列表增加状态筛选")[0].terms)
    detail_result = recall(user_graph, decompose("查看用户详情")[0].terms)

    assert list_result.candidates[0].candidate.nodeId.endswith("#UserList")
    assert detail_result.candidates[0].candidate.nodeId.endswith("#UserDetail")


def test_recall_explains_every_point_with_signals(user_graph: IRGraph) -> None:
    result = recall(user_graph, decompose("用户列表增加状态筛选")[0].terms)
    candidate = result.candidates[0].candidate

    assert candidate.signals, "候选必须带信号，否则分数无法解释"
    assert all(signal.value and signal.score > 0 for signal in candidate.signals)
    # 信号得分之和就是总分（前端展示「为什么是它」依赖这一点）
    assert candidate.score == pytest.approx(
        sum(signal.score for signal in candidate.signals), abs=1e-6
    )
    kinds = {signal.kind for signal in candidate.signals}
    assert SignalKind.routePath in kinds
    assert SignalKind.uiText in kinds


def test_recall_uses_route_title_as_identity_signal() -> None:
    """差距表 #4：「订单管理页」对不上 /order/list，但能对上路由标题。

    标题以 routeTitle 信号参与打分，并进入 quote_corpus——模型可以把标题
    作为引文，服务端照样能校验。
    """
    page_node = _page("src/pages/OrderList.tsx#OrderList", "OrderList", texts=["订单列表"])
    route_node = _node(
        "src/router.tsx#/order/list",
        NodeKind.route,
        "/order/list",
        file="src/router.tsx",
        attrs={"path": "/order/list", "title": "订单管理"},
    )
    result = recall(
        _graph([page_node, route_node], [_edge(route_node.id, page_node.id, EdgeKind.renders)]),
        decompose("订单管理")[0].terms,
    )

    assert result.candidates, "路由标题命中时应当召回页面"
    detail = result.candidates[0]
    assert (SignalKind.routeTitle, "订单管理") in {
        (signal.kind, signal.value) for signal in detail.candidate.signals
    }
    assert "订单管理" in detail.quote_corpus


def test_recall_ignores_blank_route_title() -> None:
    """空标题 / 非字符串不产信号——ast-worker 只收字面量，这里不做二次推断。"""
    page_node = _page("src/pages/OrderList.tsx#OrderList", "OrderList", texts=["订单列表"])
    route_node = _node(
        "src/router.tsx#/order/list",
        NodeKind.route,
        "/order/list",
        file="src/router.tsx",
        attrs={"path": "/order/list", "title": "   "},
    )
    result = recall(
        _graph([page_node, route_node], [_edge(route_node.id, page_node.id, EdgeKind.renders)]),
        decompose("订单管理")[0].terms,
    )

    detail = next(item for item in result.candidates if item.candidate.nodeId == page_node.id)
    assert all(signal.kind is not SignalKind.routeTitle for signal in detail.candidate.signals)


def test_recall_uses_props_description_as_evidence(user_graph: IRGraph) -> None:
    """`是否显示筛选栏` 是 props 的 JSDoc，只有它能证明 showFilter 与「筛选」相关。"""
    result = recall(user_graph, decompose("筛选")[0].terms)
    candidate = result.candidates[0].candidate
    assert any(signal.kind is SignalKind.propDescription for signal in candidate.signals)


def test_page_nodes_honors_declared_layout_ids() -> None:
    """差距表 #5：ast-worker 显式标注的 layoutComponentIds 计入布局集合。

    同一路径下 layout + page 混挂（父路由出 layout、index 出内容）时，布局组件
    只挂一条路径，「被多条路径复用」的启发式抓不到它——必须吃 attrs 的显式标注。
    布局组件仍保留为召回候选（外壳文案要能被找到），只是语义归入 layout。
    """
    layout = _page("src/layouts/FullLayout.tsx#FullLayout", "FullLayout", texts=["后台管理系统"])
    user_list = _page("src/pages/UserList.tsx#UserList", "UserList", texts=["搜索用户"])
    route = _node(
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
    g = _graph(
        [layout, user_list, route],
        [
            _edge(route.id, layout.id, EdgeKind.renders),
            _edge(route.id, user_list.id, EdgeKind.renders),
        ],
    )

    pages, route_by_page, layout_ids = page_nodes(g)
    assert layout_ids == {"src/layouts/FullLayout.tsx#FullLayout"}
    assert route_by_page[layout.id] == route.id
    assert route_by_page[user_list.id] == route.id
    assert {item.id for item in pages} == {layout.id, user_list.id}


def test_recall_keeps_locatable_evidence_for_each_quote(user_graph: IRGraph) -> None:
    """判定阶段要靠这份元数据把引文落成可复核坐标，不能只剩字符串。"""
    result = recall(user_graph, decompose("用户列表增加状态筛选")[0].terms)
    detail = next(item for item in result.candidates if item.candidate.nodeId.endswith("#UserList"))

    text_evidence = next(item for item in detail.evidence if item.text == "搜索用户")
    assert text_evidence.kind is SignalKind.uiText
    # UI 文案归属页面节点，坐标取 attrs.uiText 里的精确位置
    assert text_evidence.node_id == "src/pages/UserList.tsx#UserList"
    assert text_evidence.file == "src/pages/UserList.tsx"
    assert text_evidence.loc == Location(line=1, col=1)
    assert text_evidence.source is _STATIC
    assert text_evidence.confidence == 1.0

    # 每条证据都能在 quote_corpus 里找到（反向归属后种子文本也会进语料，
    # 所以证据集是语料的子集），两套数据不允许各说各话
    assert {item.text for item in detail.evidence} <= set(detail.quote_corpus)


def test_recall_attributes_api_evidence_to_the_api_node(user_graph: IRGraph) -> None:
    """接口 url 的坐标属于 api 节点本身，不该记在页面上。"""
    result = recall(user_graph, decompose("users 接口")[0].terms)
    detail = next(item for item in result.candidates if item.candidate.nodeId.endswith("#UserList"))

    api_evidence = next(item for item in detail.evidence if item.kind is SignalKind.apiUrl)
    assert api_evidence.text == "/api/users"
    assert api_evidence.node_id == "api:GET:/api/users"
    # api 节点没有 file，回落到其 evidence 上的文件
    assert api_evidence.file == "src/unknown.tsx"


def test_recall_reaches_api_url_through_one_hop(user_graph: IRGraph) -> None:
    """页面 calls fetchUsers，fetchUsers requests /api/users：接口词要算到页面上。"""
    result = recall(user_graph, decompose("users 接口")[0].terms)
    # UserDetail 可能因路由参数命中更多词而排在前面；这里只关心 UserList 拿到了接口信号
    user_list = next(
        item for item in result.candidates if item.candidate.nodeId.endswith("#UserList")
    )
    assert any(signal.kind is SignalKind.apiUrl for signal in user_list.candidate.signals)


def test_child_text_attributes_to_owner_page(user_graph: IRGraph) -> None:
    """子组件文案反向归属到所属页面：能召回页面，但 TableCard 自己不是页面。

    6.11 语义取代旧的「一刀切不继承」：词法命中在 TableCard 上，沿 renders
    反向归属到 UserList——页面与子组件的区分度由置信度（路径最弱一环 ×
    种子系数 0.7）表达，而不是靠丢弃。
    """
    result = recall(user_graph, decompose("查看")[0].terms)
    user_list = next(
        item
        for item in result.candidates
        if item.candidate.nodeId.endswith("#UserList")
    )
    assert user_list.candidate.score > 0
    # TableCard 不是页面：命中归属到它所属的页面，而不是产生新候选
    assert all(
        "TableCard" not in item.candidate.nodeId for item in result.candidates
    )


def test_child_text_keeps_full_attribution_path(user_graph: IRGraph) -> None:
    """子组件文案命中的归属路径完整可回查：TableCard → UserList → route。

    反向 BFS 的价值就在这里：候选不只带分数，还带「这条命中是怎么走到
    这个页面的」——每一环都能回图核对，判定从「选页面」变成「确认链路」。
    """
    result = recall(user_graph, decompose("查看")[0].terms)
    user_list = next(
        item
        for item in result.candidates
        if item.candidate.nodeId.endswith("#UserList")
    )
    assert user_list.candidate.anchorNodeIds == [
        "src/components/TableCard.tsx#TableCard",
        "src/pages/UserList.tsx#UserList",
        "src/router.tsx#/users",
    ]
    assert any(
        signal.kind is SignalKind.uiText and signal.value == "查看"
        for signal in user_list.candidate.signals
    )


# --- 布局组件 -------------------------------------------------------------


def _layout_graph(header_texts: list[str]) -> IRGraph:
    """模拟一条真实形态：route → FullLayout → Header，且 FullLayout 被多个路由复用。"""
    header = _node(
        "src/core/layouts/components/Header.tsx#Header",
        NodeKind.component,
        "Header",
        file="src/core/layouts/components/Header.tsx",
        attrs=_ui_text_attrs(header_texts),
    )
    layout = _node(
        "src/core/layouts/FullLayout.tsx#FullLayout",
        NodeKind.component,
        "FullLayout",
        file="src/core/layouts/FullLayout.tsx",
    )
    welcome = _page("src/features/Welcome/views/WelcomeView.tsx#WelcomeView", "WelcomeView")
    route_home = _node(
        "src/features/Welcome/routes.ts#/",
        NodeKind.route,
        "/",
        file="src/features/Welcome/routes.ts",
        attrs={"path": "/"},
    )
    route_posts = _node(
        "src/features/Post/routes.ts#/posts",
        NodeKind.route,
        "/posts",
        file="src/features/Post/routes.ts",
        attrs={"path": "/posts"},
    )
    post_view = _page("src/features/Post/views/PostView.tsx#PostView", "PostView")
    return _graph(
        [header, layout, welcome, post_view, route_home, route_posts],
        [
            _edge(route_home.id, layout.id, EdgeKind.renders),
            _edge(route_posts.id, layout.id, EdgeKind.renders),
            _edge(layout.id, header.id, EdgeKind.renders),
            _edge(route_home.id, welcome.id, EdgeKind.renders),
            _edge(route_posts.id, post_view.id, EdgeKind.renders),
        ],
    )


def _ui_text_attrs(texts: list[str]) -> dict:
    return {
        "uiText": {
            "texts": [
                {"text": text, "kind": "text", "via": None, "loc": {"line": 1, "col": 1}}
                for text in texts
            ]
        }
    }


def test_recall_finds_layout_child_text() -> None:
    """全站导航栏的文案写在子组件里，但它在每个页面上——必须能召回。

    这是缺陷 2：`route → FullLayout → Header`，GitHub 链接在 Header 中，
    三层之外的文案此前被完全丢弃，导致「点击 GitHub 跳转」召回 0 候选。
    """
    result = recall(_layout_graph(["github", "Products"]), expand("github"))

    assert result.candidates, "布局子组件的文案应当能召回页面"
    assert all(item.candidate.nodeId.endswith("#FullLayout") for item in result.candidates)


def test_recall_marks_layout_child_text_with_lower_score() -> None:
    """布局文案折半计权：能召回，但不能压过页面自有文案。"""
    layout_only = recall(_layout_graph(["github"]), expand("github"))

    # 同一条文案若写在页面自己身上，得分应恰好是布局版本的两倍
    page = _page("src/pages/X.tsx#X", "X", texts=["github"])
    route = _node("src/router.tsx#/x", NodeKind.route, "/x", attrs={"path": "/x"})
    own_page = _graph(
        [page, route],
        [_edge(route.id, page.id, EdgeKind.renders)],
    )
    from_own = recall(own_page, expand("github"))

    layout_score = layout_only.candidates[0].candidate.score
    own_score = from_own.candidates[0].candidate.score
    assert layout_score == pytest.approx(own_score * 0.5, abs=1e-6)


def test_recall_layout_evidence_keeps_real_source_location() -> None:
    """折半的是分数，证据坐标仍指向 Header 文件——复核时要能跳回原文。"""
    result = recall(_layout_graph(["github"]), expand("github"))
    detail = result.candidates[0]
    evidence = next(item for item in detail.evidence if item.text == "github")

    assert evidence.node_id == "src/core/layouts/components/Header.tsx#Header"
    assert evidence.file == "src/core/layouts/components/Header.tsx"
    # 引文语料必须包含它，否则判定阶段会把它判成幻觉
    assert "github" in detail.quote_corpus


def test_recall_returns_nothing_for_unrelated_requirement(user_graph: IRGraph) -> None:
    result = recall(user_graph, decompose("支付网关对接")[0].terms)
    assert result.candidates == []


def test_recall_is_deterministic_and_sorted(user_graph: IRGraph) -> None:
    terms = decompose("用户列表 用户详情")[0].terms
    first = recall(user_graph, terms, top_k=DEFAULT_TOP_K)
    second = recall(user_graph, terms, top_k=DEFAULT_TOP_K)
    assert [item.candidate.nodeId for item in first.candidates] == [
        item.candidate.nodeId for item in second.candidates
    ]
    scores = [item.candidate.score for item in first.candidates]
    assert scores == sorted(scores, reverse=True)


def test_recall_truncates_to_top_k_and_says_so(user_graph: IRGraph) -> None:
    result = recall(user_graph, decompose("用户列表 用户详情")[0].terms, top_k=1)
    assert len(result.candidates) == 1
    warning = next(item for item in result.warnings if item.code == "candidates_truncated")
    assert warning.severity is Severity.info


def test_recall_degrades_to_top_level_components_without_routes(user_graph: IRGraph) -> None:
    """无 route 节点时降级为顶层组件，并且必须显式告警（改变了候选的含义）。"""
    without_routes = _graph(
        [node for node in user_graph.nodes if node.kind is not NodeKind.route],
        [edge for edge in user_graph.edges if not edge.from_.startswith("src/router.tsx")],
    )

    result = recall(without_routes, decompose("用户列表增加状态筛选")[0].terms)

    assert result.route_by_page == {}
    assert result.candidates[0].candidate.routeId is None
    warning = next(item for item in result.warnings if item.code == "no_route_nodes")
    assert warning.severity is Severity.warning
    # 降级后 TableCard 是顶层组件，但没有命中词，所以不会进候选
    assert all("TableCard" not in item.candidate.nodeId for item in result.candidates)


def test_recall_says_why_it_cannot_recall(user_graph: IRGraph) -> None:
    empty = recall(user_graph, expand(""))
    assert empty.candidates == []
    assert [warning.code for warning in empty.warnings] == ["requirement_empty"]


def test_recall_reports_graph_without_any_page() -> None:
    bare = _graph([_node("src/util.ts#noop", NodeKind.fn, "noop", file="src/util.ts")], [])
    result = recall(bare, expand("用户列表"))
    assert result.candidates == []
    codes = [warning.code for warning in result.warnings]
    assert "no_page_candidates" in codes


# --- 拆解层 ---------------------------------------------------------------


def test_decompose_splits_bullets_and_sentences() -> None:
    features = decompose("1. 用户列表增加状态筛选\n2. 支持导出订单。另外需要权限控制")
    texts = [feature.text for feature in features]
    assert "用户列表增加状态筛选" in texts
    assert "支持导出订单" in texts
    assert "另外需要权限控制" in texts


def test_decompose_falls_back_to_whole_text_when_unparseable() -> None:
    """切不出可用片段时宁可粗，不可空：空功能点会让整个流程直接失败。"""
    features = decompose("登录")
    assert [feature.text for feature in features] == ["登录"]


def test_decompose_assigns_stable_ids_and_dedupes() -> None:
    features = decompose("用户列表增加筛选\n用户列表增加筛选")
    assert [feature.id for feature in features] == ["f1"]


def test_decompose_caps_feature_count() -> None:
    paragraph = "\n".join(f"功能点编号{i}需要支持搜索" for i in range(20))
    assert len(decompose(paragraph)) == 8


def test_decompose_reports_feature_limit_instead_of_silently_dropping() -> None:
    """超出上限被挤掉的功能点必须告警：否则用户会以为需求被完整拆解了。"""
    paragraph = "\n".join(f"功能点编号{i}需要支持搜索" for i in range(20))
    result = decompose_with_report(paragraph)

    assert len(result.features) == 8
    assert [warning.code for warning in result.warnings] == ["feature_limit_reached"]
    # 告警要说出丢了多少，不能只说「超限了」
    assert "12" in result.warnings[0].message


def test_decompose_reports_truncated_feature() -> None:
    """单个功能点被截断时同样告警，并标明是哪个功能点。"""
    result = decompose_with_report("需求" * 200)

    assert len(result.features) == 1
    assert len(result.features[0].text) == 200
    assert [warning.code for warning in result.warnings] == ["feature_truncated"]
    assert result.warnings[0].featureId == "f1"


def test_decompose_within_limits_emits_no_warning() -> None:
    requirement = "用户列表增加状态筛选"
    assert decompose_with_report(requirement) == DecomposeResult(
        features=decompose(requirement)
    )
