"""AI 为主的语义定位：把「全部页面 + 全部交互链路」交给模型挑。

## 为什么要换掉「词法召回的 Top-30」

`recall.py` 的候选集完全由 `lexicon.json` 决定：需求里的词一旦命中不了词表，
候选集就是空的，模型连「挑」的机会都没有。要支持任意项目、任意文案，就得承认
一件事——**词表永远补不完**。

所以这里把候选全集换成「这份代码里真实存在的页面与交互」，模型的工作变成
「从这里挑一条」，而不是「先猜哪些页面可能与这句需求有关」。词法不再是闸门，
只在三处保留（见 §词法的三个作用）。

## AI 仍然不产出节点

模型只说三件事：哪个页面、哪条链路、引用了哪句原文。全都要过服务端校验：

| 校验 | 失败去向 |
| --- | --- |
| `featureId` ∈ 本次功能点 | `unknown_feature` |
| `pageNodeId` ∈ 页面索引 | `node_not_in_candidates` |
| `chainId` ∈ 链路集合，且属于所选页面 | `chain_not_in_candidates` |
| `quote` 在语料里逐字存在（有链路时用链路语料，无链路时用页面语料） | `quote_not_found` |
| 字段类型 | `invalid_shape` |

链路由 `chains.py` 确定性推导，AI 既不构造也不修改它——它只能「选」。
这条边界一旦松动，链路上「每一段都是事实」的保证就没了。

## 词法的三个作用

1. **精确定位**：需求里出现明确标识符（`/api/users`、`showFilter`、`用户列表`）时，
   词法命中作为候选的既有信号留在 `candidates` 里供人复核；
2. **排序与预筛**：页面过多时按词法得分决定谁进 prompt（并如实告警）；
3. **兜底**：dry-run 与模型不可用时整条链路退回 `recall` + `judge`，语义不变。

## 页面数超限

页面索引太长会撑爆上下文。超过 `max_pages` 时按词法得分预筛，并记
`pages_prefiltered` 告警——**不能静默截断**：用户会以为 AI 看过了全部页面。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.contracts import (
    EdgeKind,
    IRGraph,
    IRNode,
    NodeKind,
    RequirementWarning,
    Signal,
    SignalKind,
    Source,
    VerifiedEvidence,
)
from app.contracts.requirement import (
    ActionChain as RequirementChain,
)
from app.contracts.requirement import (
    Candidate,
    Dropped,
    Location,
    Match,
    Reason,
    Severity,
    Unmatched,
)
from app.requirement.chains import ActionChain, ChainIndex
from app.requirement.features import DecomposedFeature
from app.requirement.judge import MAX_CONFIDENCE, QuoteMatch, match_quote, parse_json
from app.requirement.llm import LLMCallError, LLMClient
from app.requirement.page_profile import ProfilePayload
from app.requirement.recall import (
    CandidateDetail,
    RecallEvidence,
    RecallResult,
    collect_page_evidences,
    page_nodes,
)
from app.requirement.subgraph import build_subgraph

# 页面自有文案进 prompt 的条数上限。页面文案只是辅助线索（「这个页面是做什么的」），
# 真正的落点在链路里；给文案留太多条会把链路挤到上下文边缘。
_MAX_PAGE_QUOTES = 8

# 每条链路进 prompt 的可用原文条数。校验仍然对全量语料做（链路的 corpus 不截断），
# 这里只控制 prompt 体积。
_MAX_CHAIN_QUOTES = 5

# 每个页面进 prompt 的链路条数上限。一个页面几十条交互是常态，全量列出会让模型的
# 选择空间退化成噪音；截掉的条数在 payload 里如实说明（omittedChains）。
MAX_CHAINS_PER_PAGE_IN_PROMPT = 8

_SYSTEM_PROMPT = """\
你是 React 项目的需求定位助手。输入是一段需求拆解出的功能点，和一份「页面与交互链路」\
索引——每个页面下列出了它真实存在的交互（元素 → 处理函数 → 状态 / 接口 / 跳转），\
每段都带可直接引用的原文。

你的任务：判断每个功能点由哪个页面、哪个交互实现。

索引里的 summary（如果有）是别人对这个页面的一句话业务描述，只能用来理解页面用途；\
它不能作为 quote 的来源。

规则：
1. pageNodeId 只能从给出的页面里选，chainId 只能从该页面列出的链路里选。禁止发明。
2. 需求描述的是「页面上的某个动作 / 控件 / 跳转」时，必须给出 chainId；只有整页级的 \
需求（如「这个页面在做什么」）才省略 chainId。
3. quote 是你判断依据的原文，必须从该链路的 quotes（省略 chainId 时从该页面的 \
quotes）里复制：优先整条复制；确实需要截取时，截取的片段至少 4 个字、且只对应一段\
原文。服务端会逐字校验，复制不到的匹配会被整条丢弃。
4. 链路的 terminatedAt 非空表示链路在此中断（例如外链、处理逻辑由父组件注入）。\
此时在 reason 里说明「目标在路由表外 / 逻辑不可见」，不要假装它跳到了某个站内页面。
5. confidence 为 0 到 1 之间的小数，表示你对该匹配的把握，宁可保守。
6. featureId 必须来自输入的功能点清单。
7. 需求超纲、或索引里找不到能实现该功能点的交互时，把 featureId 放进 unmatched 并说明理由。
8. 只输出 JSON，不要输出任何其他文字。格式：
{"matches": [{"featureId": "f1", "pageNodeId": "...", "chainId": "...", "quote": "...", \
"confidence": 0.8, "reason": "一句话理由"}], "unmatched": [{"featureId": "f2", \
"reason": "一句话理由"}]}
"""


@dataclass(frozen=True)
class ChainBrief:
    """链路进 prompt 的紧凑形态。完整链路留在 `ChainIndex` 里，按 id 回查。"""

    chain_id: str
    element: str
    role: str | None
    href: str | None
    terminated_at: str | None
    quotes: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"chainId": self.chain_id, "element": self.element}
        if self.role:
            payload["role"] = self.role
        if self.href:
            payload["href"] = self.href
        if self.terminated_at:
            payload["terminatedAt"] = self.terminated_at
        payload["quotes"] = list(self.quotes)
        return payload


@dataclass(frozen=True)
class PageEntry:
    """一个页面在索引里的形态：路由 + 自有文案 + 交互链路。

    `evidence` 是页面自有的可定位原文（文案 / props / 接口 / 组件名），与召回层
    共用同一套归属规则。它让「整页级需求」仍能引用静态文案——这类页面可能一条
    链路都没有，若引文校验只认链路语料，纯展示页面就永远匹配不上。
    """

    node_id: str
    name: str
    route_paths: tuple[str, ...]
    file: str | None
    chains: tuple[ChainBrief, ...]
    # 该页面全部链路数（chains 可能被 prompt 上限截断）
    total_chains: int
    evidence: tuple[RecallEvidence, ...] = ()
    # 词法得分：只用于预筛与展示「为什么它在索引里靠前」，不是入选门槛
    lexical_score: float = 0.0
    # 离线画像给的一句业务描述。是模型文本、不是源码原文，只进 prompt，
    # 绝不进语料（见 profile.py 的约束 1）。
    summary: str | None = None

    @property
    def page_corpus(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.text for item in self.evidence))

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "nodeId": self.node_id,
            "name": self.name,
            "routePaths": list(self.route_paths),
            "chains": [chain.to_payload() for chain in self.chains],
        }
        if self.summary:
            payload["summary"] = self.summary
        if self.file:
            payload["file"] = self.file
        if self.evidence:
            payload["quotes"] = list(self.page_corpus)[:_MAX_PAGE_QUOTES]
        if self.total_chains > len(self.chains):
            payload["omittedChains"] = self.total_chains - len(self.chains)
        return payload


@dataclass
class PageIndex:
    """全部页面 + 链路。页面顺序即 prompt 顺序，必须稳定。"""

    pages: list[PageEntry]
    by_id: dict[str, PageEntry]
    # 预筛掉的页面数（0 表示全量进 prompt）
    omitted_pages: int = 0

    def __len__(self) -> int:
        return len(self.pages)

    def corpus_of(self, chain_id: str | None, page_id: str, chains: ChainIndex) -> tuple[str, ...]:
        """引文校验范围：有链路就只看该链路；页面级匹配退到「页面自有 + 该页链路」。

        注意这里**不包含** `PageEntry.summary`：画像是模型生成的文本，
        一旦进语料，模型就能引用自己编的描述通过校验。
        """
        if chain_id is not None:
            return chains.corpus_of(chain_id)
        page = self.by_id.get(page_id)
        page_corpus = page.page_corpus if page is not None else ()
        return tuple(dict.fromkeys((*page_corpus, *chains.page_corpus(page_id))))


@dataclass
class LocateOutcome:
    matches: list[Match] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)
    warnings: list[RequirementWarning] = field(default_factory=list)
    calls_used: int = 0
    # 被 match 引用的链路 id（有序、去重）：编排层据此填充 interactionChains
    chain_ids: list[str] = field(default_factory=list)
    # True 表示模型没产出有效结果，编排层应保留词法候选作为结论
    degraded: bool = False


def build_page_index(
    graph: IRGraph,
    chains: ChainIndex,
    *,
    recall_result: RecallResult | None = None,
    profile: ProfilePayload | None = None,
    max_pages: int | None = None,
    max_chains_per_page: int = MAX_CHAINS_PER_PAGE_IN_PROMPT,
) -> PageIndex:
    """把图 + 链路整理成喂给模型的页面索引。

    页面集合与 `chains.py` 的判定一致（被 route 渲染的组件；无 route 时退化为
    顶层组件），这样「索引里有哪些页面」与「链路归属到哪个页面」不会各说各话。
    额外并入 `chains.by_page` 的键：没有路由表的项目里链路仍然存在。

    `profile` 是离线画像（阶段 C）。只取它的 `summary` 进 prompt，不参与任何校验。
    画像按图版本存放，调用方负责保证它和 `graph` 是同一份分析。
    """
    nodes = {node.id: node for node in graph.nodes}
    pages, route_by_page, layout_ids = page_nodes(graph)
    evidences = collect_page_evidences(graph, pages, layout_ids)

    page_ids = sorted({page.id for page in pages} | set(chains.by_page))
    scores = _lexical_scores(recall_result)

    entries: list[PageEntry] = []
    for page_id in page_ids:
        node = nodes.get(page_id)
        if node is None or not _is_page(node):
            continue
        page_chains = chains.for_page(page_id)
        entries.append(
            PageEntry(
                node_id=page_id,
                name=node.name,
                route_paths=route_paths_of(page_id, route_by_page, graph),
                file=node.file,
                chains=_chain_briefs(page_chains, max_chains_per_page),
                total_chains=len(page_chains),
                evidence=tuple(evidences.get(page_id) or ()),
                lexical_score=scores.get(page_id, 0.0),
                summary=profile.summary_of(page_id) if profile is not None else None,
            )
        )

    omitted = 0
    if max_pages is not None and len(entries) > max_pages:
        # 预筛顺序：有链路的优先（纯静态页给不出交互落点），再按词法得分，
        # 最后按 id 保证稳定。
        entries.sort(key=lambda entry: (-len(entry.chains), -entry.lexical_score, entry.node_id))
        omitted = len(entries) - max_pages
        entries = entries[:max_pages]

    entries.sort(key=lambda entry: (-entry.lexical_score, entry.node_id))
    return PageIndex(
        pages=entries,
        by_id={entry.node_id: entry for entry in entries},
        omitted_pages=omitted,
    )


def _lexical_scores(result: RecallResult | None) -> dict[str, float]:
    if result is None:
        return {}
    return {detail.candidate.nodeId: detail.candidate.score for detail in result.candidates}


def _is_page(node: IRNode) -> bool:
    return node.kind in (NodeKind.component, NodeKind.server_component)


def route_paths_of(
    page_id: str, route_by_page: dict[str, str], graph: IRGraph
) -> tuple[str, ...]:
    """页面绑定的全部路由路径（不是只取 map 里的那一条）。

    `FullLayout` 挂在 `/`、`/posts`、`/products` 下时，用户说「首页的 GitHub 链接」
    与「文章页的 GitHub 链接」指向同一个落点。只回显第一条会让 prompt 里的信息
    少于事实。
    """
    nodes = {node.id: node for node in graph.nodes}
    paths = {
        nodes[edge.from_].name
        for edge in graph.edges
        if edge.kind is EdgeKind.renders
        and edge.to == page_id
        and edge.from_ in nodes
        and nodes[edge.from_].kind is NodeKind.route
    }
    if not paths and page_id in route_by_page:
        route = nodes.get(route_by_page[page_id])
        if route is not None:
            paths.add(route.name)
    return tuple(sorted(paths))


def _chain_briefs(chains: Sequence[ActionChain], limit: int) -> tuple[ChainBrief, ...]:
    return tuple(
        ChainBrief(
            chain_id=chain.id,
            element=chain.element.name,
            role=chain.view_role,
            href=chain.href,
            terminated_at=chain.terminated_at,
            quotes=tuple(chain.corpus[:_MAX_CHAIN_QUOTES]),
        )
        for chain in chains[:limit]
    )


def _build_user_prompt(
    requirement: str,
    features: list[DecomposedFeature],
    index: PageIndex,
) -> str:
    feature_lines = "\n".join(f"{feature.id}: {feature.text}" for feature in features)
    payload = [entry.to_payload() for entry in index.pages]
    return (
        f"需求：{requirement}\n\n"
        f"功能点：\n{feature_lines}\n\n"
        f"页面与交互链路索引：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


async def locate(
    features: list[DecomposedFeature],
    index: PageIndex,
    chains: ChainIndex,
    graph: IRGraph,
    llm: LLMClient,
    *,
    requirement: str,
    max_calls: int,
) -> LocateOutcome:
    """让模型从页面索引里挑落点，并逐条校验。"""
    if not index.pages:
        return LocateOutcome(
            degraded=True,
            unmatched=[
                Unmatched(featureId=feature.id, reason="图中没有可定位的页面。")
                for feature in features
            ],
            warnings=[
                RequirementWarning(
                    code="locate_skipped",
                    severity=Severity.warning,
                    message="页面索引为空，跳过模型定位，全部功能点记为未匹配。",
                )
            ],
        )

    user_prompt = _build_user_prompt(requirement, features, index)
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
            # 不可重试的错误（鉴权失败、模型不存在、请求体被拒）再发同样的请求
            # 必然同样失败，继续只会烧掉预算并拖长用户等待。
            if not error.retryable:
                break
            continue
        parsed = parse_json(text)
        if parsed is not None:
            raw = parsed
            break
        last_error = "输出不是合法的目标 JSON"

    if raw is None:
        return LocateOutcome(
            calls_used=calls,
            degraded=True,
            unmatched=[
                Unmatched(
                    featureId=feature.id,
                    reason="模型调用失败，已降级为确定性召回结果，请查看 candidates。",
                )
                for feature in features
            ],
            warnings=_prefilter_warnings(index)
            + [
                RequirementWarning(
                    code="llm_unavailable",
                    severity=Severity.error,
                    message=f"模型定位在 {calls} 次尝试内未产出有效结果"
                    f"（{last_error or '未知原因'}），已降级为确定性召回。",
                )
            ],
        )

    outcome = _assemble(raw, features, index, chains, graph, calls)
    outcome.warnings.extend(_prefilter_warnings(index))
    return outcome


def _prefilter_warnings(index: PageIndex) -> list[RequirementWarning]:
    if not index.omitted_pages:
        return []
    return [
        RequirementWarning(
            code="pages_prefiltered",
            severity=Severity.info,
            message=(
                f"页面数超过上限，本次只把 {len(index.pages)} 个页面交给模型，"
                f"另有 {index.omitted_pages} 个页面未参与定位。"
            ),
        )
    ]


def _assemble(
    raw: dict[str, object],
    features: list[DecomposedFeature],
    index: PageIndex,
    chains: ChainIndex,
    graph: IRGraph,
    calls: int,
) -> LocateOutcome:
    outcome = LocateOutcome(calls_used=calls)
    model_unmatched = _model_unmatched(raw)

    feature_ids = {feature.id for feature in features}
    raw_matches = raw.get("matches")
    if isinstance(raw_matches, list):
        for entry in raw_matches:
            if not isinstance(entry, dict):
                outcome.dropped.append(Dropped(reason=Reason.invalid_shape))
                continue
            _validate(entry, feature_ids, index, chains, graph, outcome)
    else:
        outcome.dropped.append(Dropped(reason=Reason.invalid_shape))

    matched_ids = {match.featureId for match in outcome.matches}
    outcome.unmatched = [
        Unmatched(
            featureId=feature.id,
            reason=model_unmatched.get(feature.id) or "模型未给出该功能点的匹配。",
            candidates=_top_candidates(index),
        )
        for feature in features
        if feature.id not in matched_ids
    ]
    outcome.unmatched.sort(key=lambda item: item.featureId)
    outcome.matches.sort(key=lambda item: (item.featureId, -item.confidence, item.pageNodeId))
    outcome.dropped.sort(
        key=lambda item: (item.reason.value, item.featureId or "", item.nodeId or "")
    )
    outcome.chain_ids = list(
        dict.fromkeys(match.chainId for match in outcome.matches if match.chainId)
    )
    return outcome


def _model_unmatched(raw: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    entries = raw.get("unmatched")
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        feature_id = entry.get("featureId")
        reason = entry.get("reason")
        if isinstance(feature_id, str) and isinstance(reason, str) and reason.strip():
            result[feature_id] = reason
    return result


def _top_candidates(index: PageIndex, limit: int = 3) -> list[Candidate]:
    """未命中时附几个页面，供人判断是需求超纲还是索引不足。"""
    ordered = sorted(index.pages, key=lambda entry: (-entry.lexical_score, entry.node_id))
    return [
        Candidate(
            nodeId=entry.node_id,
            name=entry.name,
            file=entry.file,
            score=round(entry.lexical_score, 4),
            signals=[
                Signal(
                    kind=SignalKind.componentName,
                    value=entry.name,
                    score=round(entry.lexical_score, 4),
                )
            ]
            if entry.lexical_score > 0
            else [],
        )
        for entry in ordered[:limit]
    ]


def _validate(
    entry: dict[str, object],
    feature_ids: set[str],
    index: PageIndex,
    chains: ChainIndex,
    graph: IRGraph,
    outcome: LocateOutcome,
) -> None:
    feature_id = entry.get("featureId")
    page_id = entry.get("pageNodeId")
    chain_id = entry.get("chainId")
    quote = entry.get("quote")
    confidence = entry.get("confidence")
    reason = entry.get("reason")

    def reject(code: Reason) -> None:
        outcome.dropped.append(
            Dropped(
                reason=code,
                featureId=feature_id if isinstance(feature_id, str) else None,
                nodeId=page_id if isinstance(page_id, str) else None,
                chainId=chain_id if isinstance(chain_id, str) else None,
                quote=quote if isinstance(quote, str) else None,
            )
        )

    if not isinstance(feature_id, str) or not isinstance(page_id, str):
        reject(Reason.invalid_shape)
        return
    if chain_id is not None and not isinstance(chain_id, str):
        reject(Reason.invalid_shape)
        return
    if feature_id not in feature_ids:
        reject(Reason.unknown_feature)
        return
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        reject(Reason.invalid_shape)
        return

    page = index.by_id.get(page_id)
    if page is None:
        reject(Reason.node_not_in_candidates)
        return

    chain: ActionChain | None = None
    if chain_id is not None:
        chain = chains.by_id.get(chain_id)
        if chain is None or chain.page_id != page_id:
            # 链路存在但不属于所选页面同样拒：否则「页面 A + 页面 B 的链路」
            # 这种自相矛盾的组合会以一条合法 match 的形态落库。
            reject(Reason.chain_not_in_candidates)
            return

    if not isinstance(quote, str) or not quote.strip():
        reject(Reason.quote_not_found)
        return

    quote_match = match_quote(quote, index.corpus_of(chain_id, page_id, chains))
    if quote_match is None:
        reject(Reason.quote_not_found)
        return

    if quote_match.partial:
        # 子串命中是放宽而不是等价：模型确实指对了位置，但它写的不是原文。
        # 记一条 info 让人能复核「到底引了哪一段」。
        outcome.warnings.append(
            RequirementWarning(
                code="quote_partial_match",
                severity=Severity.info,
                message=(
                    f"模型引文为子串，已按语料中的原文记录：\n"
                    f"引文：{quote}\n原文：{quote_match.corpus_text}"
                ),
                featureId=feature_id,
            )
        )

    shared = _pages_sharing(quote_match.corpus_text, page_id, index, chains)
    if shared:
        outcome.warnings.append(
            RequirementWarning(
                code="quote_not_discriminative",
                severity=Severity.warning,
                message=(
                    f"引文「{quote_match.corpus_text}」同样出现在其他页面里，"
                    f"不能单独证明页面归属：{', '.join(shared)}"
                ),
                featureId=feature_id,
            )
        )

    model_confidence = float(confidence)
    route_id = _route_id_of(graph, page_id)
    outcome.matches.append(
        Match(
            featureId=feature_id,
            pageNodeId=page_id,
            chainId=chain_id,
            targets=chains.targets_of(chain_id, quote_match.corpus_text) if chain_id else None,
            routeId=route_id,
            confidence=min(model_confidence, MAX_CONFIDENCE),
            modelConfidence=model_confidence,
            quote=quote_match.corpus_text,
            reason=reason if isinstance(reason, str) and reason.strip() else None,
            subgraph=build_subgraph(graph, page_id, route_id),
            verifiedEvidence=_verified_evidence(quote_match, chain, page, chains, graph),
            source=Source.ai,
        )
    )


def _route_id_of(graph: IRGraph, page_id: str) -> str | None:
    nodes = {node.id: node for node in graph.nodes}
    for edge in graph.edges:
        if edge.kind is not EdgeKind.renders or edge.to != page_id:
            continue
        source = nodes.get(edge.from_)
        if source is not None and source.kind is NodeKind.route:
            return source.id
    return None


def _pages_sharing(
    corpus_text: str,
    page_id: str,
    index: PageIndex,
    chains: ChainIndex,
) -> list[str]:
    return sorted(
        entry.node_id
        for entry in index.pages
        if entry.node_id != page_id and corpus_text in index.corpus_of(None, entry.node_id, chains)
    )


def _verified_evidence(
    match: QuoteMatch,
    chain: ActionChain | None,
    page: PageEntry,
    chains: ChainIndex,
    graph: IRGraph,
) -> list[VerifiedEvidence] | None:
    """把已通过校验的引文落成可复核坐标。

    有链路时优先取链路里命中的那一段（每段都有 file+loc）；页面级匹配退回页面
    证据里同一段原文的位置。坐标永远取自 IR，不取模型输出。
    """
    nodes = {node.id: node for node in graph.nodes}
    if chain is not None:
        targets = chains.targets_of(chain.id, match.corpus_text)
        evidence = [
            item
            for node_id in targets
            if (item := _step_evidence(nodes.get(node_id), match.corpus_text)) is not None
        ]
        if evidence:
            return evidence

    matched = [item for item in page.evidence if item.text == match.corpus_text]
    if matched:
        return [_recall_evidence(item) for item in _rank_evidence(matched)]

    node = nodes.get(page.node_id)
    item = _step_evidence(node, match.corpus_text)
    return [item] if item is not None else None


def _rank_evidence(items: Sequence[RecallEvidence]) -> list[RecallEvidence]:
    return sorted(
        items,
        key=lambda item: (
            -item.confidence,
            item.kind.value,
            item.node_id,
            item.file or "",
            item.loc.line if item.loc else 0,
            item.loc.col if item.loc else 0,
        ),
    )


def _recall_evidence(item: RecallEvidence) -> VerifiedEvidence:
    return VerifiedEvidence(
        nodeId=item.node_id,
        quote=item.text,
        file=item.file,
        loc=_to_location(item.loc),
        via=item.kind.value,
        source=item.source,
        confidence=item.confidence,
    )


def _to_location(loc: object) -> Location | None:
    """IR 的 Location 与 requirement 的 Location 是两个契约类型，必须显式转换。

    pydantic 不会跨模型自动构造（`loc` 字段拿到一个 IR Location 实例会直接报错），
    而两者的字段恰好同名，所以漏转会以「类型校验失败」的形态在运行时才炸。
    """
    if loc is None:
        return None
    line = getattr(loc, "line", None)
    col = getattr(loc, "col", None)
    if not isinstance(line, int) or not isinstance(col, int):
        return None
    return Location(line=line, col=col)


def _step_evidence(node: IRNode | None, quote: str) -> VerifiedEvidence | None:
    if node is None:
        return None
    loc = node.loc
    file = node.file
    source = Source.static
    confidence = 1.0
    if node.evidence:
        best = sorted(
            node.evidence,
            key=lambda item: (-item.confidence, item.file or "", item.loc.line if item.loc else 0),
        )[0]
        loc = loc or best.loc
        file = file or best.file
        source = best.source
        confidence = best.confidence
    return VerifiedEvidence(
        nodeId=node.id,
        quote=quote,
        file=file,
        loc=_to_location(loc),
        via=_via_of(node),
        source=source,
        confidence=confidence,
    )


def _via_of(node: IRNode) -> str:
    if node.kind is NodeKind.element:
        return SignalKind.uiText.value
    if node.kind is NodeKind.api:
        return SignalKind.apiUrl.value
    return node.kind.value


def chain_contracts(chains: ChainIndex, chain_ids: Sequence[str]) -> list[RequirementChain]:
    """命中的链路转成 `RequirementMap.interactionChains` 里的类型。

    ## 为什么要显式转换一次

    `interactionChains` 用 `$ref` 引用了 `chain.schema.json#/$defs/ActionChain`，
    但 datamodel-codegen 对跨文件 `$ref` 是**内联**的：`requirement.py` 里有一份
    `ActionChain` 副本，`chain.py` 里还有原件。两者字段相同、类不同，pydantic
    不做跨模型隐式构造，直接把 `chain.ActionChain` 塞进去会在运行时炸。

    这里按 dump→validate 转换而不是手写字段映射：字段加一个就漏一个的隐患，
    比一次序列化的开销更贵。
    """
    return [
        RequirementChain.model_validate(contract.model_dump())
        for contract in chains.contracts_for(list(chain_ids))
    ]


def merge_lexical_candidates(*results: RecallResult | None) -> list[CandidateDetail]:
    """把多个功能点的词法召回合并成一份候选集（按 nodeId 去重、取最高分）。

    词法现在是「辅助与兜底」，但它产出的 candidates 仍是产物的可见部分：
    用户要知道「代码里有哪些页面与这段需求撞了词」。这份合并必须在语义定位
    之后仍然落库，否则一次成功的 AI 定位会把词法证据整个抹掉。
    """
    merged: dict[str, CandidateDetail] = {}
    for result in results:
        if result is None:
            continue
        for detail in result.candidates:
            existing = merged.get(detail.candidate.nodeId)
            if existing is None or detail.candidate.score > existing.candidate.score:
                merged[detail.candidate.nodeId] = detail
    return sorted(merged.values(), key=lambda item: (-item.candidate.score, item.candidate.nodeId))


def semantic_signals(candidates: list[CandidateDetail], matches: list[Match]) -> None:
    """把「AI 也选了这些页面」写回候选信号。

    只追加，不改分：候选的分数仍是词法的可解释得分，semanticRank 只是标注。
    前端据此区分「词法召回的」与「AI 额外认定的」——也据此发现「AI 选了但词法
    一条都没召回」的页面，那正是词表方案的盲区。
    """
    ranks: dict[str, int] = {}
    for match in matches:
        ranks.setdefault(match.pageNodeId, len(ranks) + 1)
    for detail in candidates:
        rank = ranks.get(detail.candidate.nodeId)
        if rank is None:
            continue
        if any(signal.kind is SignalKind.semanticRank for signal in detail.candidate.signals):
            continue
        detail.candidate.signals.append(
            Signal(kind=SignalKind.semanticRank, value=f"rank={rank}", score=0.0)
        )


__all__ = [
    "ChainBrief",
    "LocateOutcome",
    "MAX_CHAINS_PER_PAGE_IN_PROMPT",
    "PageEntry",
    "PageIndex",
    "build_page_index",
    "chain_contracts",
    "locate",
    "merge_lexical_candidates",
    "semantic_signals",
]
