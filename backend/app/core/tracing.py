"""需求聊天请求的关联 ID 与结构化日志。

脚本 `scripts/trace_requirement_chat.py` 向运行中的后端发真实请求，内部过程
只能从服务端日志读取。本模块负责两件事：

1. 纯 ASGI 中间件为需求聊天请求生成 request ID，写入 ContextVar 并回填
   `X-Request-ID` 响应头。用纯 ASGI 而非 BaseHTTPMiddleware：后者的上下文
   在任务边界上可能丢失，ContextVar 传不到下游业务代码。
2. `log_event` 统一把 request ID 和字段拼到日志上；字段由调用方按白名单
   传入，绝不整体序列化用户消息、源码证据或模型响应。

日志级别沿用应用现有的 `APP_LOG_LEVEL`：INFO 记阶段概要，DEBUG 记单次
候选/检索等过程细节。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.datastructures import MutableHeaders

# 当前请求的关联 ID；脚本/前端可凭它把响应与后端日志对上。
_request_id: ContextVar[str | None] = ContextVar("requirement_chat_request_id", default=None)

# 只匹配需求聊天端点，其余请求（健康检查、静态资源等）不生成 ID。
_ROUTE_PATTERN = re.compile(r"^/projects/[^/]+/requirement-chat$")

# 工具参数里允许进日志的定位字段；自由文本一律不记录。
_TOOL_ARG_KEYS = ("category", "pageId", "chainId", "nodeId")
_MAX_FIELD_CHARS = 120


def current_request_id() -> str | None:
    return _request_id.get()


def _format_value(value: Any) -> str:
    if isinstance(value, (bool, int, float)):
        return str(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > _MAX_FIELD_CHARS:
        text = text[:_MAX_FIELD_CHARS] + "…"
    # 字符串统一加引号：字段值里若出现空格也能看出边界，便于肉眼/脚本解析。
    return json.dumps(text, ensure_ascii=False)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """记录一条带 request ID 的追踪事件；None 字段省略。"""
    if not logger.isEnabledFor(level):
        return
    parts = [f"trace.requirement_chat.event={event}"]
    request_id = current_request_id()
    if request_id is not None:
        parts.append(f"request_id={request_id}")
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_format_value(value)}")
    logger.log(level, " ".join(parts))


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def tool_args_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """工具参数白名单摘要：仅保留定位字段，丢弃任何自由文本。"""
    return {key: arguments[key] for key in _TOOL_ARG_KEYS if key in arguments}


class RequirementChatTracingMiddleware:
    """为需求聊天请求生成 request ID 的纯 ASGI 中间件。"""

    def __init__(self, app: Any, api_prefix: str = "/api/v1") -> None:
        self._app = app
        self._prefix = api_prefix.rstrip("/")
        self._logger = logging.getLogger(__name__)

    def _matches(self, scope: dict[str, Any]) -> bool:
        if scope.get("type") != "http":
            return False
        if scope.get("method") != "POST":
            return False
        path = scope.get("path", "")
        if not path.startswith(self._prefix):
            return False
        return bool(_ROUTE_PATTERN.match(path[len(self._prefix):]))

    async def __call__(self, scope, receive, send) -> None:
        if not self._matches(scope):
            await self._app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        token = _request_id.set(request_id)
        started = time.perf_counter()

        async def send_with_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
                log_event(
                    self._logger,
                    logging.INFO,
                    "http_response",
                    status_code=message.get("status"),
                )
            await send(message)

        try:
            await self._app(scope, receive, send_with_id)
        finally:
            log_event(
                self._logger,
                logging.INFO,
                "http_done",
                elapsed_ms=elapsed_ms(started),
            )
            _request_id.reset(token)
