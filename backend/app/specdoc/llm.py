"""Specdoc-only OpenAI-compatible LLM client with bounded tool execution."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.config import Settings
from app.core.tracing import elapsed_ms, log_event
from app.requirement.llm import LLMCallError

logger = logging.getLogger(__name__)

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
        # 工具循环内的单次请求重试：只针对可重试的传输层错误（超时 / 429 / 5xx）
        self._tool_retry_attempts = 3
        self._tool_retry_backoff = 0.5

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._loop = loop
        return self._client

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        messages = payload.get("messages") or []
        message_chars = sum(
            len(item.get("content") or "")
            for item in messages
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        )
        log_event(
            logger,
            logging.DEBUG,
            "llm_request",
            model=self._model,
            message_count=len(messages),
            message_characters=message_chars,
            has_tools=bool(payload.get("tools")),
        )
        try:
            response = await self._http().post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.TimeoutException as error:
            log_event(
                logger,
                logging.WARNING,
                "llm_request_error",
                model=self._model,
                error="timeout",
                elapsed_ms=elapsed_ms(started),
            )
            raise SpecDocToolError("specdoc 模型请求超时", code="llm_timeout") from error
        except httpx.HTTPError as error:
            log_event(
                logger,
                logging.WARNING,
                "llm_request_error",
                model=self._model,
                error=type(error).__name__,
                elapsed_ms=elapsed_ms(started),
            )
            raise SpecDocToolError("specdoc 模型请求失败", code="llm_unavailable") from error
        if response.status_code >= 400:
            retryable = response.status_code in (408, 429) or response.status_code >= 500
            log_event(
                logger,
                logging.WARNING,
                "llm_request_error",
                model=self._model,
                status_code=response.status_code,
                retryable=retryable,
                elapsed_ms=elapsed_ms(started),
            )
            raise SpecDocToolError(
                f"specdoc 模型返回 HTTP {response.status_code}",
                code="llm_http_error",
                retryable=retryable,
            )
        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            log_event(
                logger,
                logging.WARNING,
                "llm_request_error",
                model=self._model,
                error="invalid_response",
                elapsed_ms=elapsed_ms(started),
            )
            raise SpecDocToolError(
                "specdoc 模型响应格式无效", code="invalid_llm_response"
            ) from error
        if not isinstance(message, dict):
            raise SpecDocToolError("specdoc 模型响应 message 格式无效")
        calls = message.get("tool_calls") or []
        content = message.get("content")
        usage = data.get("usage")
        total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        log_event(
            logger,
            logging.INFO,
            "llm_response",
            model=self._model,
            elapsed_ms=elapsed_ms(started),
            tool_call_count=len(calls) if isinstance(calls, list) else 0,
            content_characters=len(content) if isinstance(content, str) else 0,
            total_tokens=total_tokens if isinstance(total_tokens, int) else None,
        )
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

    async def _request_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """工具循环内的请求重试。

        只重试可重试的传输层错误（超时 / 429 / 5xx / 网络），因为此时对话状态
        尚未推进，重发同一 payload 是安全的；协议类错误（工具参数非法、未知工具）
        在 temperature=0 下重发不会有不同结果，直接上抛。
        """
        attempts = max(1, self._tool_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return await self._request(payload)
            except SpecDocToolError as error:
                if not error.retryable or attempt >= attempts:
                    raise
                logger.warning(
                    "specdoc 工具请求失败，第 %d/%d 次重试：%s", attempt, attempts, error
                )
                await asyncio.sleep(self._tool_retry_backoff * attempt)
        raise AssertionError("unreachable")

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
        user: str | None = None,
        *,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]],
        executor: ToolExecutor,
        temperature: float = 0,
        checkpoint: Any | None = None,
        scope: str = "steps",
        max_model_requests: int | None = None,
        max_tool_calls: int | None = None,
    ) -> str:
        if messages is not None and user is not None:
            raise ValueError("pass either user or messages, not both")
        if messages is None:
            if user is None:
                raise ValueError("user or messages is required")
            messages = [{"role": "user", "content": user}]
        else:
            messages = [dict(message) for message in messages]
            if not messages or any(
                message.get("role") not in {"user", "assistant"}
                or not isinstance(message.get("content"), str)
                for message in messages
            ):
                raise ValueError("messages must contain user/assistant text messages")
        allowed_tools = {
            tool.get("function", {}).get("name")
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        }
        # 断点续跑：checkpoint 为 None 时与旧行为完全一致（正式任务链与
        # business_analysis 分支都走这条）。恢复只复用「已完成」的部分：
        # 已拿到的 assistant 响应、已执行完的工具结果；未完成的轮次重发请求。
        state = await checkpoint.load_scope(scope) if checkpoint is not None else None
        if state is not None:
            restored = state.get("messages")
            if isinstance(restored, list) and restored:
                messages = [dict(item) for item in restored if isinstance(item, dict)]
            restore_ledger(executor, state.get("ledger"))
            model_requests = int(state.get("model_requests") or 0)
            tool_calls_used = int(state.get("tool_calls") or 0)
            log_event(
                logger,
                logging.INFO,
                "llm_tool_loop_resumed",
                scope=scope,
                model_requests=model_requests,
                tool_calls=tool_calls_used,
                messages=len(messages),
            )
        else:
            model_requests = 0
            tool_calls_used = 0

        async def _persist(status: str = "running") -> None:
            if checkpoint is None:
                return
            await checkpoint.save_scope(
                scope,
                {
                    "schema": 1,
                    "status": status,
                    "messages": messages,
                    "model_requests": model_requests,
                    "tool_calls": tool_calls_used,
                    "ledger": _dump_ledger(executor),
                },
            )

        while True:
            if max_model_requests is not None and model_requests >= max_model_requests:
                raise SpecDocToolError(
                    "specdoc 工具循环模型请求次数已达上限", code="tool_budget_exhausted"
                )
            payload = self._payload(system, messages, with_tools=True)
            payload["temperature"] = temperature
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            # 轮内只做传输层重试（_request_with_retry）；耗尽后上抛，由章节级
            # 续跑（infer._chat_with_tools_resume）带着 checkpoint 重新进入循环。
            message = await self._request_with_retry(payload)
            model_requests += 1
            calls = message.get("tool_calls")
            if calls is None:
                content = message.get("content")
                if isinstance(content, str):
                    await _persist("answered")
                    return content
                raise SpecDocToolError("specdoc 模型最终响应缺少文本 content")
            if not isinstance(calls, list) or not calls:
                raise SpecDocToolError("specdoc 模型 tool_calls 格式无效")
            assistant_message = {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": calls,
            }
            messages.append(assistant_message)
            # 先落盘 assistant 响应，再执行工具：进程在工具执行前挂掉时，
            # 恢复后不会重复请求这一轮模型。
            await _persist()
            answered_ids = {
                item.get("tool_call_id")
                for item in messages
                if isinstance(item, dict) and item.get("role") == "tool"
            }
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
                if call_id in answered_ids:
                    # 恢复场景：这一条工具回复已经落盘，直接跳过
                    continue
                if max_tool_calls is not None and tool_calls_used >= max_tool_calls:
                    raise SpecDocToolError(
                        "specdoc 工具调用次数已达上限", code="tool_budget_exhausted"
                    )
                tool_calls_used += 1
                result = await _execute_tool(name, arguments, executor)
                log_event(
                    logger,
                    logging.DEBUG,
                    "llm_tool_call",
                    tool=name,
                    status=result.get("status") if isinstance(result, dict) else None,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    }
                )
                answered_ids.add(call_id)
                await _persist()


async def _execute_tool(
    name: str, arguments: dict[str, Any], executor: ToolExecutor
) -> dict[str, Any]:
    """工具失败回传给模型，不打断整章——与旧实现保持一致。"""
    try:
        return await executor(name, arguments)
    except SpecDocToolError as error:
        return {"status": "error", "code": error.code, "message": str(error)}
    except Exception as error:  # noqa: BLE001 - tool boundary normalizes failures
        return {"status": "error", "code": "tool_error", "message": str(error)}


def _dump_ledger(executor: ToolExecutor) -> list[dict[str, Any]]:
    """把检索器已接纳的事实持久化：恢复后引文校验仍需同一份事实边界。"""
    ledger = getattr(executor, "ledger", None)
    if ledger is None:
        ledger = getattr(getattr(executor, "__self__", None), "ledger", None)
    facts = getattr(ledger, "facts", None)
    if not isinstance(facts, dict):
        return []
    return [fact.as_dict() for fact in facts.values()]


def restore_ledger(executor: ToolExecutor, items: Any) -> None:
    ledger = getattr(executor, "ledger", None)
    if ledger is None:
        ledger = getattr(getattr(executor, "__self__", None), "ledger", None)
    if ledger is None:
        return
    ledger.restore(items)


def build_specdoc_llm_client(settings: Settings) -> OpenAICompatibleToolLLM | None:
    if not settings.llm_ready:
        return None
    return OpenAICompatibleToolLLM(settings)
