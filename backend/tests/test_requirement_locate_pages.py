"""AI 在线定位测试：页面索引、链路选择、以及每一道服务端校验闸。

全部用 FakeLLMClient —— 定位逻辑的正确性不能依赖真实模型的输出分布。
真实模型的「挑得好不好」属于评估集的事，这里只验证「挑错了会被拦住」。
"""

from __future__ import annotations

import json

import pytest

from app.contracts import EdgeKind, IRGraph, NodeKind
from app.contracts.requirement import Reason
from app.requirement.chains import (
    TERMINATED_EXTERNAL,
    TERMINATED_INJECTED_HANDLER,
    derive_chains,
)
from app.requirement.features import decompose
from app.requirement.llm import LLMCallError
from app.requirement.locate import (
    PageIndex,
    build_page_index,
    locate,
    merge_lexical_candidates,
    semantic_signals,
)
from app.requirement.recall import recall
from tests.fakes import FakeLLMClient
from tests.graph_fixture import edge, graph, node, page, user_graph

# ---------------------------------------------------------------------------
# 夹具：一个页面 + 一条完整链路（按钮 → handler → 接口）+ 一条外链
# ---------------------------------------------------------------------------

_ORDERS_PAGE = "src/pages/OrderList.tsx#OrderList"
_EXPORT_BUTTON = f"{_ORDERS_PAGE}/jsx:button"
_GITHUB_LINK = f"{_ORDERS_PAGE}/jsx:a"
_HANDLER = "src/pages/OrderList.tsx#handleExport"
_API = "api:GET:/api/orders/export"


def _orders_graph() -> IRGraph:
    order_list = page(_ORDERS_PAGE, "OrderList", texts=["订单列表"])
    export_btn = node(
        _EXPORT_BUTTON,
        NodeKind.element,
        "<button> 导出",
        file="src/pages/OrderList.tsx",
        attrs={"tag": "button", "viewRole": "control", "viewText": "导出"},
    )
    handler = node(_HANDLER, NodeKind.fn, "handleExport", file="src/pages/OrderList.tsx")
    api = node(
        _API,
        NodeKind.api,
        "GET /api/orders/export",
        attrs={"method": "GET", "url": "/api/orders/export"},
    )
    github = node(
        _GITHUB_LINK,
        NodeKind.element,
        "<a> GitHub",
        file="src/pages/OrderList.tsx",
        attrs={
            "tag": "a",
            "viewRole": "link",
            "viewText": "GitHub",
            "href": "https://github.com/x/y",
            "external": True,
        },
    )
    route = node(
        "src/router.tsx#/orders",
        NodeKind.route,
        "/orders",
        file="src/router.tsx",
        attrs={"path": "/orders"},
    )
    return graph(
        [order_list, export_btn, handler, api, github, route],
        [
            edge(route.id, _ORDERS_PAGE, EdgeKind.renders),
            edge(_ORDERS_PAGE, _EXPORT_BUTTON, EdgeKind.contains),
            edge(_ORDERS_PAGE, _GITHUB_LINK, EdgeKind.contains),
            edge(_EXPORT_BUTTON, _HANDLER, EdgeKind.calls),
            edge(_HANDLER, _API, EdgeKind.requests),
        ],
    )


def _index(source: IRGraph, requirement: str = "订单列表支持导出") -> tuple[PageIndex, object]:
    features = decompose(requirement)
    recall_result = recall(source, features[0].terms)
    chains = derive_chains(source)
    return build_page_index(source, chains, recall_result=recall_result), chains


def _export_chain_id(chains) -> str:
    return next(
        chain.id
        for chain in chains.for_page(_ORDERS_PAGE)
        if chain.element.node_id == _EXPORT_BUTTON
    )


def _github_chain_id(chains) -> str:
    return next(
        chain.id for chain in chains.for_page(_ORDERS_PAGE) if chain.element.node_id == _GITHUB_LINK
    )


def _response(*matches: dict, unmatched: list[dict] | None = None) -> str:
    return json.dumps(
        {"matches": list(matches), "unmatched": unmatched or []}, ensure_ascii=False
    )


async def _run(
    source: IRGraph,
    response: str | list[str],
    *,
    requirement: str = "订单列表支持导出",
):
    features = decompose(requirement)
    index, chains = _index(source, requirement)
    llm = FakeLLMClient([response] if isinstance(response, str) else response)
    return (
        await locate(
            features, index, chains, source, llm, requirement=requirement, max_calls=4
        ),
        llm,
        features,
    )


# ---------------------------------------------------------------------------
# 页面索引
# ---------------------------------------------------------------------------


def test_index_lists_pages_with_their_chains() -> None:
    """索引必须让模型看到「这一页有哪些交互」，否则它只能猜页面。"""
    source = _orders_graph()
    index, chains = _index(source)

    assert len(index.pages) == 1
    entry = index.pages[0]
    assert entry.node_id == _ORDERS_PAGE
    assert entry.route_paths == ("/orders",)
    assert entry.total_chains == 2
    payload = entry.to_payload()
    assert {chain["chainId"] for chain in payload["chains"]} == set(chains.by_id)
    # 外链断点要进 prompt：模型据此说明「跳到了站外」
    github = next(c for c in payload["chains"] if c.get("href"))
    assert github["terminatedAt"] == TERMINATED_EXTERNAL


def test_index_quotes_include_element_text_and_href() -> None:
    """链路原文是模型唯一可以直接引用的东西，缺一段就会逼模型编造引文。"""
    source = _orders_graph()
    index, chains = _index(source)
    payload = index.by_id[_ORDERS_PAGE].to_payload()

    github = next(c for c in payload["chains"] if c["chainId"] == _github_chain_id(chains))
    assert "GitHub" in github["quotes"]
    assert "https://github.com/x/y" in github["quotes"]

    export = next(c for c in payload["chains"] if c["chainId"] == _export_chain_id(chains))
    assert "导出" in export["quotes"]
    assert "handleExport" in export["quotes"]
    assert "/api/orders/export" in export["quotes"]


def test_index_reports_truncated_chains_and_pages() -> None:
    """截断必须写进 payload 与产物告警：用户以为模型看过了全部交互就糟了。"""
    source = user_graph()
    chains = derive_chains(source)
    index = build_page_index(source, chains, max_pages=1)

    assert index.omitted_pages == 1
    prompts = index.pages[0].to_payload()
    assert isinstance(prompts["nodeId"], str)


def test_page_corpus_covers_static_pages_without_chains() -> None:
    """纯展示页面没有任何链路，但页面自己的文案仍是有效证据。"""
    about = page("src/pages/About.tsx#About", "About", texts=["关于我们", "公司简介"])
    route = node(
        "src/router.tsx#/about", NodeKind.route, "/about", file="src/router.tsx", attrs={}
    )
    source = graph(
        [about, route], [edge(route.id, about.id, EdgeKind.renders)]
    )

    index, chains = _index(source, "关于我们页面的公司简介")
    assert chains.by_id == {}
    entry = index.by_id[about.id]
    assert entry.total_chains == 0
    assert "关于我们" in index.corpus_of(None, about.id, chains)


# ---------------------------------------------------------------------------
# 校验闸
# ---------------------------------------------------------------------------


async def test_locate_accepts_chain_match_and_returns_path() -> None:
    source = _orders_graph()
    chains = derive_chains(source)
    chain_id = _export_chain_id(chains)

    outcome, *_ = await _run(
        source,
        _response(
            {
                "featureId": "f1",
                "pageNodeId": _ORDERS_PAGE,
                "chainId": chain_id,
                "quote": "导出",
                "confidence": 0.95,
                "reason": "导出按钮对应导出功能",
            }
        ),
    )

    assert outcome.dropped == []
    match = outcome.matches[0]
    assert match.chainId == chain_id
    assert match.pageNodeId == _ORDERS_PAGE
    # 引文落在起点段：targets 由服务端算出，不由模型提供
    assert match.targets == [_EXPORT_BUTTON]
    # 置信度封顶，自报值留档
    assert match.confidence == 0.6
    assert match.modelConfidence == 0.95
    # 链路 id 要能被编排层拿去填 interactionChains
    assert outcome.chain_ids == [chain_id]
    assert match.routeId == "src/router.tsx#/orders"


async def test_locate_drops_chain_from_another_page() -> None:
    """「页面 A + 页面 B 的链路」这种自相矛盾的组合必须整条拒绝。"""
    source = user_graph()
    # 给 UserList 加一个按钮，让它有链路；再用 UserDetail 作页面 + UserList 的链路
    graph_with_buttons = _orders_graph()
    chains = derive_chains(graph_with_buttons)
    chain_id = _export_chain_id(chains)
    # 把 UserDetail 也塞进图里，让 index 里有两个页面
    detail = page("src/pages/UserDetail.tsx#UserDetail", "UserDetail", texts=["用户详情"])
    route_detail = node(
        "src/router.tsx#/users/:userId",
        NodeKind.route,
        "/users/:userId",
        file="src/router.tsx",
        attrs={},
    )
    mixed = graph(
        [*graph_with_buttons.nodes, detail, route_detail],
        [
            *graph_with_buttons.edges,
            edge(route_detail.id, detail.id, EdgeKind.renders),
        ],
    )

    outcome, *_ = await _run(
        mixed,
        _response(
            {
                "featureId": "f1",
                "pageNodeId": detail.id,
                "chainId": chain_id,
                "quote": "导出",
                "confidence": 0.9,
            }
        ),
    )

    assert outcome.matches == []
    assert [item.reason for item in outcome.dropped] == [Reason.chain_not_in_candidates]
    assert outcome.dropped[0].chainId == chain_id
    assert source.nodes  # 保持夹具语义：另一份图未受影响


async def test_locate_drops_fabricated_chain() -> None:
    source = _orders_graph()
    outcome, *_ = await _run(
        source,
        _response(
            {
                "featureId": "f1",
                "pageNodeId": _ORDERS_PAGE,
                "chainId": f"{_EXPORT_BUTTON}::编造的路径",
                "quote": "导出",
                "confidence": 0.9,
            }
        ),
    )

    assert outcome.matches == []
    assert [item.reason for item in outcome.dropped] == [Reason.chain_not_in_candidates]


async def test_locate_rejects_quote_outside_the_selected_chain() -> None:
    """引文必须是**所选链路**的原文：选了外链却引导出按钮的文案，等于没说清落点。"""
    source = _orders_graph()
    chains = derive_chains(source)
    outcome, *_ = await _run(
        source,
        _response(
            {
                "featureId": "f1",
                "pageNodeId": _ORDERS_PAGE,
                "chainId": _github_chain_id(chains),
                "quote": "导出",
                "confidence": 0.9,
            }
        ),
    )

    assert outcome.matches == []
    assert [item.reason for item in outcome.dropped] == [Reason.quote_not_found]
    assert outcome.dropped[0].quote == "导出"


async def test_locate_accepts_external_link_chain_with_termination() -> None:
    """外链是合法落点：链路到此中断，但要如实标记而不是假装跳到了站内页面。"""
    source = _orders_graph()
    chains = derive_chains(source)

    outcome, *_ = await _run(
        source,
        _response(
            {
                "featureId": "f1",
                "pageNodeId": _ORDERS_PAGE,
                "chainId": _github_chain_id(chains),
                "quote": "https://github.com/x/y",
                "confidence": 0.8,
            }
        ),
        requirement="点击 github 页面跳转",
    )

    assert len(outcome.matches) == 1
    contract = chains.contracts_for(outcome.chain_ids)[0]
    assert contract.terminatedAt == TERMINATED_EXTERNAL
    assert contract.href == "https://github.com/x/y"
    assert [step.role.value for step in contract.steps] == ["element"]


async def test_locate_allows_page_level_match_without_chain() -> None:
    """整页级需求可以不给 chainId；此时引文退到页面语料。"""
    source = _orders_graph()
    outcome, *_ = await _run(
        source,
        _response(
            {
                "featureId": "f1",
                "pageNodeId": _ORDERS_PAGE,
                "quote": "订单列表",
                "confidence": 0.7,
            }
        ),
        requirement="订单列表在做什么",
    )

    assert len(outcome.matches) == 1
    match = outcome.matches[0]
    assert match.chainId is None
    assert match.targets is None
    assert outcome.chain_ids == []
    # 页面级匹配的证据落在页面节点上
    assert match.verifiedEvidence[0].nodeId == _ORDERS_PAGE


async def test_locate_drops_unknown_page() -> None:
    source = _orders_graph()
    outcome, *_ = await _run(
        source,
        _response(
            {
                "featureId": "f1",
                "pageNodeId": "src/pages/Invented.tsx#Invented",
                "quote": "导出",
                "confidence": 0.9,
            }
        ),
    )

    assert outcome.matches == []
    assert [item.reason for item in outcome.dropped] == [Reason.node_not_in_candidates]


async def test_locate_drops_unknown_feature_and_bad_shape() -> None:
    source = _orders_graph()
    outcome, *_ = await _run(
        source,
        _response(
            {"featureId": "f9", "pageNodeId": _ORDERS_PAGE, "quote": "导出", "confidence": 0.9},
            {"featureId": "f1", "pageNodeId": _ORDERS_PAGE, "chainId": 42, "quote": "导出"},
        ),
    )

    assert outcome.matches == []
    reasons = {item.reason for item in outcome.dropped}
    assert reasons == {Reason.unknown_feature, Reason.invalid_shape}


async def test_locate_flags_shared_quote_as_not_discriminative() -> None:
    """两个页面都有同一句话时，引文证明不了归属——保留匹配但必须标注。"""
    first = page("src/pages/A.tsx#A", "A", texts=["暂无数据"])
    second = page("src/pages/B.tsx#B", "B", texts=["暂无数据"])
    route_a = node("src/router.tsx#/a", NodeKind.route, "/a", file="src/router.tsx", attrs={})
    route_b = node("src/router.tsx#/b", NodeKind.route, "/b", file="src/router.tsx", attrs={})
    source = graph(
        [first, second, route_a, route_b],
        [
            edge(route_a.id, first.id, EdgeKind.renders),
            edge(route_b.id, second.id, EdgeKind.renders),
        ],
    )

    outcome, *_ = await _run(
        source,
        _response(
            {"featureId": "f1", "pageNodeId": first.id, "quote": "暂无数据", "confidence": 0.6},
            unmatched=[{"featureId": "f2", "reason": "没有支付相关页面"}],
        ),
        requirement="列表空态提示",
    )

    assert len(outcome.matches) == 1
    flagged = [w for w in outcome.warnings if w.code == "quote_not_discriminative"]
    assert flagged
    assert second.id in flagged[0].message

    # 差距表 #24：歧义引文降权（与 judge 路径同一份规则）
    match = outcome.matches[0]
    assert match.confidence == pytest.approx(0.6 * 0.8)
    assert match.modelConfidence == 0.6


async def test_locate_defers_on_non_retryable_error() -> None:
    source = _orders_graph()
    outcome, llm, features = await _run(
        source, ["{}"], requirement="订单列表支持导出"
    )
    assert outcome.degraded is False
    # 空 matches 也算有效输出：模型可以说「都不匹配」。
    assert outcome.matches == []
    assert [item.featureId for item in outcome.unmatched] == [f.id for f in features]

    llm_401 = FakeLLMClient(error=LLMCallError("HTTP 401", retryable=False))
    features2 = decompose("订单列表支持导出")
    index, chains = _index(source)
    failed = await locate(
        features2, index, chains, source, llm_401, requirement="订单列表支持导出", max_calls=4
    )

    assert failed.degraded is True
    assert len(llm_401.calls) == 1
    assert failed.matches == []
    assert failed.warnings[0].code == "llm_unavailable"
    assert "1 次尝试" in failed.warnings[0].message


async def test_locate_retries_then_succeeds() -> None:
    source = _orders_graph()
    chains = derive_chains(source)
    good = _response(
        {
            "featureId": "f1",
            "pageNodeId": _ORDERS_PAGE,
            "chainId": _export_chain_id(chains),
            "quote": "导出",
            "confidence": 0.7,
        }
    )
    outcome, llm, _ = await _run(source, ["这不是 JSON", good])

    assert outcome.calls_used == 2
    assert len(outcome.matches) == 1
    assert "上一次输出无效" in llm.calls[1][1]


async def test_locate_prompt_includes_terminated_chains() -> None:
    """prompt 要说明链路断点，否则模型会把外链当成站内跳转来叙述。"""
    source = _orders_graph()
    _, llm, _ = await _run(
        source,
        _response(),
        requirement="点击 github 页面跳转",
    )

    user_prompt = llm.calls[0][1]
    assert TERMINATED_EXTERNAL in user_prompt
    assert "https://github.com/x/y" in user_prompt
    assert "chainId" in llm.calls[0][0]


async def test_locate_reports_empty_index_without_calling_model() -> None:
    """没有页面时不该烧一次模型调用。"""
    source = graph([], [])
    features = decompose("订单列表支持导出")
    chains = derive_chains(source)
    index = build_page_index(source, chains)
    llm = FakeLLMClient()

    outcome = await locate(
        features, index, chains, source, llm, requirement="订单列表", max_calls=4
    )

    assert llm.calls == []
    assert outcome.degraded is True
    assert outcome.warnings[0].code == "locate_skipped"


# ---------------------------------------------------------------------------
# 词法候选的保留与标注
# ---------------------------------------------------------------------------


def test_merge_lexical_candidates_dedupes_by_node_keeping_highest() -> None:
    source = user_graph()
    features = decompose("用户列表增加状态筛选。用户详情页查看状态")
    results = [recall(source, feature.terms) for feature in features]

    merged = merge_lexical_candidates(*results)

    by_id = {detail.candidate.nodeId: detail for detail in merged}
    assert len(by_id) == len(merged), "同一页面不该在候选里出现两次"
    scores = [detail.candidate.score for detail in merged]
    assert scores == sorted(scores, reverse=True)


def test_semantic_signals_annotates_rank_without_touching_score() -> None:
    """semanticRank 只落名次：候选的分数仍是词法的可解释得分。"""
    source = user_graph()
    features = decompose("用户列表增加状态筛选")
    candidates = recall(source, features[0].terms).candidates
    before = {detail.candidate.nodeId: detail.candidate.score for detail in candidates}

    from app.contracts.requirement import Match

    target = candidates[0].candidate.nodeId
    match = Match(
        featureId="f1",
        pageNodeId=target,
        confidence=0.6,
        source="ai",
        subgraph={"nodeIds": [], "edgeIds": [], "counts": {
            "component": 0, "hook": 0, "state": 0, "api": 0,
            "event": 0, "fn": 0, "route": 0, "element": 0,
        }},
    )
    semantic_signals(candidates, [match])

    after = {detail.candidate.nodeId: detail.candidate.score for detail in candidates}
    assert after == before
    annotated = next(d for d in candidates if d.candidate.nodeId == target)
    kinds = [signal.kind.value for signal in annotated.candidate.signals]
    assert "semanticRank" in kinds
    rank_signal = next(s for s in annotated.candidate.signals if s.kind.value == "semanticRank")
    assert rank_signal.value == "rank=1"
    assert rank_signal.score == 0.0


def test_semantic_signals_is_idempotent() -> None:
    """重复标注不该在候选上叠加多个 rank。"""
    source = user_graph()
    features = decompose("用户列表增加状态筛选")
    candidates = recall(source, features[0].terms).candidates
    from app.contracts.requirement import Match

    match = Match(
        featureId="f1",
        pageNodeId=candidates[0].candidate.nodeId,
        confidence=0.6,
        source="ai",
        subgraph={"nodeIds": [], "edgeIds": [], "counts": {
            "component": 0, "hook": 0, "state": 0, "api": 0,
            "event": 0, "fn": 0, "route": 0, "element": 0,
        }},
    )
    semantic_signals(candidates, [match])
    semantic_signals(candidates, [match])

    ranks = [
        signal
        for signal in candidates[0].candidate.signals
        if signal.kind.value == "semanticRank"
    ]
    assert len(ranks) == 1


# ---------------------------------------------------------------------------
# 未被选中的链路仍然可查（供前端「为什么不是这条」）
# ---------------------------------------------------------------------------


def test_injected_handler_chain_is_selectable_and_marked() -> None:
    source = graph(
        [
            page("src/pages/Table.tsx#Table", "Table", texts=["表格"]),
            node(
                "src/pages/Table.tsx#Table/jsx:RowItem",
                NodeKind.element,
                "<RowItem>",
                file="src/pages/Table.tsx",
                attrs={
                    "tag": "RowItem",
                    "viewRole": "control",
                    "viewText": "行",
                    "events": [
                        {
                            "name": "onSelect",
                            "handler": "props.onSelect",
                            "kind": "other",
                            "handlerName": None,
                            "targets": [],
                        }
                    ],
                },
            ),
            node(
                "src/router.tsx#/table",
                NodeKind.route,
                "/table",
                file="src/router.tsx",
                attrs={},
            ),
        ],
        [
            edge("src/router.tsx#/table", "src/pages/Table.tsx#Table", EdgeKind.renders),
            edge(
                "src/pages/Table.tsx#Table",
                "src/pages/Table.tsx#Table/jsx:RowItem",
                EdgeKind.contains,
            ),
        ],
    )
    chains = derive_chains(source)
    chain = chains.for_page("src/pages/Table.tsx#Table")[0]
    assert chain.terminated_at == TERMINATED_INJECTED_HANDLER

    index = build_page_index(source, chains)
    quoted = chain.corpus[0]
    assert any(quoted in entry.to_payload().get("quotes", []) or quoted in [
        q for c in entry.to_payload()["chains"] for q in c["quotes"]  # type: ignore[index]
    ] for entry in index.pages)


@pytest.mark.parametrize("chain_selector", ["export", "github"])
def test_every_chain_is_in_the_index(chain_selector: str) -> None:
    source = _orders_graph()
    index, chains = _index(source)
    payload = index.by_id[_ORDERS_PAGE].to_payload()
    listed = {chain["chainId"] for chain in payload["chains"]}

    assert chains.by_id, chain_selector in {"export", "github"}
    assert set(chains.by_id) == listed


# ---------------------------------------------------------------------------
# 端到端：用户最初反馈的场景
# ---------------------------------------------------------------------------
#
# 「点击github页面跳转」——需求里的 `github` 是驼峰被切成 git+hub 后永远命中不了
# 整词，词表路线下候选为空。这条测试锁定的是新路径：即使词法一个候选都没召回，
# 模型仍能从页面索引里看到 `<a> GitHub` 这条链路（含 href 与外链断点）并选中它。


_GITHUB_ELEMENT = "src/core/Header.tsx#Header/jsx:a"


def _layout_graph() -> IRGraph:
    """`FullLayout → Header → <a href=github>`，layout 挂在三条路由下。"""
    full_layout = page("src/core/FullLayout.tsx#FullLayout", "FullLayout", texts=["欢迎"])
    header = page("src/core/Header.tsx#Header", "Header", texts=["GitHub", "Posts"])
    github = node(
        _GITHUB_ELEMENT,
        NodeKind.element,
        "<a> GitHub",
        file="src/core/Header.tsx",
        attrs={
            "tag": "a",
            "viewRole": "link",
            "viewText": "GitHub",
            "href": "https://github.com/naserrasoulii/feature-based-react",
            "external": True,
        },
    )
    routes = [
        node(f"src/router.tsx#{path}", NodeKind.route, path, file="src/router.tsx", attrs={})
        for path in ("/", "/posts", "/products")
    ]
    return graph(
        [full_layout, header, github, *routes],
        [
            *(edge(route.id, full_layout.id, EdgeKind.renders) for route in routes),
            edge(full_layout.id, header.id, EdgeKind.renders),
            edge(header.id, github.id, EdgeKind.contains),
        ],
    )


async def test_github_link_is_locatable_without_lexicon_hit() -> None:
    """需求命不中词表时，AI 仍然要能看到并选中这条链路。"""
    source = _layout_graph()
    requirement = "点击github页面跳转"
    features = decompose(requirement)
    chains = derive_chains(source)

    # 前提：词法召回确实一个候选都没有（否则这条测试没验证到新路径）
    lexical = recall(source, features[0].terms)
    assert lexical.candidates == [], "词法若已召回，这条测试就失去意义"

    index = build_page_index(source, chains, recall_result=lexical)
    assert len(index.pages) == 1, "页面索引不能依赖词法召回是否命中"
    entry = index.pages[0]
    assert entry.node_id == "src/core/FullLayout.tsx#FullLayout"
    # layout 挂在三条路由下，索引要如实给出全部路径
    assert entry.route_paths == ("/", "/posts", "/products")

    github_chain = next(
        chain for chain in chains.by_id.values() if chain.element.name == "<a> GitHub"
    )
    outcome = await locate(
        features,
        index,
        chains,
        source,
        FakeLLMClient(
            [
                _response(
                    {
                        "featureId": "f1",
                        "pageNodeId": entry.node_id,
                        "chainId": github_chain.id,
                        "quote": "https://github.com/naserrasoulii/feature-based-react",
                        "confidence": 0.9,
                        "reason": "页面右上角的 GitHub 外链",
                    }
                )
            ]
        ),
        requirement=requirement,
        max_calls=4,
    )

    assert outcome.dropped == []
    match = outcome.matches[0]
    assert match.pageNodeId == entry.node_id
    assert match.chainId == github_chain.id
    assert match.targets == [_GITHUB_ELEMENT]

    contract = chains.contracts_for(outcome.chain_ids)[0]
    assert contract.href == "https://github.com/naserrasoulii/feature-based-react"
    assert contract.terminatedAt == TERMINATED_EXTERNAL
    assert contract.path.nodeIds == [_GITHUB_ELEMENT]
    # 外链没有站内下游，edgeIds 为空是对的——不编造跳转边
    assert contract.path.edgeIds == []
    assert contract.steps[0].file == "src/core/Header.tsx"
    assert contract.steps[0].loc is not None

