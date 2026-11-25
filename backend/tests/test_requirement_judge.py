"""模型判定层测试：校验规则、置信度封顶、降级与重试。

全部用 FakeLLMClient——判定逻辑的正确性不能依赖真实模型的输出分布。
"""

from __future__ import annotations

import json

import pytest

from app.contracts import IRGraph
from app.contracts.requirement import Reason
from app.requirement.features import decompose
from app.requirement.judge import judge
from app.requirement.llm import LLMCallError
from app.requirement.recall import recall
from tests.fakes import FakeLLMClient
from tests.graph_fixture import user_graph


def _candidates(graph: IRGraph, requirement: str = "用户列表增加状态筛选"):
    features = decompose(requirement)
    return features, recall(graph, features[0].terms).candidates


def _valid_response(graph: IRGraph, *, confidence: float = 0.9) -> str:
    """一条合法的模型输出：选 UserList，引文取它的 uiText。"""
    return json.dumps(
        {
            "matches": [
                {
                    "featureId": "f1",
                    "pageNodeId": "src/pages/UserList.tsx#UserList",
                    "quote": "搜索用户",
                    "confidence": confidence,
                    "reason": "页面文案与需求一致",
                }
            ],
            "unmatched": [],
        },
        ensure_ascii=False,
    )


async def test_judge_accepts_valid_match_and_caps_confidence() -> None:
    graph = user_graph()
    features, candidates = _candidates(graph)
    llm = FakeLLMClient([_valid_response(graph, confidence=0.9)])

    outcome = await judge(
        features, candidates, graph, llm,
        requirement="用户列表增加状态筛选", max_calls=4,
    )

    assert outcome.calls_used == 1
    assert outcome.dropped == []
    assert outcome.unmatched == []
    match = outcome.matches[0]
    assert match.pageNodeId == "src/pages/UserList.tsx#UserList"
    assert match.featureId == "f1"
    # AI 推断的置信度硬上限 0.6；模型自报值只记在 modelConfidence
    assert match.confidence == 0.6
    assert match.modelConfidence == 0.9
    assert match.quote == "搜索用户"
    assert match.verifiedEvidence is not None
    assert len(match.verifiedEvidence) == 1
    evidence = match.verifiedEvidence[0]
    assert evidence.nodeId == "src/pages/UserList.tsx#UserList"
    assert evidence.quote == "搜索用户"
    assert evidence.via == "uiText"
    assert evidence.source.value == "static"
    assert evidence.file == "src/pages/UserList.tsx"
    assert evidence.loc is not None and evidence.loc.line == 1
    assert evidence.confidence == 1.0
    assert match.routeId == "src/router.tsx#/users"
    assert "src/pages/UserList.tsx#UserList" in match.subgraph.nodeIds
    assert match.subgraph.counts.route == 1


async def test_judge_drops_match_outside_candidates() -> None:
    graph = user_graph()
    features, candidates = _candidates(graph)
    response = json.dumps(
        {
            "matches": [
                {
                    "featureId": "f1",
                    "pageNodeId": "src/pages/Invented.tsx#Invented",
                    "quote": "搜索用户",
                    "confidence": 0.9,
                }
            ],
            "unmatched": [],
        }
    )
    outcome = await judge(
        features, candidates, graph, FakeLLMClient([response]), requirement="用户列表", max_calls=4
    )

    assert outcome.matches == []
    assert [dropped.reason for dropped in outcome.dropped] == [Reason.node_not_in_candidates]
    assert outcome.dropped[0].nodeId == "src/pages/Invented.tsx#Invented"
    assert outcome.unmatched[0].featureId == "f1"
    # 未匹配要附召回前三，供人判断是需求超纲还是召回不足
    assert len(outcome.unmatched[0].candidates or []) == 2


async def test_judge_drops_fabricated_quote() -> None:
    graph = user_graph()
    features, candidates = _candidates(graph)
    response = json.dumps(
        {
            "matches": [
                {
                    "featureId": "f1",
                    "pageNodeId": "src/pages/UserList.tsx#UserList",
                    "quote": "这段话模型是编的",
                    "confidence": 0.9,
                }
            ],
            "unmatched": [],
        }
    )
    outcome = await judge(
        features, candidates, graph, FakeLLMClient([response]), requirement="用户列表", max_calls=4
    )

    assert outcome.matches == []
    assert [dropped.reason for dropped in outcome.dropped] == [Reason.quote_not_found]
    assert outcome.dropped[0].quote == "这段话模型是编的"


async def test_judge_drops_unknown_feature_and_invalid_shape() -> None:
    graph = user_graph()
    features, candidates = _candidates(graph)
    response = json.dumps(
        {
            "matches": [
                {
                    "featureId": "f99",
                    "pageNodeId": "src/pages/UserList.tsx#UserList",
                    "quote": "搜索用户",
                    "confidence": 0.9,
                },
                {
                    "featureId": "f1",
                    "pageNodeId": 123,
                    "quote": "搜索用户",
                    "confidence": "高",
                },
            ],
            "unmatched": [],
        }
    )
    outcome = await judge(
        features, candidates, graph, FakeLLMClient([response]), requirement="用户列表", max_calls=4
    )

    assert outcome.matches == []
    reasons = [dropped.reason for dropped in outcome.dropped]
    assert Reason.unknown_feature in reasons
    assert Reason.invalid_shape in reasons


async def test_judge_records_unmatched_with_model_reason() -> None:
    graph = user_graph()
    features = decompose("用户列表增加状态筛选。支付网关对接")
    # 第二个功能点（支付）召不回候选，整体候选仍来自第一个功能点
    candidates = recall(graph, features[0].terms).candidates
    response = json.dumps(
        {
            "matches": [
                {
                    "featureId": "f1",
                    "pageNodeId": "src/pages/UserList.tsx#UserList",
                    "quote": "搜索用户",
                    "confidence": 0.5,
                }
            ],
            "unmatched": [{"featureId": "f2", "reason": "候选里没有支付相关页面"}],
        },
        ensure_ascii=False,
    )
    outcome = await judge(
        features, candidates, graph, FakeLLMClient([response]), requirement="需求", max_calls=4
    )

    assert len(outcome.matches) == 1
    assert [item.featureId for item in outcome.unmatched] == ["f2"]
    assert outcome.unmatched[0].reason == "候选里没有支付相关页面"


async def test_judge_retries_on_invalid_json_then_succeeds() -> None:
    graph = user_graph()
    features, candidates = _candidates(graph)
    llm = FakeLLMClient(["这不是 JSON", "```json\n" + _valid_response(graph) + "\n```"])

    outcome = await judge(features, candidates, graph, llm, requirement="用户列表", max_calls=4)

    assert outcome.calls_used == 2
    assert len(outcome.matches) == 1
    # 重试的 prompt 里要带上失败原因，模型才知道要修正什么
    assert "上一次输出无效" in llm.calls[1][1]


async def test_judge_degrades_after_budget_exhausted() -> None:
    graph = user_graph()
    features, candidates = _candidates(graph)
    llm = FakeLLMClient(error=LLMCallError("连接被重置"))

    outcome = await judge(features, candidates, graph, llm, requirement="用户列表", max_calls=4)

    assert outcome.calls_used == 4
    assert outcome.matches == []
    assert [item.featureId for item in outcome.unmatched] == [feature.id for feature in features]
    warning = outcome.warnings[0]
    assert warning.code == "llm_unavailable"
    assert "连接被重置" in warning.message
    # 预算内每次失败都要重试
    assert len(llm.calls) == 4


async def test_judge_stops_immediately_on_non_retryable_error() -> None:
    """鉴权失败、模型不存在这类错误重发同样的请求必然同样失败，不该烧预算。"""
    graph = user_graph()
    features, candidates = _candidates(graph)
    llm = FakeLLMClient(error=LLMCallError("HTTP 401", retryable=False))

    outcome = await judge(features, candidates, graph, llm, requirement="用户列表", max_calls=4)

    assert outcome.calls_used == 1
    assert len(llm.calls) == 1
    assert outcome.matches == []
    warning = outcome.warnings[0]
    assert warning.code == "llm_unavailable"
    # 告警要如实说「1 次尝试」，不能沿用 max_calls 的数字误导排查方向
    assert "1 次尝试" in warning.message
    assert "HTTP 401" in warning.message


async def test_judge_skips_model_call_when_no_candidates() -> None:
    bare_graph = user_graph()
    features = decompose("支付网关对接")
    llm = FakeLLMClient()

    outcome = await judge(features, [], bare_graph, llm, requirement="支付网关对接", max_calls=4)

    assert llm.calls == []
    assert outcome.calls_used == 0
    assert [item.featureId for item in outcome.unmatched] == [feature.id for feature in features]
    assert outcome.warnings[0].code == "judge_skipped"


@pytest.mark.parametrize("fence", ["", "```json\n", "```\n"])
async def test_judge_tolerates_code_fences(fence: str) -> None:
    graph = user_graph()
    features, candidates = _candidates(graph)
    response = json.dumps(
        {
            "matches": [
                {
                    "featureId": "f1",
                    "pageNodeId": "src/pages/UserList.tsx#UserList",
                    "quote": "搜索用户",
                    "confidence": 0.4,
                }
            ],
            "unmatched": [],
        }
    )
    text = f"{fence}{response}{'```' if fence else ''}"
    outcome = await judge(
        features, candidates, graph, FakeLLMClient([text]), requirement="用户列表", max_calls=4
    )

    assert len(outcome.matches) == 1
    assert outcome.matches[0].confidence == 0.4


# ---------------------------------------------------------------------------
# 引文校验：整条相等优先，归一化子串兜底（设计文档 §6.5）
# ---------------------------------------------------------------------------


def _match_response(quote: str, node_id: str = "src/pages/UserList.tsx#UserList") -> str:
    return json.dumps(
        {
            "matches": [
                {"featureId": "f1", "pageNodeId": node_id, "quote": quote, "confidence": 0.8}
            ],
            "unmatched": [],
        },
        ensure_ascii=False,
    )


async def _judge_with_quote(graph: IRGraph, quote: str) -> object:
    features, candidates = _candidates(graph)
    return await judge(
        features, candidates, graph, FakeLLMClient([_match_response(quote)]),
        requirement="用户列表增加状态筛选", max_calls=4,
    )


async def test_quote_shorter_than_corpus_is_rejected() -> None:
    """短于长度下限的片段一律拒绝：它命中不了多少东西，也证明不了什么。"""
    graph = user_graph()
    outcome = await _judge_with_quote(graph, "搜索")

    assert outcome.matches == []
    assert [dropped.reason for dropped in outcome.dropped] == [Reason.quote_not_found]


async def test_quote_substring_accepted_only_when_at_least_four_chars() -> None:
    """模型截取语料里一段文案（原文更长的那个方向）时，按截取处理。"""
    graph = user_graph()
    # 语料里有 prop 描述「是否显示筛选栏」；模型引其中「显示筛选栏」
    outcome = await _judge_with_quote(graph, "显示筛选栏")

    assert len(outcome.matches) == 1
    match = outcome.matches[0]
    # 落库的是语料里的原文，不是模型写的片段：证据必须能在代码里原样找到
    assert match.quote == "是否显示筛选栏"
    assert match.verifiedEvidence is not None
    assert match.verifiedEvidence[0].quote == "是否显示筛选栏"
    # 放宽必须留痕，否则无法复核模型到底引了哪一段
    partial = [w for w in outcome.warnings if w.code == "quote_partial_match"]
    assert len(partial) == 1
    assert partial[0].severity.value == "info"
    assert "显示筛选栏" in partial[0].message
    assert "是否显示筛选栏" in partial[0].message


async def test_exact_quote_emits_no_partial_warning() -> None:
    graph = user_graph()
    outcome = await _judge_with_quote(graph, "搜索用户")

    assert len(outcome.matches) == 1
    assert outcome.matches[0].quote == "搜索用户"
    assert [w for w in outcome.warnings if w.code == "quote_partial_match"] == []


async def test_quote_normalization_ignores_width_and_whitespace() -> None:
    """全角括号 / 多余空白这类差异不该把正确引文判成错的。"""
    graph = user_graph()
    # 语料原文是「搜索用户」；模型输出里插入空白——归一化后等值，按整条命中处理
    outcome = await _judge_with_quote(graph, "  搜索用户  ")

    assert len(outcome.matches) == 1
    assert outcome.matches[0].quote == "搜索用户"
    # 这是等值命中而不是截取，不该留 partial 痕迹
    assert [w for w in outcome.warnings if w.code == "quote_partial_match"] == []


async def test_ambiguous_substring_is_rejected() -> None:
    """同一片段对上多段原文时无法证明模型指的是哪一段，按校验失败处理。"""
    from app.requirement.judge import match_quote

    # 「显示筛选栏」同时是两条不同原文的片段（一条 prop 描述、一条可见文案）
    assert (
        match_quote(
            "显示筛选栏",
            ["是否显示筛选栏", "顶部显示筛选栏组件", "搜索用户", "姓名"],
        )
        is None
    )


async def test_short_substring_never_matches() -> None:
    """超短片段的"命中"没有信息量，必须拒绝，否则整道闸形同虚设。"""
    from app.requirement.judge import match_quote

    assert match_quote("列表", ["用户列表", "订单列表", "列表标题"]) is None
    assert match_quote("状态", ["待审核状态"]) is None


async def test_fabricated_quote_still_rejected_after_relaxation() -> None:
    """放宽的是形态，不是真实性：语料里根本没有的内容仍要整条丢弃。"""
    graph = user_graph()
    outcome = await _judge_with_quote(graph, "用户列表支持导出 Excel")

    assert outcome.matches == []
    assert [dropped.reason for dropped in outcome.dropped] == [Reason.quote_not_found]


async def test_quote_shared_across_candidates_is_flagged() -> None:
    """引文在多处出现时不能证明归属：匹配保留，但必须标出歧义。"""
    graph = user_graph()
    features, candidates = _candidates(graph)
    assert len(candidates) > 1, "夹具应当召回多个候选，否则这条测试没有意义"

    # 让两个候选都含同一段原文，模拟「暂无数据」这类共通文案
    for detail in candidates:
        detail.quote_corpus.append("暂无数据")
        detail.ui_texts.append("暂无数据")

    target = candidates[0].candidate.nodeId
    other = candidates[1].candidate.nodeId
    response = json.dumps(
        {
            "matches": [
                {
                    "featureId": "f1",
                    "pageNodeId": target,
                    "quote": "暂无数据",
                    "confidence": 0.8,
                }
            ],
            "unmatched": [],
        },
        ensure_ascii=False,
    )
    outcome = await judge(
        features, candidates, graph, FakeLLMClient([response]),
        requirement="用户列表增加状态筛选", max_calls=4,
    )

    # 不丢弃：引文确实在语料里，模型没编造；但归属不可判定必须留痕
    assert len(outcome.matches) == 1
    flagged = [w for w in outcome.warnings if w.code == "quote_not_discriminative"]
    assert len(flagged) == 1
    assert flagged[0].severity.value == "warning"
    assert other in flagged[0].message

    # 差距表 #24：歧义引文降权——0.8 先封顶到 0.6，再按 0.8 系数降权；
    # 模型原始值保留在 modelConfidence 里，降权幅度可审计
    match = outcome.matches[0]
    assert match.confidence == pytest.approx(0.48)
    assert match.modelConfidence == 0.8
    assert "降权" in flagged[0].message


async def test_unique_quote_is_not_flagged_as_ambiguous() -> None:
    graph = user_graph()
    outcome = await _judge_with_quote(graph, "搜索用户")

    assert len(outcome.matches) == 1
    assert [w for w in outcome.warnings if w.code == "quote_not_discriminative"] == []
