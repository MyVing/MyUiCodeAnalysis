"""模型接入：OpenAI 兼容端点的最小封装。

刻意不引 openai SDK：判定路径只发单轮 chat completion，httpx POST 足够；
provider 抽象为 LLMClient 协议，测试用 Fake 注入，编排层不感知 HTTP 细节。
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class LLMCallError(Exception):
    """模型调用失败（网络 / HTTP / 响应缺内容）。

    code 会成为任务错误码与告警码的一部分：llm_unavailable 表示端点层面失败，
    编排层捕获后降级或重试，任务不会带堆栈死掉。
    """

    def __init__(self, message: str, *, code: str = "llm_unavailable") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LLMClient(Protocol):
    async def chat(self, system: str, user: str) -> str:
        """单轮补全。实现方负责超时与错误归一：失败抛 LLMCallError。"""


class OpenAICompatibleLLM:
    """POST {base_url}/chat/completions 的最小实现。

    每次调用新建 AsyncClient：一次需求定位最多几次调用，连接复用的收益
    不值得引入客户端生命周期管理（关闭钩子、事件循环绑定）。
    """

    def __init__(self, settings: Settings) -> None:
        # 只在 llm_ready 后构造，key 缺失属于装配错误而非运行时分支
        assert settings.llm_api_key
        self._base_url = settings.llm_base_url.rstrip("/")
        self._api_key = settings.llm_api_key
        self._model = settings.llm_model
        self._timeout = settings.llm_timeout_seconds

    async def chat(self, system: str, user: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
        except httpx.HTTPError as error:
            raise LLMCallError(f"模型请求失败：{error}") from error

        if response.status_code >= 400:
            raise LLMCallError(
                f"模型返回 HTTP {response.status_code}：{response.text[:200]}"
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
