"""编排器与服务层测试：dry-run 留空 + 告警；无模型时确定性章节照常产出。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.config import Settings
from app.schemas import Project, SpecDocTask
from app.specdoc.orchestrator import SCHEMA_VERSION, SpecDocOrchestrator
from app.specdoc.render import render_markdown
from tests.fakes import FakeLLMClient
from tests.specdoc_fixture import spec_graph, spec_requirement_map

VALID_RULES = {
    "rules": [
        {
            "featureId": "f1",
            "category": "validation",
            "target": "付款账户",
            "condition": "未选择付款账户",
            "result": "转账按钮置灰，不可提交",
            "quote": "请选择付款账户",
        },
        {
            "category": "display",
            "target": "可用余额",
            "condition": "余额不足",
            "result": "余额文本标红",
            "quote": "可用余额",
        },
    ]
}
VALID_MAPPINGS = {
    "fieldMappings": [
        {
            "frontField": "payeeAccount",
            "backendParam": "payee_account",
            "api": "POST /api/transfer",
            "quote": "POST /api/transfer",
        }
    ]
}

_PROJECT = Project(
    id="p_spec",
    name="订单平台",
    root_path="D:/work/orders",
    framework="next",
    created_at=datetime.now(UTC),
)


def _task(dry_run: bool = False) -> SpecDocTask:
    return SpecDocTask(
        id="stask_test000001",
        project_id="p_spec",
        requirement_id="req_spec",
        status="running",
        dry_run=dry_run,
        created_at=datetime.now(UTC),
    )


async def test_dry_run_keeps_deterministic_sections_only() -> None:
    """dry-run：要素/事件/接口照常，三个推断章节留空 + dry_run_skipped_llm_sections。"""
    graph_ = spec_graph()
    orchestrator = SpecDocOrchestrator(Settings(_env_file=None), llm=None)
    doc = await orchestrator.run(
        _PROJECT, _task(dry_run=True), spec_requirement_map(graph_), graph_
    )

    assert doc.dryRun is True
    assert doc.schemaVersion == SCHEMA_VERSION
    assert len(doc.elements) >= 3  # select / span / button
    assert len(doc.events) == 2
    assert len(doc.apiMappings) == 2
    assert doc.steps == [] and doc.rules == [] and doc.fieldMappings == []
    assert [warning.code for warning in doc.warnings] == ["dry_run_skipped_llm_sections"]


async def test_missing_model_yields_llm_unavailable() -> None:
    """非 dry-run 但模型不可用：确定性章节照常，推断章节留空 + llm_unavailable。"""
    graph_ = spec_graph()
    orchestrator = SpecDocOrchestrator(Settings(_env_file=None), llm=None)
    doc = await orchestrator.run(
        _PROJECT, _task(dry_run=False), spec_requirement_map(graph_), graph_
    )
    assert doc.elements and doc.apiMappings
    assert doc.steps == []
    assert [warning.code for warning in doc.warnings] == ["llm_unavailable"]


async def test_full_pipeline_numbers_rules_and_echoes_meta() -> None:
    """全链路：规则按 fr.{featureId}.{n} 编号；页面级规则归 fr.page.{n}。"""
    graph_ = spec_graph()
    llm = FakeLLMClient(
        [
            json.dumps({"steps": []}),
            json.dumps(VALID_RULES),
            json.dumps(VALID_MAPPINGS),
        ]
    )
    settings = Settings(_env_file=None, llm_max_calls_per_request=2)
    orchestrator = SpecDocOrchestrator(settings, llm=llm)
    doc = await orchestrator.run(
        _PROJECT, _task(dry_run=False), spec_requirement_map(graph_), graph_
    )

    assert doc.specDocId == "stask_test000001"
    assert doc.requirementId == "req_spec"
    assert doc.analysisId == "an_spec"
    assert doc.pages[0].pageNo == 1
    rule_ids = [rule.id for rule in doc.rules]
    assert rule_ids == ["fr.f1.1", "fr.page.1"]
    assert len(llm.calls) == 3  # 三个章节各一次调用
    # prompt 里没有页面画像——硬约束：语料只能是代码原文
    system, user = llm.calls[0]
    assert "功能点" in user and "页面语料" in user


async def test_api_mappings_render_under_their_page() -> None:
    """接口映射按触发要素的页面归组。

    mapping.elementId 是图上要素节点的原始 id，要素清单 id 是 ui.{pageNo}.{n}——
    直接查必然落空（接口映射会全部归到无名页，2.1.4.5 恒空）。经事件桥接后
    应落在 Transfer 页，动作名取要素可见文案。
    """
    graph_ = spec_graph()
    orchestrator = SpecDocOrchestrator(Settings(_env_file=None), llm=None)
    doc = await orchestrator.run(
        _PROJECT, _task(dry_run=True), spec_requirement_map(graph_), graph_
    )
    body = render_markdown(doc)

    assert "_本页面无后端接口调用。_" not in body
    assert "| 转账 | POST /api/transfer | **POST** | 0.95 |" in body
    assert "| 付款账户 | GET /api/account/queryBalance | **GET** | 0.95 |" in body


async def test_dropped_entries_surface_in_doc() -> None:
    """引文校验失败的模型输出进 dropped，任务仍成功——审计可见。"""
    graph_ = spec_graph()
    bad_rules = {
        "rules": [
            {
                "featureId": "f1",
                "category": "validation",
                "condition": "x",
                "result": "y",
                "quote": "幻想出来的依据",
            }
        ]
    }
    llm = FakeLLMClient(
        [
            json.dumps({"steps": []}),
            json.dumps(bad_rules),
            json.dumps(VALID_MAPPINGS),
        ]
    )
    settings = Settings(_env_file=None, llm_max_calls_per_request=1)
    orchestrator = SpecDocOrchestrator(settings, llm=llm)
    doc = await orchestrator.run(
        _PROJECT, _task(dry_run=False), spec_requirement_map(graph_), graph_
    )
    assert doc.rules == []
    assert len(doc.dropped) == 1
    assert doc.dropped[0].reason.value == "quote_not_found"
