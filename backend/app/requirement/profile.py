"""离线页面画像：分析完成后给每个页面写一段业务描述，供在线定位复用。

## 为什么要单独做一次离线调用

在线定位的 prompt 里已经有「页面名 + 路由 + 链路摘要」。缺的是**整页语义**：

> 纯展示页面没有链路，索引里只剩几条文案；
> 链路很多的页面，模型要在几十条交互里猜「这一页是干什么的」。

画像补的就是这一句：一次批量调用，给每个页面写 ≤120 字的业务描述，之后每次
需求定位都直接读缓存（`AnalysisRepository.get_page_profiles`），不再重复烧模型。

## 三条硬约束

1. **画像不是证据。** 它是模型生成的文本，不是源码原文。引文校验（`match_quote`）
   永远只认 `chains.corpus` / 页面 evidence，画像文本不得进入任何语料集合。
   否则模型先编一句「这个页面支持导出」，下一轮自己引用它通过校验——自证循环。
2. **模型只产出文本。** 与 `features.py` 同一条原则：输出里除了页面 nodeId
   （用于归属，服务端会校验它必须在候选集合内）没有任何结构判断。
3. **失败不影响分析。** 画像只是定位的辅助线索。调用失败 / 输出无效时把原因
   写进 payload 的 `warnings`，下游定位照常工作（索引里少一段描述而已）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.contracts import IRGraph
from app.requirement.chains import ChainIndex, derive_chains
from app.requirement.judge import parse_json
from app.requirement.llm import LLMCallError, LLMClient
from app.requirement.locate import route_paths_of
from app.requirement.page_profile import PageProfile, ProfilePayload
from app.requirement.recall import collect_page_evidences, page_nodes

logger = logging.getLogger(__name__)

# 单段描述的长度上限。中文 120 字已经足够说清「这页在做什么」；
# 更长的描述会把 prompt 预算从链路细节那里挤走。
MAX_SUMMARY_CHARS = 120

# 每个页面进 prompt 的文案条数：与 locate.py 的 _MAX_PAGE_QUOTES 同量级，
# 两处都只是「辅助线索」，不是落点。
_MAX_PAGE_QUOTES = 6

# 每个页面进 prompt 的链路条数。画像阶段只要知道「这页有哪些动作」，
# 链路的完整细节留给在线定位。
_MAX_CHAIN_HINTS = 6

_SYSTEM_PROMPT = """\
你在为一个 React 项目的页面写业务画像。输入是一份页面清单：每个页面带路由路径、\
可见文案、以及页面上真实存在的交互（元素名与角色）。

任务：为每个页面写一句业务描述，说明「这个页面给用户做什么」。

规则：
1. 只描述给出的页面，不要新增页面，不要评论代码质量。
2. 每条描述不超过 120 字，用中文，写成陈述句。
3. 描述基于给出的文案与交互推断，看不出来的不要编。纯静态页面就如实说它是展示页。
4. 不要出现文件路径、组件 id 或任何形如 `xxx.tsx#Yyy` 的标识符。
5. 只输出 JSON，不要输出任何其他文字。格式：
{"profiles": [{"nodeId": "给定的页面 nodeId", "summary": "一句业务描述"}]}
"""


@dataclass
class ProfileOutcome:
    payload: ProfilePayload
    calls_used: int = 0
    # True 表示模型没产出任何有效描述，下游应完全忽略画像
    degraded: bool = False


@dataclass(frozen=True)
class _PageHint:
    node_id: str
    name: str
    route_paths: tuple[str, ...]
    quotes: tuple[str, ...]
    chains: tuple[tuple[str, str | None], ...]

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"nodeId": self.node_id, "name": self.name}
        if self.route_paths:
            payload["routePaths"] = list(self.route_paths)
        if self.quotes:
            payload["quotes"] = list(self.quotes)
        if self.chains:
            payload["chains"] = [
                {"element": name, **({"role": role} if role else {})}
                for name, role in self.chains
            ]
        return payload


def build_profile_hints(
    graph: IRGraph,
    chains: ChainIndex | None = None,
    *,
    max_pages: int | None = None,
) -> list[_PageHint]:
    """把图整理成画像阶段的输入清单（纯确定性，可单测）。

    页面集合与 `locate.build_page_index` 同源（`page_nodes` + 链路归属），
    这样「画像里有哪些页面」与「定位时能选哪些页面」不会各说各话。
    """
    chains = chains if chains is not None else derive_chains(graph)
    nodes = {node.id: node for node in graph.nodes}
    pages, route_by_page, layout_ids = page_nodes(graph)
    evidences = collect_page_evidences(graph, pages, layout_ids)

    hints: list[_PageHint] = []
    for page_id in sorted({page.id for page in pages} | set(chains.by_page)):
        node = nodes.get(page_id)
        if node is None:
            continue
        page_chains = chains.for_page(page_id)
        hints.append(
            _PageHint(
                node_id=page_id,
                name=node.name,
                route_paths=route_paths_of(page_id, route_by_page, graph),
                quotes=tuple(item.text for item in (evidences.get(page_id) or ()))[
                    :_MAX_PAGE_QUOTES
                ],
                chains=tuple(
                    (chain.element.name, chain.view_role)
                    for chain in page_chains[:_MAX_CHAIN_HINTS]
                ),
            )
        )

    if max_pages is not None and len(hints) > max_pages:
        # 与在线定位同样的预筛口径：交互多的页面信息量大，优先保留
        hints.sort(key=lambda hint: (-len(hint.chains), hint.node_id))
        hints = hints[:max_pages]
    return hints


async def profile_pages(
    graph: IRGraph,
    llm: LLMClient,
    *,
    analysis_id: str,
    model: str | None = None,
    max_calls: int = 2,
    max_pages: int | None = None,
) -> ProfileOutcome:
    """一次性给全部页面写画像。失败不抛异常，返回 degraded 结果。"""
    hints = build_profile_hints(graph, max_pages=max_pages)
    generated_at = datetime.now(UTC)
    if not hints:
        return ProfileOutcome(
            payload=ProfilePayload(
                analysis_id=analysis_id,
                generated_at=generated_at,
                model=model,
                warnings=["图中没有可画像的页面。"],
            ),
            degraded=True,
        )

    hint_by_id = {hint.node_id: hint for hint in hints}
    user_prompt = "页面清单：\n" + json.dumps(
        [hint.to_payload() for hint in hints], ensure_ascii=False, indent=2
    )

    raw: dict[str, object] | None = None
    last_error: str | None = None
    calls = 0
    for _ in range(max_calls):
        calls += 1
        prompt = (
            user_prompt
            if last_error is None
            else f"{user_prompt}\n\n上一次输出无效（{last_error}），请严格按规则重新输出。"
        )
        try:
            text = await llm.chat(_SYSTEM_PROMPT, prompt)
        except LLMCallError as error:
            last_error = error.message
            if not error.retryable:
                break
            continue
        parsed = parse_json(text)
        if parsed is not None:
            raw = parsed
            break
        last_error = "输出不是合法的目标 JSON"

    warnings: list[str] = []
    if raw is None:
        return ProfileOutcome(
            payload=ProfilePayload(
                analysis_id=analysis_id,
                generated_at=generated_at,
                model=model,
                warnings=[
                    f"画像生成失败（{last_error or '未知原因'}），"
                    "本次定位不带页面描述。"
                ],
            ),
            calls_used=calls,
            degraded=True,
        )

    profiles: list[PageProfile] = []
    seen: set[str] = set()
    rejected = 0
    for entry in _model_profiles(raw):
        page_id = entry.get("nodeId")
        summary = entry.get("summary")
        if not isinstance(page_id, str) or not isinstance(summary, str):
            rejected += 1
            continue
        hint = hint_by_id.get(page_id)
        if hint is None:
            rejected += 1
            continue
        text = " ".join(summary.split())
        if not text or len(text) > MAX_SUMMARY_CHARS * 2:
            rejected += 1
            continue
        if page_id in seen:
            rejected += 1
            continue
        seen.add(page_id)
        profiles.append(
            PageProfile(
                page_id=page_id,
                name=hint.name,
                route_paths=list(hint.route_paths),
                summary=text[:MAX_SUMMARY_CHARS],
            )
        )

    if rejected:
        warnings.append(f"模型输出的 {rejected} 条画像未通过校验，已忽略。")
    if not profiles:
        return ProfileOutcome(
            payload=ProfilePayload(
                analysis_id=analysis_id,
                generated_at=generated_at,
                model=model,
                warnings=[*warnings, "模型没有给出任何可用的页面描述。"],
            ),
            calls_used=calls,
            degraded=True,
        )

    missing = len(hints) - len(profiles)
    if missing > 0:
        warnings.append(f"{missing} 个页面没有生成描述，定位时按无画像处理。")

    profiles.sort(key=lambda profile: profile.page_id)
    return ProfileOutcome(
        payload=ProfilePayload(
            analysis_id=analysis_id,
            generated_at=generated_at,
            model=model,
            profiles=profiles,
            warnings=warnings,
        ),
        calls_used=calls,
    )


def _model_profiles(raw: dict[str, object]) -> list[dict[str, object]]:
    entries = raw.get("profiles")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


__all__ = [
    "MAX_SUMMARY_CHARS",
    "PageProfile",
    "ProfileOutcome",
    "ProfilePayload",
    "build_profile_hints",
    "profile_pages",
]