"""模型接入：OpenAI 兼容端点的最小封装。

刻意不引 openai SDK：判定路径只发单轮 chat completion，httpx POST 足够；
provider 抽象为 LLMClient 协议，测试用 Fake 注入，编排层不感知 HTTP 细节。

结构性输出优先靠 `response_format=json_object` 约束，而不是靠 prompt 里
「只输出 JSON」这句话——后者是建议，前者是协议。但仍要保留 `_parse_json`
兜底：少数兼容端点会忽略该字段，或返回带代码围栏的内容。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class LLMCallError(Exception):
    """模型调用失败（网络 / HTTP / 响应缺内容）。

    `retryable` 决定编排层要不要重试：
      - True：网络抖动、限流、5xx、输出不是合法 JSON——换一次机会可能成功；
      - False：鉴权失败、模型不存在、请求体被拒——同样的请求再发一次必然同样失败，
        temperature=0 下重试只是空转，白白烧掉调用预算。

    默认 True 是保守选择：未知错误按可重试处理，最坏是浪费一次预算，
    而不是把一个本可恢复的故障直接判死。
    """

    def __init__(
        self, message: str, *, code: str = "llm_unavailable", retryable: bool = True
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class LLMClient(Protocol):
    async def chat(self, system: str, user: str) -> str:
        """单轮补全。实现方负责超时与错误归一：失败抛 LLMCallError。"""

    async def chat_messages(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0,
    ) -> str:
        """多轮补全：messages 为全量历史（role ∈ user/assistant）。失败抛 LLMCallError。"""


class OpenAICompatibleLLM:
    """POST {base_url}/chat/completions 的最小实现。

    AsyncClient 跨调用复用（懒创建、按事件循环绑定）：TLS/TCP 握手可能高达
    数十秒（中转网络慢时实测 44s+），每次调用新建客户端意味着文档生成连打
    4 次就要付 4 次握手，60s 超时必爆；复用后只有第一次付这个成本。
    """

    def __init__(self, settings: Settings) -> None:
        # 只在 llm_ready 后构造，key 缺失属于装配错误而非运行时分支
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
        """复用客户端；事件循环变了（测试多次 asyncio.run）则重建。"""
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            # 旧循环已退出，无法（也无需）优雅关闭旧客户端，交给 GC
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._loop = loop
        return self._client

    def _build_payload(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "max_tokens": self._max_tokens,
        }
        if self._response_format:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def chat(self, system: str, user: str) -> str:
        return await self.chat_messages(system, [{"role": "user", "content": user}])

    async def chat_messages(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0,
    ) -> str:
        payload = self._build_payload(system, messages, temperature=temperature)
        try:
            response = await self._http().post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.TimeoutException as error:
            # 超时可能只是这一次慢：重试有意义
            raise LLMCallError(f"模型请求超时：{error}") from error
        except httpx.HTTPError as error:
            raise LLMCallError(f"模型请求失败：{error}") from error

        if response.status_code >= 400:
            # 4xx 里只有限流和请求超时值得重试；其余（401/403/404/422）是配置或
            # 请求体问题，重发同样的请求只会得到同样的结果。
            retryable = response.status_code in (408, 429) or response.status_code >= 500
            raise LLMCallError(
                f"模型返回 HTTP {response.status_code}：{response.text[:200]}",
                retryable=retryable,
            )
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMCallError(f"模型响应缺少 choices[0].message.content：{error}") from error
        if not isinstance(content, str):
            raise LLMCallError("模型响应的 content 不是字符串")
        return content


def build_llm_client(settings: Settings) -> LLMClient | None:
    """按配置构造 provider；未就绪时返回 None（调用方走 dry-run / 拒绝路径）。"""
    if not settings.llm_ready:
        return None
    return OpenAICompatibleLLM(settings)
