"""推断章节测试：mock LLM——引文逐字校验、0.6 封顶、dry-run 留空。"""

from __future__ import annotations

import json

from app.contracts.requirement import Reason
from app.requirement.llm import LLMCallError
from app.specdoc.collect import collect_materials
from app.specdoc.infer import infer_sections
from tests.fakes import FakeLLMClient
from tests.specdoc_fixture import spec_graph, spec_requirement_map

PAGE_ID = "src/pages/Transfer.tsx#Transfer"


def _materials():
    graph_ = spec_graph()
    return collect_materials(spec_requirement_map(graph_), graph_)


_VALID_STEPS = {
    "steps": [
        {
            "featureId": "f1",
            "chainId": next(
                chain.id for chain in _materials().pages[0].chains
                if chain.element.node_id.endswith("jsx:button")
            ),
                "nodeId": "src/pages/Transfer.tsx#Transfer/jsx:button",
            "precondition": ["已登录"],
            "actions": ["选择付款账户，系统展示该账户可用余额", "点击转账按钮，系统提示转账成功"],
            "quote": "转账",
            "confidence": 0.9,
        }
    ]
}
_VALID_RULES = {
    "rules": [
        {
            "featureId": "f1",
            "chainId": next(
                chain.id for chain in _materials().pages[0].chains
                if chain.element.node_id.endswith("jsx:button")
            ),
            "elementId": "src/pages/Transfer.tsx#Transfer/jsx:button",
            "category": "validation",
            "target": "付款账户",
            "condition": "未选择付款账户",
            "result": "转账按钮置灰，不可提交",
            "quote": "转账",
            "confidence": 0.8,
        }
    ]
}
_VALID_MAPPINGS = {
    "fieldMappings": [
        {
            "frontField": "payeeAccount",
            "backendParam": "payee_account",
            "elementId": "src/pages/Transfer.tsx#Transfer/jsx:select",
            "api": "POST /api/transfer",
            "quote": "POST /api/transfer",
            "confidence": 0.7,
        }
    ]
}


def _materials():
    graph_ = spec_graph()
    return collect_materials(spec_requirement_map(graph_), graph_)


def _llm_with(steps=_VALID_STEPS, rules=_VALID_RULES, mappings=_VALID_MAPPINGS) -> FakeLLMClient:
    return FakeLLMClient(
        [json.dumps(steps), json.dumps(rules), json.dumps(mappings)]
    )


async def test_valid_outputs_are_capped_and_bound_to_pages() -> None:
    """引文命中：AI 证据封顶 0.6（0.9 自报也不透传）；步骤/规则挂到定位页面。"""
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement,
        requirement_map,
        _materials(),
        _llm_with(),
        max_calls=2,
    )
    assert len(outcome.steps) == 1
    step = outcome.steps[0]
    assert step.id == "step.f1"
    assert step.pageId == PAGE_ID
    evidence = step.evidence[0]
    assert evidence.source.value == "ai"
    assert evidence.confidence == 0.6
    assert evidence.nodeId == "src/pages/Transfer.tsx#Transfer/jsx:button"
    assert evidence.quote == "转账"

    assert len(outcome.rules) == 1
    rule = outcome.rules[0]
    assert rule.category.value == "validation"
    assert rule.pageId == PAGE_ID
    assert rule.evidence[0].confidence == 0.6

    assert len(outcome.field_mappings) == 1
    assert outcome.field_mappings[0].pageId == PAGE_ID
    assert outcome.dropped == []


async def test_list_style_mapping_quote_falls_back_to_lines() -> None:
    """模型把 quote 写成关键词罗列时拆行兜底：能逐字命中的行救回该条映射。

    回归背景：register 页实测中模型输出多行列表式 quote，整体逐字匹配必然
    失败，字段映射整批被拒。拆出的每一行仍走同一套逐字校验，纪律不变。
    """
    listy = {
        "fieldMappings": [
            {
                "frontField": "payeeAccount",
                "backendParam": "payee_account",
                "elementId": "src/pages/Transfer.tsx#Transfer/jsx:button",
                "api": "POST /api/transfer",
                "quote": "- payeeAccount\n- payee_account\n- POST /api/transfer",
                "confidence": 0.7,
            }
        ]
    }
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement,
        requirement_map,
        _materials(),
        _llm_with(mappings=listy),
        max_calls=1,
    )
    assert len(outcome.field_mappings) == 1
    mapping = outcome.field_mappings[0]
    assert mapping.pageId == PAGE_ID
    assert mapping.quote == "POST /api/transfer"
    assert outcome.dropped == []


async def test_handler_source_quote_resolves_to_handler_evidence() -> None:
    """事件 handler 源码可被规则引用，并解析到 handler 的真实节点与位置。"""
    rules = {
        "rules": [
            {
                "featureId": "f2",
                "chainId": next(
                    chain.id for chain in _materials().pages[0].chains
                    if chain.element.node_id.endswith("jsx:select")
                ),
                "nodeId": "src/pages/Transfer.tsx#handleAcctChange",
                "category": "linkage",
                "elementId": "src/pages/Transfer.tsx#Transfer/jsx:select",
                "target": "付款账户",
                "condition": "账户变更时",
                "result": "查询对应账户余额",
                "quote": "function handleAcctChange(value) { queryBalance(value); }",
                "confidence": 0.8,
            }
        ]
    }
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement,
        requirement_map,
        _materials(),
        _llm_with(rules=rules),
        max_calls=1,
    )

    assert len(outcome.rules) == 1
    evidence = outcome.rules[0].evidence[0]
    assert evidence.nodeId == "src/pages/Transfer.tsx#handleAcctChange"
    assert evidence.file == "src/pages/Transfer.tsx"
    assert evidence.loc is not None and evidence.loc.line == 1
    assert outcome.dropped == []


async def test_rule_quote_in_structured_pack_is_accepted_past_legacy_line_limit() -> None:
    """Structured evidence replaces the old flat 120-line prompt cutoff."""
    graph_ = spec_graph()
    page_node = next(node for node in graph_.nodes if node.id == PAGE_ID)
    page_node.attrs["uiText"]["texts"] = [
        {"text": f"页面文本 {index}", "kind": "text"} for index in range(130)
    ]
    requirement_map = spec_requirement_map(graph_)
    rules = {
        "rules": [
            {
                "featureId": "f2",
                "chainId": next(
                    chain.id for chain in _materials().pages[0].chains
                    if chain.element.node_id.endswith("jsx:select")
                ),
                "nodeId": "src/pages/Transfer.tsx#handleAcctChange",
                "category": "linkage",
                "elementId": "src/pages/Transfer.tsx#Transfer/jsx:select",
                "target": "付款账户",
                "condition": "账户变更时",
                "result": "查询对应账户余额",
                "quote": "function handleAcctChange(value) { queryBalance(value); }",
                "confidence": 0.8,
            }
        ]
    }
    outcome = await infer_sections(
        requirement_map.requirement,
        requirement_map,
        collect_materials(requirement_map, graph_),
        _llm_with(rules=rules),
        max_calls=1,
    )

    assert len(outcome.rules) == 1
    assert outcome.rules[0].evidence[0].quote == (
        "function handleAcctChange(value) { queryBalance(value); }"
    )
    assert outcome.dropped == []


async def test_invalid_quote_goes_to_dropped() -> None:
    """引文不在语料里：进 dropped（quote_not_found），不产出条目。"""
    bad = {"steps": [{**_VALID_STEPS["steps"][0], "quote": "语料里没有这句话"}]}
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement,
        requirement_map,
        _materials(),
        _llm_with(steps=bad),
        max_calls=1,
    )
    assert outcome.steps == []
    assert [item.reason for item in outcome.dropped] == [Reason.quote_not_found]
    assert outcome.dropped[0].featureId == "f1"


async def test_invalid_category_and_unknown_feature_are_rejected() -> None:
    """category 越界 → invalid_shape；featureId 编造 → unknown_feature。"""
    bad = {
        "rules": [
            {"featureId": "f1", "category": "magic", "condition": "x", "result": "y"},
            {
                "featureId": "f99",
                "category": "display",
                "condition": "x",
                "result": "y",
                "quote": "转账",
            },
        ]
    }
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement,
        requirement_map,
        _materials(),
        _llm_with(rules=bad),
        max_calls=1,
    )
    assert outcome.rules == []
    assert [item.reason for item in outcome.dropped] == [
        Reason.invalid_shape,
        Reason.unknown_feature,
    ]


async def test_unresolvable_model_keeps_sections_empty_with_warning() -> None:
    """模型持续输出非法 JSON：章节留空 + llm_unavailable 告警，不抛错。"""
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement,
        requirement_map,
        _materials(),
        FakeLLMClient(["not json", "still not json", "nope", "…", "…", "…"]),
        max_calls=2,
    )
    assert outcome.steps == [] and outcome.rules == [] and outcome.field_mappings == []
    assert [warning.code for warning in outcome.warnings] == [
        "llm_unavailable",
        "llm_unavailable",
        "llm_unavailable",
    ]


async def test_non_retryable_error_short_circuits() -> None:
    """不可重试错误：不再烧预算，直接留空 + 告警。"""
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement,
        requirement_map,
        _materials(),
        FakeLLMClient(error=LLMCallError("invalid api key", retryable=False)),
        max_calls=3,
    )
    assert outcome.steps == []
    assert outcome.warnings[0].code == "llm_unavailable"


async def test_partial_failure_keeps_good_sections() -> None:
    """步骤章失败、规则章成功：成功章节照常产出——章节互相独立。"""
    requirement_map = spec_requirement_map(spec_graph())
    llm = FakeLLMClient(
        [
            "bad",
            "bad",  # 步骤章两次调用都失败
            json.dumps(_VALID_RULES),
            json.dumps(_VALID_MAPPINGS),
        ]
    )
    outcome = await infer_sections(
        requirement_map.requirement,
        requirement_map,
        _materials(),
        llm,
        max_calls=2,
    )
    assert outcome.steps == []
    assert len(outcome.rules) == 1
    assert len(outcome.field_mappings) == 1
