"""模型判定：候选集 + 功能点 → matches / unmatched / dropped。

信任模型的原因，校验模型的产出。四条服务端校验缺一不可：
  1. featureId 必须是本次拆解出的功能点（unknown_feature）
  2. pageNodeId 必须落在候选集内（node_not_in_candidates）
  3. quote 必须能在该候选的原文语料里找到（quote_not_found）——整条相等优先，
     归一化子串兜底，见 `match_quote`
  4. 字段类型必须符合预期（invalid_shape）
未通过校验的输出进 dropped 而不是静默丢弃——审核模型是否在幻想，只能靠这份记录。

模型自报的置信度不透传：落库置信度硬上限 0.6（AI 推断永远不等于语法事实），
引文歧义（同段原文命中多个候选）时再按 0.8 系数降权——排序直接分辨强弱，
原始值记在 modelConfidence 里供比对封顶与降权是否生效。

## 本模块现在的角色

主路径是 `locate.py`（页面 + 交互链路）。本模块保留「页面级」判定能力：
图里没有任何可推导链路时（纯静态页面），页面仍是合法落点。两条路径共用
`match_quote` / `parse_json` / `MAX_CONFIDENCE` —— 校验规则只能有一份，否则
`chain_not_in_candidates` 这类新闸在一条路径上生效、另一条上悄悄缺失。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.contracts import IRGraph
from app.contracts.requirement import (
    Candidate,
    Dropped,
    Location,
    Match,
    Reason,
    RequirementWarning,
    Severity,
    Source,
    Unmatched,
    VerifiedEvidence,
)
from app.requirement.features import DecomposedFeature
from app.requirement.llm import LLMCallError, LLMClient
from app.requirement.recall import CandidateDetail
from app.requirement.subgraph import build_subgraph
from app.requirement.text import normalize

# 未匹配功能点附带的召回候选数：足够判断「需求超纲」还是「召回不足」
_UNMATCHED_CANDIDATES = 3

# 落库置信度硬上限，与契约约定一致
MAX_CONFIDENCE = 0.6

# 引文歧义的降权系数：同段原文命中多个候选时，引文只能证明「这句话存在」，
# 不能证明归属。乘 0.8 把这类匹配压到同置信度的非歧义匹配之下，审核者看排序
# 就能分辨强弱，不必逐条读 quote_not_discriminative 告警（差距表 #24）。
AMBIGUOUS_QUOTE_SCALE = 0.8

# 子串兜底的最小长度。更短的片段（「列表」「状态」「的」）能命中几乎一切语料，
# 这样的"校验"等于不校验。
MIN_SUBSTRING_QUOTE = 4

_SYSTEM_PROMPT = """\
你是 React 项目的需求定位助手。输入是一段需求拆解出的功能点，和一份「页面候选」\
清单（来自代码图谱的确定性召回，每个候选带路由、文件、文案、props、接口等原文证据）。

你的任务：判断每个功能点由哪个（或哪些）页面实现。

规则：
1. pageNodeId 只能从候选清单里选，禁止发明候选外的节点。
2. quote 是你判断依据的原文，必须从该候选的 signals/uiTexts/propNames/\
propDescriptions/apiUrls/name 里复制：优先整条复制；确实需要截取时，截取的片段\
至少 4 个字、且在该候选里只对应一段原文。服务端会做校验，复制不到的匹配会被\
整条丢弃。summary（如果有）是页面用途的一句话描述，只能用来理解页面，不能作为 quote。
3. confidence 为 0 到 1 之间的小数，表示你对该匹配的把握，宁可保守。
4. featureId 必须来自输入的功能点清单。
5. 需求超纲、或候选里找不到能实现该功能点的页面时，把 featureId 放进 unmatched \
并说明理由。
6. 只输出 JSON，不要输出任何其他文字。格式：
{"matches": [{"featureId": "f1", "pageNodeId": "...", "quote": "...", \
"confidence": 0.8, "reason": "一句话理由"}], "unmatched": [{"featureId": "f2", \
"reason": "一句话理由"}]}
"""

_CODE_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


@dataclass
class JudgeOutcome:
    matches: list[Match] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)
    warnings: list[RequirementWarning] = field(default_factory=list)
    calls_used: int = 0


async def judge(
    features: list[DecomposedFeature],
    candidates: list[CandidateDetail],
    graph: IRGraph,
    llm: LLMClient,
    *,
    requirement: str,
    max_calls: int,
    summaries: dict[str, str] | None = None,
) -> JudgeOutcome:
    if not candidates:
        # 召回已经失败（no_page_candidates 等），不必烧一次模型调用
        return JudgeOutcome(
            unmatched=[
                Unmatched(featureId=feature.id, reason="图中没有可定位的页面候选。")
                for feature in features
            ],
            warnings=[
                RequirementWarning(
                    code="judge_skipped",
                    severity=Severity.warning,
                    message="候选集为空，跳过模型判定，全部功能点记为未匹配。",
                )
            ],
        )

    candidate_by_id = {detail.candidate.nodeId: detail for detail in candidates}
    user_prompt = _build_user_prompt(requirement, features, candidates, summaries)

    raw: dict[str, object] | None = None
    last_error: str | None = None
    calls = 0
    for _ in range(max_calls):
        calls += 1
        prompt = user_prompt if last_error is None else (
            f"{user_prompt}\n\n上一次输出无效（{last_error}），请严格按规则重新输出。"
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
        # 预算耗尽仍拿不到有效输出：降级为确定性结果而不是把任务打成失败。
        # candidates 仍然有效（召回独立于模型），只是少了 matches。
        return JudgeOutcome(
            calls_used=calls,
            unmatched=[
                Unmatched(
                    featureId=feature.id,
                    reason="模型调用失败，已降级为确定性召回结果，请查看 candidates。",
                )
                for feature in features
            ],
            warnings=[
                RequirementWarning(
                    code="llm_unavailable",
                    severity=Severity.error,
                    message=f"模型判定在 {calls} 次尝试内未产出有效结果"
                    f"（{last_error or '未知原因'}），已降级为确定性召回。",
                )
            ],
        )

    return _assemble_outcome(raw, features, candidate_by_id, graph, calls)


def _build_user_prompt(
    requirement: str,
    features: list[DecomposedFeature],
    candidates: list[CandidateDetail],
    summaries: dict[str, str] | None = None,
) -> str:
    feature_lines = "\n".join(
        f"{feature.id}: {feature.text}" for feature in features
    )
    candidate_payload = []
    for detail in candidates:
        candidate = detail.candidate
        payload: dict[str, object] = {
            "nodeId": candidate.nodeId,
            "name": candidate.name,
            "routeId": candidate.routeId,
            "file": candidate.file,
            "score": candidate.score,
            "signals": [
                {"kind": signal.kind.value, "value": signal.value}
                for signal in candidate.signals
            ],
            "uiTexts": detail.ui_texts,
            "propNames": detail.prop_names,
            "propDescriptions": detail.prop_descriptions,
            "apiUrls": detail.api_urls,
        }
        # 锚点归属路径（反向 BFS 产物）：让模型知道「这条命中的每一环」，
        # 判定从「选页面」变成「确认链路」。没有锚点的纯词法候选不带。
        if candidate.anchorNodeIds:
            payload["anchorNodeIds"] = candidate.anchorNodeIds
            payload["anchorEdgeIds"] = candidate.anchorEdgeIds or []
        # 页面画像：只帮助理解页面用途，不是引文来源（见 profile.py 的约束 1）
        summary = (summaries or {}).get(candidate.nodeId)
        if summary:
            payload["summary"] = summary
        candidate_payload.append(payload)
    candidates_json = json.dumps(candidate_payload, ensure_ascii=False, indent=2)
    return (
        f"需求：{requirement}\n\n"
        f"功能点：\n{feature_lines}\n\n"
        f"页面候选（按召回得分降序）：\n{candidates_json}"
    )


def capped_confidence(model_confidence: float, *, quote_ambiguous: bool) -> float:
    """落库置信度：硬上限 0.6；引文歧义时再按系数降权（差距表 #24）。

    降权发生在封顶之后，结果必然仍在 0.6 以内，不与「AI 推断封顶 0.6」冲突；
    模型原始值始终记在 modelConfidence 里，降权幅度可审计。
    两条判定路径（页面级 judge / 链路级 locate）共用本函数——降权规则只能有一份。
    """
    value = min(model_confidence, MAX_CONFIDENCE)
    return value * AMBIGUOUS_QUOTE_SCALE if quote_ambiguous else value


def parse_json(text: str) -> dict[str, object] | None:
    """从模型输出里提取目标 JSON；容忍代码围栏与前后杂文。"""
    stripped = _CODE_FENCE_RE.sub("", text.strip())
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _assemble_outcome(
    raw: dict[str, object],
    features: list[DecomposedFeature],
    candidate_by_id: dict[str, CandidateDetail],
    graph: IRGraph,
    calls: int,
) -> JudgeOutcome:
    outcome = JudgeOutcome(calls_used=calls)
    model_unmatched: dict[str, str] = {}

    raw_unmatched = raw.get("unmatched")
    if isinstance(raw_unmatched, list):
        for entry in raw_unmatched:
            if isinstance(entry, dict):
                feature_id = entry.get("featureId")
                reason = entry.get("reason")
                if isinstance(feature_id, str) and isinstance(reason, str) and reason.strip():
                    model_unmatched[feature_id] = reason

    raw_matches = raw.get("matches")
    if isinstance(raw_matches, list):
        for entry in raw_matches:
            if not isinstance(entry, dict):
                outcome.dropped.append(Dropped(reason=Reason.invalid_shape))
                continue
            _validate_match(
                entry,
                feature_ids={feature.id for feature in features},
                candidate_by_id=candidate_by_id,
                graph=graph,
                matches=outcome.matches,
                dropped=outcome.dropped,
                warnings=outcome.warnings,
            )
    else:
        outcome.dropped.append(Dropped(reason=Reason.invalid_shape))

    matched_ids = {match.featureId for match in outcome.matches}
    outcome.unmatched = [
        Unmatched(
            featureId=feature.id,
            reason=model_unmatched.get(feature.id) or "模型未给出该功能点的匹配。",
            candidates=_top_candidates(candidate_by_id),
        )
        for feature in features
        if feature.id not in matched_ids
    ]
    outcome.unmatched.sort(key=lambda item: item.featureId)
    outcome.matches.sort(key=lambda item: (item.featureId, -item.confidence, item.pageNodeId))
    outcome.dropped.sort(
        key=lambda item: (item.reason.value, item.featureId or "", item.nodeId or "")
    )
    return outcome


def _top_candidates(candidate_by_id: dict[str, CandidateDetail]) -> list[Candidate]:
    ordered = sorted(
        candidate_by_id.values(),
        key=lambda detail: (-detail.candidate.score, detail.candidate.nodeId),
    )
    return [detail.candidate for detail in ordered[:_UNMATCHED_CANDIDATES]]


@dataclass(frozen=True)
class QuoteMatch:
    """引文校验结果。

    命中时要带回**语料里那段原文**而不是模型写的字串：`Match.quote` 与
    `VerifiedEvidence.quote` 都必须是代码里的真实文本，否则前端点开证据看到的
    是一句代码里不存在的话。
    """

    # 语料中命中的那一整条原文
    corpus_text: str
    # True 表示模型截取了子串，而不是整条复制
    partial: bool


def match_quote(quote: str, corpus: Sequence[str]) -> QuoteMatch | None:
    """在候选语料里定位引文：整条相等优先，归一化子串兜底。

    为什么要子串兜底：企业项目里一条文案常常很长（「用户列表-状态筛选-全部」），
    模型引用其中「状态筛选」是完全合理的判断依据。只做整条相等会把它判成幻觉，
    这是系统性偏向长引文的偏差，不是随机误差。

    兜底必须成对约束，否则等于取消校验：
      - 长度下限 `MIN_SUBSTRING_QUOTE`：短词能命中几乎一切语料；
      - **唯一命中**：同一片段在语料里对上多段原文时，无法证明模型指的是哪一段，
        按「校验失败」处理（宁缺勿滥），而不是挑第一条蒙过去。
    """
    # 先用模型原样字串比：语料本身就是源码原文，不需要归一化才能命中
    for text in corpus:
        if text == quote:
            return QuoteMatch(corpus_text=text, partial=False)

    # 再按归一化后的等值比：全角/半角、空白、大小写差异不该冤枉正确引文
    normalized_quote = normalize(quote)
    if not normalized_quote:
        return None
    for text in corpus:
        if normalize(text) == normalized_quote:
            return QuoteMatch(corpus_text=text, partial=False)

    if len(normalized_quote) < MIN_SUBSTRING_QUOTE:
        return None

    hits = [text for text in corpus if normalized_quote in normalize(text)]
    # 去重后再判唯一：语料里同一句话出现两次不算歧义
    distinct = list(dict.fromkeys(hits))
    if len(distinct) != 1:
        return None

    return QuoteMatch(corpus_text=distinct[0], partial=True)


def _candidates_sharing(
    corpus_text: str,
    node_id: str,
    candidate_by_id: dict[str, CandidateDetail],
) -> list[str]:
    """除命中候选外，语料里也含这段原文的候选。

    多页面共用一句文案（「暂无数据」「加载中」）是常态：此时引文命中只能证明
    「这句话在这堆候选里存在过」，不能证明归属。这种弱化必须让人看见——否则
    一条本该含糊的匹配会以和其他匹配完全相同的面貌出现在审核界面上。
    """
    return sorted(
        other.candidate.nodeId
        for other in candidate_by_id.values()
        if other.candidate.nodeId != node_id and corpus_text in other.quote_corpus
    )


def _verified_evidence(
    detail: CandidateDetail,
    match: QuoteMatch,
) -> list[VerifiedEvidence]:
    """按已通过语料校验的引文查找后端保存的结构化来源。

    子串命中时一条原文可能对应多条结构化证据（同一段文案挂在多个节点上），
    因此按 `corpus_text` 相等匹配，而不是按模型给的片段匹配。
    """
    matching = [item for item in detail.evidence if item.text == match.corpus_text]
    matching.sort(
        key=lambda item: (
            -item.confidence,
            item.kind.value,
            item.node_id,
            item.file or "",
            item.loc.line if item.loc else 0,
            item.loc.col if item.loc else 0,
        )
    )
    return [
        VerifiedEvidence(
            nodeId=item.node_id,
            quote=item.text,
            file=item.file,
            loc=(
                Location(line=item.loc.line, col=item.loc.col)
                if item.loc is not None
                else None
            ),
            via=item.kind.value,
            source=item.source.value,
            confidence=item.confidence,
        )
        for item in matching
    ]


def _validate_match(
    entry: dict[str, object],
    *,
    feature_ids: set[str],
    candidate_by_id: dict[str, CandidateDetail],
    graph: IRGraph,
    matches: list[Match],
    dropped: list[Dropped],
    warnings: list[RequirementWarning],
) -> None:
    feature_id = entry.get("featureId")
    node_id = entry.get("pageNodeId")
    quote = entry.get("quote")
    confidence = entry.get("confidence")
    reason = entry.get("reason")

    def reject(reason_code: Reason) -> None:
        dropped.append(
            Dropped(
                reason=reason_code,
                featureId=feature_id if isinstance(feature_id, str) else None,
                nodeId=node_id if isinstance(node_id, str) else None,
                quote=quote if isinstance(quote, str) else None,
            )
        )

    if not isinstance(feature_id, str) or not isinstance(node_id, str):
        reject(Reason.invalid_shape)
        return
    if feature_id not in feature_ids:
        reject(Reason.unknown_feature)
        return
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        reject(Reason.invalid_shape)
        return
    if not isinstance(quote, str) or not quote.strip():
        # quote 是唯一的可复核依据：缺失视同引文校验失败
        reject(Reason.quote_not_found)
        return
    detail = candidate_by_id.get(node_id)
    if detail is None:
        reject(Reason.node_not_in_candidates)
        return

    quote_match = match_quote(quote, detail.quote_corpus)
    if quote_match is None:
        reject(Reason.quote_not_found)
        return

    if quote_match.partial:
        # 子串命中是放宽而不是等价：模型确实指对了位置，但它写的不是原文。
        # 记一条 info 让人能复核「到底引了哪一段」，而不是把它当整条复制放过。
        warnings.append(
            RequirementWarning(
                code="quote_partial_match",
                severity=Severity.info,
                message=(
                    f"模型引文为子串，已按候选语料中的原文记录：\n"
                    f"引文：{quote}\n原文：{quote_match.corpus_text}"
                ),
                featureId=feature_id,
            )
        )

    # 同一段原文也出现在别的候选里时，引文无法证明归属（「暂无数据」这类共通文案
    # 尤其常见）。匹配仍然保留——模型还有 reason 与置信度——但必须标注出来，
    # 否则审核者会以为这条和其他匹配一样确定。
    shared_with = _candidates_sharing(quote_match.corpus_text, node_id, candidate_by_id)
    if shared_with:
        warnings.append(
            RequirementWarning(
                code="quote_not_discriminative",
                severity=Severity.warning,
                message=(
                    f"引文「{quote_match.corpus_text}」同样出现在其他候选里，"
                    f"不能单独证明页面归属：{', '.join(shared_with)}；"
                    f"已按 {AMBIGUOUS_QUOTE_SCALE} 系数对该匹配降权"
                ),
                featureId=feature_id,
            )
        )

    verified_evidence = _verified_evidence(detail, quote_match)
    if not verified_evidence:
        warnings.append(
            RequirementWarning(
                code="verified_evidence_unresolved",
                severity=Severity.warning,
                message=f"引文已通过语料校验，但无法解析到结构化证据位置：{quote_match.corpus_text}",
                featureId=feature_id,
            )
        )

    model_confidence = float(confidence)
    matches.append(
        Match(
            featureId=feature_id,
            pageNodeId=node_id,
            routeId=detail.candidate.routeId,
            confidence=capped_confidence(model_confidence, quote_ambiguous=bool(shared_with)),
            modelConfidence=model_confidence,
            # 落库的是语料原文，不是模型写的片段：证据必须能在代码里原样找到
            quote=quote_match.corpus_text,
            reason=reason if isinstance(reason, str) and reason.strip() else None,
            subgraph=build_subgraph(graph, node_id, detail.candidate.routeId),
            verifiedEvidence=verified_evidence,
            source=Source.ai,
        )
    )
