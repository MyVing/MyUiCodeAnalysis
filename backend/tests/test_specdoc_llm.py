from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.specdoc.llm import OpenAICompatibleToolLLM, SpecDocToolError, build_specdoc_llm_client


async def test_tool_loop_returns_final_text_and_round_trips_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict] = []
    responses = [
        {"tool_calls": [{"id": "call-1", "type": "function", "function": {
            "name": "search_evidence", "arguments": '{"category":"chain"}'
        }}]},
        {"content": '{"steps": []}'},
    ]
    client = OpenAICompatibleToolLLM(Settings(
        _env_file=None, llm_enabled=True, llm_api_key="test", llm_response_format=True
    ))

    async def request(payload):
        requests.append(payload)
        return responses.pop(0)

    monkeypatch.setattr(client, "_request", request)

    async def execute(name: str, arguments: dict) -> dict:
        assert name == "search_evidence"
        return {"status": "ok", "facts": [{"text": "点击转账"}]}

    result = await client.chat_with_tools(
        "system", "user", tools=[{"type": "function", "function": {"name": "search_evidence"}}],
        executor=execute,
    )

    assert result == '{"steps": []}'
    assert "response_format" not in requests[0]
    assert requests[0]["messages"][-1] == {"role": "user", "content": "user"}
    assert requests[1]["messages"][-2]["tool_calls"][0]["id"] == "call-1"
    assert json.loads(requests[1]["messages"][-1]["content"]) == {
        "status": "ok", "facts": [{"text": "点击转账"}]
    }


async def test_tool_loop_rejects_unknown_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpenAICompatibleToolLLM(Settings(_env_file=None, llm_enabled=True, llm_api_key="test"))

    async def request(payload):
        return {"tool_calls": [{"id": "call-1", "type": "function", "function": {
            "name": "unknown", "arguments": "{}"
        }}]}

    monkeypatch.setattr(client, "_request", request)

    async def execute(name: str, arguments: dict) -> dict:
        raise AssertionError("unknown tool must not be executed")

    with pytest.raises(SpecDocToolError) as error:
        await client.chat_with_tools("system", "user", tools=[], executor=execute)
    assert error.value.code == "unknown_tool"


async def test_tool_loop_enforces_call_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpenAICompatibleToolLLM(Settings(_env_file=None, llm_enabled=True, llm_api_key="test"))

    async def request(payload):
        return {"tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            {"id": "call-2", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]}

    monkeypatch.setattr(client, "_request", request)

    async def execute(name: str, arguments: dict) -> dict:
        return {}

    with pytest.raises(SpecDocToolError) as error:
        await client.chat_with_tools("system", "user", tools=[], executor=execute, max_tool_calls=1)
    assert error.value.code == "tool_budget_exhausted"


async def test_tool_loop_surfaces_endpoint_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpenAICompatibleToolLLM(
        Settings(_env_file=None, llm_enabled=True, llm_api_key="test")
    )

    async def post(*args, **kwargs):
        return httpx.Response(400, json={"error": {"message": "tools unsupported"}})

    class HTTP:
        async def post(self, *args, **kwargs):
            return await post(*args, **kwargs)

    monkeypatch.setattr(client, "_http", lambda: HTTP())
    with pytest.raises(SpecDocToolError) as error:
        await client.chat_with_tools(
            "system", "user", tools=[], executor=lambda *_: None  # type: ignore[arg-type]
        )
    assert error.value.retryable is False


def test_specdoc_factory_is_independent_and_honors_readiness() -> None:
    settings = Settings(_env_file=None, llm_enabled=True, llm_api_key="test")
    assert isinstance(build_specdoc_llm_client(settings), OpenAICompatibleToolLLM)
    assert build_specdoc_llm_client(Settings(_env_file=None, llm_enabled=False)) is None


async def test_tool_loop_retries_retryable_request_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时 / 429 / 5xx 属于可重试错误：重发同一 payload，不消耗对话进度。"""
    client = OpenAICompatibleToolLLM(
        Settings(_env_file=None, llm_enabled=True, llm_api_key="test")
    )
    client._tool_retry_backoff = 0
    attempts = 0

    async def request(payload):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SpecDocToolError("specdoc 模型返回 HTTP 429", retryable=True)
        return {"content": '{"steps": []}'}

    monkeypatch.setattr(client, "_request", request)

    result = await client.chat_with_tools(
        "system", "user", tools=[], executor=lambda *_: None  # type: ignore[arg-type]
    )

    assert result == '{"steps": []}'
    assert attempts == 3


async def test_tool_loop_does_not_retry_non_retryable_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpenAICompatibleToolLLM(
        Settings(_env_file=None, llm_enabled=True, llm_api_key="test")
    )
    client._tool_retry_backoff = 0
    attempts = 0

    async def request(payload):
        nonlocal attempts
        attempts += 1
        raise SpecDocToolError("specdoc 模型返回 HTTP 401", retryable=False)

    monkeypatch.setattr(client, "_request", request)

    with pytest.raises(SpecDocToolError):
        await client.chat_with_tools(
            "system", "user", tools=[], executor=lambda *_: None  # type: ignore[arg-type]
        )
    assert attempts == 1


async def test_tool_loop_gives_up_after_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpenAICompatibleToolLLM(
        Settings(_env_file=None, llm_enabled=True, llm_api_key="test")
    )
    client._tool_retry_backoff = 0
    client._tool_retry_attempts = 2
    attempts = 0

    async def request(payload):
        nonlocal attempts
        attempts += 1
        raise SpecDocToolError("specdoc 模型请求超时", retryable=True)

    monkeypatch.setattr(client, "_request", request)

    with pytest.raises(SpecDocToolError):
        await client.chat_with_tools(
            "system", "user", tools=[], executor=lambda *_: None  # type: ignore[arg-type]
        )
    assert attempts == 2
