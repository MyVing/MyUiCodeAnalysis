"""模型接入层测试：请求体形状与错误分类。

不碰真实网络——httpx.AsyncClient 被替换为带 MockTransport 的实例，
断言的是「发出去什么」与「失败怎么归类」，不依赖任何外部端点。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.core.config import Settings
from app.requirement.llm import LLMCallError, OpenAICompatibleLLM, build_llm_client

Handler = Callable[[httpx.Request], httpx.Response]


def _settings(**overrides: object) -> Settings:
    """构造一个 definitely-ready 的配置；环境变量与 .env 都不参与。"""
    base: dict[str, object] = {
        "llm_enabled": True,
        "llm_api_key": "sk-test",
        "llm_base_url": "https://example.test/v1",
        "llm_model": "test-model",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _ok_response(content: str = '{"matches": [], "unmatched": []}') -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.fixture
def use_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Handler], list[httpx.Request]]:
    """替换 httpx.AsyncClient：传入 handler，返回捕获到的请求列表。"""
    captured: list[httpx.Request] = []
    original = httpx.AsyncClient

    def make(handler: Handler) -> list[httpx.Request]:
        def recording(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return handler(request)

        def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(recording)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        captured.clear()
        return captured

    return make


async def test_payload_carries_json_response_format_and_max_tokens(
    use_transport: Callable[[Handler], list[httpx.Request]],
) -> None:
    captured = use_transport(lambda request: _ok_response())
    llm = OpenAICompatibleLLM(_settings())

    await llm.chat("system", "user")

    body = json.loads(captured[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 4096
    assert body["temperature"] == 0
    assert body["model"] == "test-model"
    assert body["messages"][0] == {"role": "system", "content": "system"}
    assert body["messages"][1] == {"role": "user", "content": "user"}
    assert captured[0].headers["authorization"] == "Bearer sk-test"


async def test_response_format_can_be_disabled_for_incompatible_endpoints(
    use_transport: Callable[[Handler], list[httpx.Request]],
) -> None:
    captured = use_transport(lambda request: _ok_response())
    llm = OpenAICompatibleLLM(_settings(llm_response_format=False))

    await llm.chat("system", "user")

    body = json.loads(captured[0].content)
    assert "response_format" not in body


async def test_http_401_is_not_retryable(
    use_transport: Callable[[Handler], list[httpx.Request]],
) -> None:
    use_transport(lambda request: httpx.Response(401, text="invalid api key"))
    llm = OpenAICompatibleLLM(_settings())

    with pytest.raises(LLMCallError) as excinfo:
        await llm.chat("system", "user")

    assert excinfo.value.retryable is False
    assert "401" in excinfo.value.message


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_transient_status_codes_are_retryable(
    use_transport: Callable[[Handler], list[httpx.Request]], status: int
) -> None:
    use_transport(lambda request: httpx.Response(status, text="busy"))
    llm = OpenAICompatibleLLM(_settings())

    with pytest.raises(LLMCallError) as excinfo:
        await llm.chat("system", "user")

    assert excinfo.value.retryable is True


async def test_timeout_is_retryable(
    use_transport: Callable[[Handler], list[httpx.Request]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    use_transport(handler)
    llm = OpenAICompatibleLLM(_settings())

    with pytest.raises(LLMCallError) as excinfo:
        await llm.chat("system", "user")

    assert excinfo.value.retryable is True
    assert "超时" in excinfo.value.message


async def test_malformed_body_is_retryable(
    use_transport: Callable[[Handler], list[httpx.Request]],
) -> None:
    use_transport(lambda request: httpx.Response(200, json={"choices": []}))
    llm = OpenAICompatibleLLM(_settings())

    with pytest.raises(LLMCallError) as excinfo:
        await llm.chat("system", "user")

    assert excinfo.value.retryable is True


def test_build_llm_client_returns_none_when_not_ready() -> None:
    assert build_llm_client(_settings(llm_enabled=False)) is None
    assert build_llm_client(_settings(llm_api_key=None)) is None


def test_build_llm_client_returns_client_when_ready() -> None:
    assert isinstance(build_llm_client(_settings()), OpenAICompatibleLLM)
