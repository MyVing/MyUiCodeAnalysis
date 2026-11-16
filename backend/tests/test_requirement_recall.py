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
    Severity,
    SignalKind,
    Source,
)
from app.requirement.features import decompose
from app.requirement.lexicon import BIGRAM_TERM_WEIGHT, expand
from app.requirement.recall import DEFAULT_TOP_K, recall
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
    return IREdge(id=f"{source}->{target}", from_=source, to=target, kind=kind, evidence=[_evidence("src/x.tsx")])


def _page(
    node_id: str,
    name: str,
    *,
    texts: list[str] | None = None,
    props: list[tuple[str, str | None]] | None = None,
) -> IRNode:
    attrs: dict = {}
    if texts is not None:
        attrs["uiText"] = {"texts": [{"text": text, "kind": "text", "via": None, "loc": {"line": 1, "col": 1}} for text in texts]}
    if props is not None:
        attrs["props"] = {
            "typeName": None,
            "typeFile": None,
            "generic": False,
            "fields": [
                {"name": prop, "type": "string", "optional": False, "defaultValue": None,
                 "description": description, "file": "src/types.ts", "loc": {"line": 1, "col": 1}}
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
        props=[("title", "列表标题"), ("showFilter", "是否显示筛选栏"), ("onSelect", "选中回调，由页面注入")],
    )
    user_detail = _page(
        "src/pages/UserDetail.tsx#UserDetail",
        "UserDetail",
        texts=["用户详情", "编号", "状态", "待审核"],
        props=[("userId", None)],
    )
    table_card = _page("src/components/TableCard.tsx#TableCard", "TableCard", texts=["查看"])
    fetch_users = _node("src/pages/UserList.tsx#fetchUsers", NodeKind.fn, "fetchUsers", file="src/pages/UserList.tsx")
    api = _node("api:GET:/api/users", NodeKind.api, "GET /api/users", attrs={"method": "GET", "url": "/api/users"})
    route_list = _node("src/router.tsx#/users", NodeKind.route, "/users", file="src/router.tsx", attrs={"path": "/users"})
    route_detail = _node(
        "src/router.tsx#/users/:userId", NodeKind.route, "/users/:userId",
        file="src/router.tsx", attrs={"path": "/users/:userId", "params": ["userId"]},
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
    assert candidate.score == pytest.approx(sum(signal.score for signal in candidate.signals), abs=1e-6)
    kinds = {signal.kind for signal in candidate.signals}
    assert SignalKind.routePath in kinds
    assert SignalKind.uiText in kinds


def test_recall_uses_props_description_as_evidence(user_graph: IRGraph) -> None:
    """`是否显示筛选栏` 是 props 的 JSDoc，只有它能证明 showFilter 与「筛选」相关。"""
    result = recall(user_graph, decompose("筛选")[0].terms)
    candidate = result.candidates[0].candidate
    assert any(signal.kind is SignalKind.propDescription for signal in candidate.signals)


def test_recall_reaches_api_url_through_one_hop(user_graph: IRGraph) -> None:
    """页面 calls fetchUsers，fetchUsers requests /api/users：接口词要算到页面上。"""
    result = recall(user_graph, decompose("users 接口")[0].terms)
    candidate = result.candidates[0].candidate
    assert any(signal.kind is SignalKind.apiUrl for signal in candidate.signals)


def test_recall_does_not_inherit_children_texts(user_graph: IRGraph) -> None:
    """TableCard 的文案只归 TableCard，不许被页面继承——否则候选之间失去区分度。"""
    result = recall(user_graph, decompose("查看")[0].terms)
    assert result.candidates == []


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
