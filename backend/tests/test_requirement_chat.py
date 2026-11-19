"""需求对话 API 测试：多轮历史透传、JSON 兜底、LLM 未配置/调用失败映射。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_llm_client
from app.requirement.chat import CHAT_SYSTEM_PROMPT
from app.requirement.llm import LLMCallError
from tests.fakes import FakeLLMClient


@pytest.fixture
def fake_llm(client: TestClient) -> FakeLLMClient:
    fake = FakeLLMClient()
    client.app.dependency_overrides[get_llm_client] = lambda: fake
    return fake


def _post(client: TestClient, project_id: str, messages: list[dict[str, str]]) -> object:
    response = client.post(
        f"/api/v1/projects/{project_id}/requirement-chat",
        json={"messages": messages},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_chat_returns_reply_and_draft(
    client: TestClient, project: dict[str, object], fake_llm: FakeLLMClient
) -> None:
    fake_llm.responses = [
        json.dumps(
            {
                "reply": "信息够了，草稿已生成",
                "draftRequirement": "在用户列表页增加按状态筛选…",
            }
        )
    ]
    body = _post(
        client,
        str(project["id"]),
        [
            {"role": "user", "content": "我想加个筛选"},
            {"role": "assistant", "content": "筛选什么字段？在哪一页？"},
            {"role": "user", "content": "用户列表页按状态筛"},
        ],
    )
    assert body["reply"] == "信息够了，草稿已生成"
    assert body["draftRequirement"] == "在用户列表页增加按状态筛选…"
    assert body["docMarkdown"] is None and body["docPage"] is None

    # 全量历史透传给模型；对话走 chat_messages（温度 0.7、系统提示词就位）
    assert len(fake_llm.message_calls) == 1
    system, messages, temperature = fake_llm.message_calls[0]
    assert system == CHAT_SYSTEM_PROMPT
    assert [item["role"] for item in messages] == ["user", "assistant", "user"]
    assert messages[-1]["content"] == "用户列表页按状态筛"
    assert temperature == pytest.approx(0.7)


def test_non_json_output_falls_back_to_plain_reply(
    client: TestClient, project: dict[str, object], fake_llm: FakeLLMClient
) -> None:
    """兼容端点忽略 json_object 约束时：整段当 reply，不给草稿也不报错。"""
    fake_llm.responses = ["请告诉我你想在哪个页面加什么功能？"]
    body = _post(client, str(project["id"]), [{"role": "user", "content": "加个导出"}])
    assert body["reply"] == "请告诉我你想在哪个页面加什么功能？"
    assert body["draftRequirement"] is None
    assert body["docMarkdown"] is None and body["docPage"] is None


def test_chat_is_unavailable_without_llm(
    client: TestClient, project: dict[str, object]
) -> None:
    # conftest 默认把 get_llm_client 覆盖为 None
    response = client.post(
        f"/api/v1/projects/{project['id']}/requirement-chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503
    assert "llm" in response.json()["detail"].lower() or "模型" in response.json()["detail"]


def test_llm_error_maps_to_502(
    client: TestClient, project: dict[str, object], fake_llm: FakeLLMClient
) -> None:
    fake_llm.error = LLMCallError("模型返回 HTTP 500")
    response = client.post(
        f"/api/v1/projects/{project['id']}/requirement-chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "模型返回 HTTP 500"


def test_chat_rejects_unknown_project(client: TestClient, fake_llm: FakeLLMClient) -> None:
    response = client.post(
        "/api/v1/projects/p_missing/requirement-chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 404


def test_chat_rejects_empty_messages(client: TestClient, project: dict[str, object]) -> None:
    response = client.post(
        f"/api/v1/projects/{project['id']}/requirement-chat",
        json={"messages": []},
    )
    assert response.status_code == 422
