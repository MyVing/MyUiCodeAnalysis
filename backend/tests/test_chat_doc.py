"""对话式规格文档测试：意图分流 → 锚点定位 → specdoc 装配 → markdown 回传。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_analysis_repository, get_llm_client
from app.contracts.requirement import Candidate, Signal, SignalKind
from app.core.config import Settings
from app.requirement.chat import RequirementChatService
from app.requirement.chat_doc import _synthetic_requirement_map, locate_route_feature
from app.requirement.recall import CandidateDetail
from app.schemas import Project
from tests.conftest import settings_with
from tests.fakes import FakeLLMClient
from tests.specdoc_fixture import spec_graph

_PROJECT = Project(
    id="p_chat",
    name="转账演示",
    root_path="D:/work/demo",
    framework="react",
    created_at="2026-01-01T00:00:00Z",  # type: ignore[arg-type]
)

_VALID_STEPS = {
    "steps": [
        {
            "featureId": "f1",
            "precondition": ["已登录"],
            "actions": ["选择付款账户", "点击转账按钮提交转账"],
            "quote": "请选择付款账户",
            "confidence": 0.9,
        }
    ]
}
_VALID_RULES = {
    "rules": [
        {
            "featureId": "f1",
            "category": "validation",
            "target": "付款账户",
            "condition": "未选择付款账户",
            "result": "无法提交转账",
            "quote": "请选择付款账户",
            "confidence": 0.8,
        }
    ]
}
_VALID_MAPPINGS = {
    "fieldMappings": [
        {
            "frontField": "payeeAccount",
            "backendParam": "payee_account",
            "api": "POST /api/transfer",
            "quote": "POST /api/transfer",
            "confidence": 0.7,
        }
    ]
}

_DOC_INTENT = json.dumps({"intent": "doc_query", "featureText": "转账"}, ensure_ascii=False)
_NO_MATCH_INTENT = json.dumps(
    {"intent": "doc_query", "featureText": "导出报表"}, ensure_ascii=False
)
_CLARIFY_INTENT = json.dumps({"intent": "clarify", "featureText": None})


def _service(llm: FakeLLMClient, settings: Settings | None = None) -> RequirementChatService:
    return RequirementChatService(llm, settings or settings_with())


async def test_doc_query_locates_page_and_returns_markdown() -> None:
    """问「转账的前端交互视图」：意图判定 → 定位 Transfer 页 → 文档含两章节。"""
    llm = FakeLLMClient(
        [
            _DOC_INTENT,
            json.dumps(_VALID_STEPS),
            json.dumps(_VALID_RULES),
            json.dumps(_VALID_MAPPINGS),
        ]
    )
    reply, draft, doc_markdown, doc_page = await _service(llm).chat(
        [{"role": "user", "content": "帮我看看转账的前端交互视图和逻辑"}],
        project=_PROJECT,
        graph=spec_graph(),
    )

    assert draft is None
    assert "已定位到页面" in reply
    assert doc_page == "Transfer"
    assert doc_markdown is not None
    assert "业务操作步骤" in doc_markdown
    assert "前端交互视图" in doc_markdown
    # 意图判定走 chat_messages；三个章节推断走 chat
    assert len(llm.message_calls) == 1
    assert len(llm.calls) == 3


async def test_route_path_locates_page_without_intent_rewrite() -> None:
    """显式路由不依赖模型 featureText 或词表即可定位页面。"""
    llm = FakeLLMClient([
        json.dumps(_VALID_STEPS),
        json.dumps(_VALID_RULES),
        json.dumps(_VALID_MAPPINGS),
    ])
    best = locate_route_feature(spec_graph(), "/transfer/")
    assert best is not None
    assert best.candidate.name == "Transfer"
    reply, draft, doc_markdown, doc_page = await _service(llm).chat(
        [{"role": "user", "content": "请查看这个页面"}],
        project=_PROJECT,
        graph=spec_graph(),
        route_path=" /transfer/ ",
    )
    assert draft is None
    assert doc_page == "Transfer"
    assert doc_markdown is not None
    assert "已定位到页面" in reply
    assert len(llm.message_calls) == 0
    assert len(llm.calls) == 3


async def test_route_path_not_found_has_route_guidance() -> None:
    llm = FakeLLMClient([])
    reply, draft, doc_markdown, doc_page = await _service(llm).chat(
        [{"role": "user", "content": "请查看这个页面"}],
        project=_PROJECT,
        graph=spec_graph(),
        route_path="/missing",
    )
    assert "没有找到路由" in reply
    assert draft is None and doc_markdown is None and doc_page is None
    assert len(llm.message_calls) == 0


async def test_raw_query_fallback_preserves_explicit_route_anchor() -> None:
    """模型把路由改写成中文时，原始用户消息仍可作为召回兜底。"""
    llm = FakeLLMClient(
        [
            json.dumps({"intent": "doc_query", "featureText": "转账页面"}),
            json.dumps(_VALID_STEPS),
            json.dumps(_VALID_RULES),
            json.dumps(_VALID_MAPPINGS),
        ]
    )
    reply, _draft, markdown, page = await _service(llm).chat(
        [{"role": "user", "content": "/transfer 的前端交互视图"}],
        project=_PROJECT,
        graph=spec_graph(),
    )
    assert page == "Transfer"
    assert markdown is not None
    assert "已定位到页面" in reply


    """澄清类消息不走文档生成：单次模型调用，草稿照常返回。"""
    llm = FakeLLMClient(
        [
            _CLARIFY_INTENT,
            json.dumps({"reply": "筛选什么字段？", "draftRequirement": None}),
        ]
    )
    reply, draft, doc_markdown, doc_page = await _service(llm).chat(
        [{"role": "user", "content": "我想加个筛选"}],
        project=_PROJECT,
        graph=spec_graph(),
    )

    assert reply == "筛选什么字段？"
    assert draft is None
    assert doc_markdown is None and doc_page is None
    assert len(llm.message_calls) == 2


async def test_no_graph_skips_intent_and_clarifies() -> None:
    """项目无解析产物：不判意图，直接澄清（与旧行为一致）。"""
    llm = FakeLLMClient([json.dumps({"reply": "先做代码分析", "draftRequirement": None})])
    reply, draft, doc_markdown, doc_page = await _service(llm).chat(
        [{"role": "user", "content": "转账的交互视图"}]
    )

    assert reply == "先做代码分析"
    assert doc_markdown is None and doc_page is None
    assert len(llm.message_calls) == 1


async def test_no_match_replies_with_guidance() -> None:
    """意图是查文档但图里没命中：给出换说法的引导，不进装配。"""
    llm = FakeLLMClient([_NO_MATCH_INTENT])
    reply, draft, doc_markdown, doc_page = await _service(llm).chat(
        [{"role": "user", "content": "导出报表的前端交互视图"}],
        project=_PROJECT,
        graph=spec_graph(),
    )

    assert "没有定位到" in reply
    assert doc_markdown is None and doc_page is None
    assert len(llm.message_calls) == 1


def _seed_graph(client: TestClient, project_id: str) -> None:
    async def _run() -> None:
        analyses = client.app.dependency_overrides[get_analysis_repository]()
        await analyses.save_graph(project_id, spec_graph())

    client.portal.call(_run)


@pytest.fixture
def fake_llm(client: TestClient) -> FakeLLMClient:
    fake = FakeLLMClient()
    client.app.dependency_overrides[get_llm_client] = lambda: fake
    return fake


def test_chat_endpoint_returns_doc_markdown(
    client: TestClient, project: dict[str, object], fake_llm: FakeLLMClient
) -> None:
    """端到端：项目已有解析产物时，查文档类消息直接带回规格文档。"""
    _seed_graph(client, str(project["id"]))
    fake_llm.responses = [
        _DOC_INTENT,
        json.dumps(_VALID_STEPS),
        json.dumps(_VALID_RULES),
        json.dumps(_VALID_MAPPINGS),
    ]
    response = client.post(
        f"/api/v1/projects/{project['id']}/requirement-chat",
        json={"messages": [{"role": "user", "content": "转账的前端交互视图"}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["docPage"] == "Transfer"
    assert "业务操作步骤" in body["docMarkdown"]


def test_chat_endpoint_still_clarifies_without_graph(
    client: TestClient, project: dict[str, object], fake_llm: FakeLLMClient
) -> None:
    fake_llm.responses = [json.dumps({"reply": "在哪个页面？", "draftRequirement": None})]
    response = client.post(
        f"/api/v1/projects/{project['id']}/requirement-chat",
        json={"messages": [{"role": "user", "content": "加个筛选"}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == "在哪个页面？"
    assert body["docMarkdown"] is None and body["docPage"] is None


def test_synthetic_match_caps_confidence_within_contract() -> None:
    """候选得分是多信号之和（可能 >1）：合成 Match 时必须归一到契约的 [0,1]。"""
    best = CandidateDetail(
        candidate=Candidate(
            nodeId="src/pages/Transfer.tsx#Transfer",
            routeId="route:/transfer",
            name="Transfer",
            score=1.323,
            signals=[
                Signal(kind=SignalKind.uiText, value="请选择付款账户", score=0.7),
                Signal(kind=SignalKind.apiUrl, value="POST /api/transfer", score=0.623),
            ],
        )
    )
    match = _synthetic_requirement_map(_PROJECT, best, "转账", spec_graph()).matches[0]
    assert match.modelConfidence == 1.0
    assert match.confidence == 0.6
