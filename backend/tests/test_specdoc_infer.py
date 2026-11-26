"""推断章节测试：mock LLM——引文逐字校验、0.6 封顶、dry-run 留空。"""

from __future__ import annotations

import json

from app.contracts import EdgeKind, NodeKind
from app.contracts.requirement import Match, Reason
from app.requirement.chains import derive_chains
from app.requirement.llm import LLMCallError
from app.specdoc.collect import collect_materials
from app.specdoc.infer import (
    _evidence_from_step,
    _locate_mapping_quote,
    _page_contexts,
    infer_sections,
)
from tests.fakes import FakeLLMClient
from tests.graph_fixture import edge, node, page
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


async def test_tool_steps_only_validate_quotes_returned_by_retrieval() -> None:
    graph_ = spec_graph()
    requirement_map = spec_requirement_map(graph_)
    materials = collect_materials(requirement_map, graph_)
    chain_id = next(
        chain.id for chain in materials.pages[0].chains
        if chain.element.node_id.endswith("jsx:button")
    )

    class ToolFake:
        async def chat_with_tools(self, system, user, *, tools, executor, max_tool_calls):
            result = await executor("search_evidence", {
                "pageId": PAGE_ID, "chainId": chain_id, "category": "chain"
            })
            quote = result["facts"][0]["text"]
            return json.dumps({"steps": [{
                "featureId": "f1", "chainId": chain_id,
                "nodeId": result["facts"][0]["nodeId"],
                "actions": ["点击按钮"], "quote": quote, "confidence": 0.8,
            }]}, ensure_ascii=False)

        async def chat(self, system, user):
            if "规则" in system:
                return json.dumps({"rules": []})
            return json.dumps({"fieldMappings": []})

    outcome = await infer_sections(
        "转账", requirement_map, materials, ToolFake(), max_calls=2  # type: ignore[arg-type]
    )

    assert len(outcome.steps) == 1
    assert outcome.steps[0].quote in outcome.steps[0].evidence[0].quote


def _multi_page_inputs():
    graph_ = spec_graph()
    second_id = "src/pages/TransferReview.tsx#TransferReview"
    second_page = page(second_id, "TransferReview", texts=["复核页面专属文案"])
    second_element = node(
        f"{second_id}/jsx:button",
        NodeKind.element,
        "<button> 复核转账",
        file="src/pages/TransferReview.tsx",
        attrs={"tag": "button", "viewRole": "control", "viewText": "复核转账"},
    )
    second_handler = node(
        "src/pages/TransferReview.tsx#submitReview",
        NodeKind.fn,
        "submitReview",
        file="src/pages/TransferReview.tsx",
    )
    second_api = node(
        "api:POST:/api/transfer/review",
        NodeKind.api,
        "POST /api/transfer/review",
        attrs={"method": "POST", "url": "/api/transfer/review"},
    )
    second_route = node(
        "src/router.tsx#/transfer-review",
        NodeKind.route,
        "/transfer-review",
        file="src/router.tsx",
        attrs={"path": "/transfer-review"},
    )
    graph_.nodes.extend([second_page, second_element, second_handler, second_api, second_route])
    graph_.edges.extend([
        edge(second_route.id, second_page.id, EdgeKind.renders),
        edge(second_page.id, second_element.id, EdgeKind.contains),
        edge(second_element.id, second_handler.id, EdgeKind.calls),
        edge(second_handler.id, second_api.id, EdgeKind.requests),
    ])
    requirement_map = spec_requirement_map(graph_)
    requirement_map.matches.append(
        Match(
            featureId="f1",
            pageNodeId=second_id,
            chainId=next(
                chain.id for chain in derive_chains(graph_).for_page(second_id)
                if chain.element.node_id == second_element.id
            ),
            routeId=second_route.id,
            confidence=0.8,
            quote="复核转账",
            subgraph=requirement_map.matches[0].subgraph,
            source=requirement_map.matches[0].source,
        )
    )
    materials = collect_materials(requirement_map, graph_)
    return (
        requirement_map, materials, second_id,
        requirement_map.matches[-1].chainId, second_element.id,
    )


async def test_tool_prompt_contains_only_requirement_matched_chains() -> None:
    requirement_map, materials, _, _, _ = _multi_page_inputs()
    # 模拟需求产物里残留旧图链路：骨架应只认 collect 校验过的每页命中集合
    requirement_map.matches.append(
        requirement_map.matches[-1].model_copy(update={"chainId": "stale::chain"})
    )
    captured_users: list[str] = []

    class ToolFake:
        async def chat_with_tools(self, system, user, *, tools, executor, max_tool_calls):
            captured_users.append(user)
            if "步骤" in system:
                return json.dumps({"steps": []})
            if "规则" in system:
                return json.dumps({"rules": []})
            return json.dumps({"fieldMappings": []})

    await infer_sections(
        requirement_map.requirement,
        requirement_map,
        materials,
        ToolFake(),  # type: ignore[arg-type]
        max_calls=1,
    )

    skeletons = [
        json.loads(user.split("页面与链路骨架：\n", 1)[1].split("\n请", 1)[0])
        for user in captured_users[:2]
    ]
    expected_by_page = {
        material.page.pageId: set(material.matched_chain_ids)
        for material in materials.pages
    }
    for skeleton in skeletons:
        actual_by_page = {
            page_value["pageId"]: {
                chain["chainId"] for chain in page_value["chains"]
            }
            for page_value in skeleton["pages"]
        }
        assert actual_by_page == expected_by_page
    assert all(
        "texts" not in step
        for skeleton in skeletons
        for page_value in skeleton["pages"]
        for chain in page_value["chains"]
        for step in chain["steps"]
    )


async def test_steps_use_chain_page_for_multi_page_feature() -> None:
    requirement_map, materials, second_id, chain_id, element_id = _multi_page_inputs()
    steps = {"steps": [{
        "featureId": "f1", "chainId": chain_id, "nodeId": element_id,
        "actions": ["点击复核"], "quote": "复核转账",
    }]}
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, materials,
        _llm_with(steps=steps), max_calls=1,
    )
    assert len(outcome.steps) == 1
    assert outcome.steps[0].pageId == second_id
    assert outcome.steps[0].evidence[0].nodeId == element_id


async def test_step_rejects_page_not_matched_by_feature() -> None:
    requirement_map, materials, _, _, _ = _multi_page_inputs()
    steps = {"steps": [{
        "featureId": "f1", "pageId": "not-a-page", "chainId": "fake-chain",
        "nodeId": "fake-node", "actions": ["执行"], "quote": "转账",
    }]}
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, materials,
        _llm_with(steps=steps), max_calls=1,
    )
    assert outcome.steps == []
    assert outcome.dropped[0].reason == Reason.node_not_in_candidates


async def test_rule_page_id_selects_display_ui_text_and_rejects_element_text() -> None:
    requirement_map, materials, second_id, _, _ = _multi_page_inputs()
    rules = {"rules": [{
        "featureId": "f1", "pageId": second_id, "category": "display",
        "condition": "复核页面打开时", "result": "显示复核提示",
        "quote": "复核页面专属文案",
    }]}
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, materials,
        _llm_with(rules=rules), max_calls=1,
    )
    assert len(outcome.rules) == 1
    assert outcome.rules[0].pageId == second_id
    assert outcome.rules[0].evidence[0].nodeId == second_id

    rules["rules"][0]["quote"] = "复核转账"
    rejected = await infer_sections(
        requirement_map.requirement, requirement_map, materials,
        _llm_with(rules=rules), max_calls=1,
    )
    assert rejected.rules == []
    assert rejected.dropped[0].reason == Reason.quote_not_found


async def test_rule_rejects_chain_page_conflict() -> None:
    requirement_map, materials, second_id, _, _ = _multi_page_inputs()
    first_chain = next(
        chain.id for chain in materials.pages[0].chains
        if chain.element.node_id.endswith("jsx:button")
    )
    rules = {"rules": [{
        "featureId": "f1", "pageId": second_id, "chainId": first_chain,
        "elementId": "src/pages/Transfer.tsx#Transfer/jsx:button",
        "category": "validation", "condition": "x", "result": "y", "quote": "转账",
    }]}
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, materials,
        _llm_with(rules=rules), max_calls=1,
    )
    assert outcome.rules == []
    assert outcome.dropped[0].reason == Reason.chain_not_in_candidates


async def test_non_display_rule_does_not_accept_page_ui_text_without_chain() -> None:
    requirement_map, materials, second_id, _, _ = _multi_page_inputs()
    rules = {"rules": [{
        "featureId": "f1", "pageId": second_id, "category": "validation",
        "condition": "页面打开时", "result": "显示信息",
        "quote": "复核页面专属文案但不可被非display类型引用",
    }]}
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, materials,
        _llm_with(rules=rules), max_calls=1,
    )
    assert outcome.rules == []
    assert outcome.dropped[0].reason == Reason.quote_not_found


async def test_display_rule_rejects_explicit_element_anchor_without_chain() -> None:
    requirement_map, materials, second_id, _, element_id = _multi_page_inputs()
    rules = {"rules": [{
        "featureId": "f1", "pageId": second_id, "elementId": element_id,
        "category": "display", "condition": "页面打开时", "result": "展示提示",
        "quote": "复核转账",
    }]}
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, materials,
        _llm_with(rules=rules), max_calls=1,
    )
    assert outcome.rules == []
    assert outcome.dropped[0].reason == Reason.node_not_in_candidates


    steps = {
        "steps": [
            {
                "featureId": "f1",
                "chainId": "fake-chain",
                "nodeId": "src/pages/Transfer.tsx#Transfer/jsx:button",
                "actions": ["点击转账"],
                "quote": "转账",
            }
        ]
    }
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, _materials(),
        _llm_with(steps=steps), max_calls=1,
    )

    assert outcome.steps == []
    dropped = next(item for item in outcome.dropped if item.featureId == "f1")
    assert dropped.reason == Reason.chain_not_in_candidates
    assert dropped.chainId == "fake-chain"
    assert dropped.nodeId == "src/pages/Transfer.tsx#Transfer/jsx:button"


async def test_rules_reject_unknown_chain_without_page_level_fallback() -> None:
    rules = {
        "rules": [
            {
                "featureId": "f1",
                "chainId": "fake-chain",
                "elementId": "src/pages/Transfer.tsx#Transfer/jsx:button",
                "category": "validation",
                "condition": "x",
                "result": "y",
                "quote": "转账",
            }
        ]
    }
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, _materials(),
        _llm_with(rules=rules), max_calls=1,
    )

    assert outcome.rules == []
    dropped = next(item for item in outcome.dropped if item.featureId == "f1")
    assert dropped.reason == Reason.chain_not_in_candidates
    assert dropped.chainId == "fake-chain"
    assert not any(w.code == "evidence_ref_missing" for w in outcome.warnings)


async def test_rule_node_outside_chain_is_rejected_as_invalid_anchor() -> None:
    rules = {
        "rules": [
            {
                "featureId": "f1",
                "chainId": next(
                    chain.id for chain in _materials().pages[0].chains
                    if chain.element.node_id.endswith("jsx:button")
                ),
                "nodeId": "not-in-chain",
                "category": "validation",
                "condition": "x",
                "result": "y",
                "quote": "转账",
            }
        ]
    }
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, _materials(),
        _llm_with(rules=rules), max_calls=1,
    )

    assert outcome.rules == []
    dropped = next(item for item in outcome.dropped if item.featureId == "f1")
    assert dropped.reason == Reason.node_not_in_candidates
    assert dropped.nodeId == "not-in-chain"


async def test_page_level_match_can_infer_steps_with_page_evidence() -> None:
    graph_ = spec_graph()
    requirement_map = spec_requirement_map(graph_)
    requirement_map.matches[0] = requirement_map.matches[0].model_copy(update={"chainId": None})
    steps = {"steps": [{
        "featureId": "f1", "pageId": PAGE_ID, "nodeId": PAGE_ID,
        "actions": ["页面展示转账信息"], "quote": "请选择付款账户",
    }]}
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, collect_materials(requirement_map, graph_),
        _llm_with(steps=steps), max_calls=1,
    )
    assert len(outcome.steps) == 1
    assert outcome.steps[0].evidence[0].nodeId == PAGE_ID
    assert any(w.code == "evidence_ref_missing" for w in outcome.warnings)


async def test_page_level_match_can_infer_steps_from_real_chain_without_chain_ref() -> None:
    graph_ = spec_graph()
    requirement_map = spec_requirement_map(graph_)
    requirement_map.matches[0] = requirement_map.matches[0].model_copy(update={"chainId": None})
    steps = {"steps": [{
        "featureId": "f1", "pageId": PAGE_ID,
        "nodeId": "src/pages/Transfer.tsx#Transfer/jsx:button",
        "actions": ["点击转账"], "quote": "转账",
    }]}
    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, collect_materials(requirement_map, graph_),
        _llm_with(steps=steps), max_calls=1,
    )
    assert len(outcome.steps) == 1
    assert outcome.steps[0].evidence[0].nodeId == "src/pages/Transfer.tsx#Transfer/jsx:button"


async def test_repeated_steps_receive_unique_stable_ids() -> None:
    steps = {"steps": [
        {"featureId": "f1", "chainId": _VALID_STEPS["steps"][0]["chainId"],
         "nodeId": "src/pages/Transfer.tsx#Transfer/jsx:button",
         "actions": ["点击转账"], "quote": "转账"},
        {"featureId": "f1", "chainId": _VALID_STEPS["steps"][0]["chainId"],
         "nodeId": "src/pages/Transfer.tsx#Transfer/jsx:button",
         "actions": ["再次点击"], "quote": "转账"},
    ]}
    requirement_map = spec_requirement_map(spec_graph())
    first = await infer_sections(requirement_map.requirement, requirement_map, _materials(),
                                 _llm_with(steps=steps), max_calls=1)
    second = await infer_sections(requirement_map.requirement, requirement_map, _materials(),
                                  _llm_with(steps=steps), max_calls=1)
    assert [step.id for step in first.steps] == ["step.f1.1", "step.f1.2"]
    assert [step.id for step in second.steps] == [step.id for step in first.steps]


async def test_valid_chain_display_rule_can_quote_page_ui_text() -> None:
    chain_id = _VALID_STEPS["steps"][0]["chainId"]
    rules = {"rules": [{
        "featureId": "f1", "chainId": chain_id, "category": "display",
        "condition": "页面加载时", "result": "显示付款账户提示",
        "quote": "请选择付款账户",
    }]}
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(requirement_map.requirement, requirement_map, _materials(),
                                   _llm_with(rules=rules), max_calls=1)
    assert len(outcome.rules) == 1
    assert outcome.rules[0].evidence[0].nodeId == PAGE_ID


async def test_non_display_rule_rejects_real_page_ui_text_with_chain() -> None:
    chain_id = _VALID_STEPS["steps"][0]["chainId"]
    rules = {"rules": [{
        "featureId": "f1", "chainId": chain_id, "category": "validation",
        "condition": "未选择账户", "result": "提示错误",
        "quote": "请选择付款账户",
    }]}
    requirement_map = spec_requirement_map(spec_graph())
    outcome = await infer_sections(requirement_map.requirement, requirement_map, _materials(),
                                   _llm_with(rules=rules), max_calls=1)
    assert outcome.rules == []
    assert outcome.dropped[0].reason == Reason.quote_not_found


async def test_mapping_form_quote_points_to_form_element() -> None:
    graph_ = spec_graph()
    select = next(node for node in graph_.nodes if node.id.endswith("jsx:select"))
    select.attrs["formField"] = {"name": "payeeAccount", "quote": '<select name="payeeAccount">'}
    requirement_map = spec_requirement_map(graph_)
    mapping = {"fieldMappings": [{
        "frontField": "payeeAccount", "backendParam": "payee_account",
        "elementId": select.id, "api": "POST /api/transfer",
        "quote": '<select name="payeeAccount">',
    }]}
    outcome = await infer_sections(requirement_map.requirement, requirement_map,
                                   collect_materials(requirement_map, graph_),
                                   _llm_with(mappings=mapping), max_calls=1)
    assert len(outcome.field_mappings) == 1
    assert outcome.field_mappings[0].evidence[0].nodeId == select.id


async def test_mapping_continues_to_later_page_with_same_api_url() -> None:
    requirement_map, materials, second_id, _, element_id = _multi_page_inputs()
    first_api = next(node for node in materials.pages[0].api_nodes.values()
                     if node.attrs.get("url") == "/api/transfer")
    first_api.attrs["requestBody"] = {"unrelated": "first page body"}
    second_api = next(node for node in materials.pages[1].api_nodes.values()
                      if node.attrs.get("url") == "/api/transfer/review")
    second_api.attrs["url"] = "/api/transfer"
    second_api.attrs["requestBody"] = {"payee_account": "second page body"}
    second_element_id = next(source.node.id for source in materials.pages[1].elements
                             if source.node.attrs.get("tag") == "button")
    contexts = _page_contexts(materials)
    contexts[1].api_urls = (*contexts[1].api_urls, ("POST", "/api/transfer"))
    contexts[1].texts_by_node[second_api.id] = ("payee_account: second page body",)
    contexts[1].evidence_by_text["payee_account: second page body"] = _evidence_from_step(
        second_api.id, "payee_account: second page body", None
    )
    evidence, page_id = _locate_mapping_quote(
        "POST /api/transfer", "payee_account: second page body", contexts,
        element_id=second_element_id,
    )
    assert evidence is not None
    assert page_id == second_id
    assert evidence.nodeId == second_api.id


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
    assert step.id == "step.f1.1"
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
        "evidence_full_prompt_fallback",
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
    assert outcome.warnings[0].code == "evidence_full_prompt_fallback"
    assert outcome.warnings[1].code == "llm_unavailable"


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
