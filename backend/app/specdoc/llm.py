"""Specdoc-only OpenAI-compatible LLM client with bounded tool execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.config import Settings
from app.requirement.llm import LLMCallError

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class SpecDocToolError(LLMCallError):
    """Tool protocol or execution failed for specdoc inference."""


class OpenAICompatibleToolLLM:
    """Standalone specdoc client; does not alter the shared requirement client."""

    def __init__(self, settings: Settings) -> None:
        assert settings.llm_api_key
        self._base_url = settings.llm_base_url.rstrip("/")
        self._api_key = settings.llm_api_key
        self._model = settings.llm_model
        self._timeout = settings.llm_timeout_seconds
        self._max_tokens = settings.llm_max_tokens
        self._response_format = settings.llm_response_format
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._loop = loop
        return self._client

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http().post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.TimeoutException as error:
            raise SpecDocToolError(f"specdoc 模型请求超时：{error}") from error
        except httpx.HTTPError as error:
            raise SpecDocToolError(f"specdoc 模型请求失败：{error}") from error
        if response.status_code >= 400:
            retryable = response.status_code in (408, 429) or response.status_code >= 500
            raise SpecDocToolError(
                f"specdoc 模型返回 HTTP {response.status_code}：{response.text[:200]}",
                retryable=retryable,
            )
        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise SpecDocToolError(f"specdoc 模型响应格式无效：{error}") from error
        if not isinstance(message, dict):
            raise SpecDocToolError("specdoc 模型响应 message 格式无效")
        return message

    def _payload(
        self, system: str, messages: list[dict[str, Any]], *, with_tools: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0,
            "max_tokens": self._max_tokens,
        }
        # Tool schemas and JSON response_format are not consistently supported together.
        if self._response_format and not with_tools:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def chat(self, system: str, user: str) -> str:
        return await self.chat_messages(system, [{"role": "user", "content": user}])

    async def chat_messages(
        self, system: str, messages: list[dict[str, Any]], *, temperature: float = 0
    ) -> str:
        payload = self._payload(system, messages)
        payload["temperature"] = temperature
        message = await self._request(payload)
        content = message.get("content")
        if not isinstance(content, str):
            raise SpecDocToolError("specdoc 模型响应缺少文本 content")
        return content

    async def chat_with_tools(
        self,
        system: str,
        user: str,
        *,
        tools: list[dict[str, Any]],
        executor: ToolExecutor,
        max_tool_calls: int = 6,
    ) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        allowed_tools = {
            tool.get("function", {}).get("name")
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        }
        payload = self._payload(system, messages, with_tools=True)
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        used_calls = 0
        while True:
            message = await self._request(payload)
            calls = message.get("tool_calls")
            if calls is None:
                content = message.get("content")
                if isinstance(content, str):
                    return content
                raise SpecDocToolError("specdoc 模型最终响应缺少文本 content")
            if not isinstance(calls, list) or not calls:
                raise SpecDocToolError("specdoc 模型 tool_calls 格式无效")
            if used_calls + len(calls) > max_tool_calls:
                raise SpecDocToolError("specdoc 工具调用次数超过上限", code="tool_budget_exhausted")
            assistant_message = {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": calls,
            }
            messages.append(assistant_message)
            for call in calls:
                try:
                    call_id = call["id"]
                    function = call["function"]
                    name = function["name"]
                    arguments = json.loads(function["arguments"])
                except (KeyError, TypeError, ValueError) as error:
                    raise SpecDocToolError(f"specdoc 模型工具参数无效：{error}") from error
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
                    raise SpecDocToolError("specdoc 模型工具调用缺少有效 id 或 name")
                if name not in allowed_tools:
                    raise SpecDocToolError("模型请求了未提供的 specdoc 工具", code="unknown_tool")
                if not isinstance(arguments, dict):
                    raise SpecDocToolError("specdoc 工具参数必须是 JSON object")
                try:
                    result = await executor(name, arguments)
                except SpecDocToolError:
                    raise
                except Exception as error:  # noqa: BLE001 - tool boundary normalizes failures
                    raise SpecDocToolError(f"specdoc 证据检索失败：{error}") from error
                messages.append(
                    {"role": "tool", "tool_call_id": call_id,
                     "content": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}
                )
                used_calls += 1
            payload = self._payload(system, messages, with_tools=True)
            payload["tools"] = tools
            payload["tool_choice"] = "auto"


def build_specdoc_llm_client(settings: Settings) -> OpenAICompatibleToolLLM | None:
    if not settings.llm_ready:
        return None
    return OpenAICompatibleToolLLM(settings)
