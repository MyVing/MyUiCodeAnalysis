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
