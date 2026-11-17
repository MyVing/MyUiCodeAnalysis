"""模型判定：候选集 + 功能点 → matches / unmatched / dropped。

信任模型的原因，校验模型的产出。四条服务端校验缺一不可：
  1. featureId 必须是本次拆解出的功能点（unknown_feature）
  2. pageNodeId 必须落在候选集内（node_not_in_candidates）
  3. quote 必须能在该候选的原文语料里逐字找到（quote_not_found）
  4. 字段类型必须符合预期（invalid_shape）
未通过校验的输出进 dropped 而不是静默丢弃——审核模型是否在幻想，只能靠这份记录。

模型自报的置信度不透传：落库置信度硬上限 0.6（AI 推断永远不等于语法事实），
原始值记在 modelConfidence 里供比对封顶是否生效。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.contracts import IRGraph
from app.contracts.requirement import (
    Candidate,
    Dropped,
    Match,
    Reason,
    RequirementWarning,
    Severity,
    Source,
    Unmatched,
)
from app.requirement.features import DecomposedFeature
from app.requirement.llm import LLMCallError, LLMClient
from app.requirement.recall import CandidateDetail
from app.requirement.subgraph import build_subgraph

# 未匹配功能点附带的召回候选数：足够判断「需求超纲」还是「召回不足」
_UNMATCHED_CANDIDATES = 3

# 落库置信度硬上限，与契约约定一致
_MAX_CONFIDENCE = 0.6

_SYSTEM_PROMPT = """\
你是 React 项目的需求定位助手。输入是一段需求拆解出的功能点，和一份「页面候选」\
清单（来自代码图谱的确定性召回，每个候选带路由、文件、文案、props、接口等原文证据）。

你的任务：判断每个功能点由哪个（或哪些）页面实现。

规则：
1. pageNodeId 只能从候选清单里选，禁止发明候选外的节点。
2. quote 是你判断依据的原文，必须从该候选的 signals/uiTexts/propNames/\
propDescriptions/apiUrls/name 里逐字复制；服务端会做逐字校验，复制不到的匹配会被\
整条丢弃。
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
    user_prompt = _build_user_prompt(requirement, features, candidates)

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
            continue
        parsed = _parse_json(text)
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
                    message=f"模型判定在 {max_calls} 次尝试内未产出有效结果"
                    f"（{last_error or '未知原因'}），已降级为确定性召回。",
                )
            ],
        )

    return _assemble_outcome(raw, features, candidate_by_id, graph, calls)


def _build_user_prompt(
    requirement: str,
    features: list[DecomposedFeature],
    candidates: list[CandidateDetail],
) -> str:
    feature_lines = "\n".join(
        f"{feature.id}: {feature.text}" for feature in features
    )
    candidate_payload = []
    for detail in candidates:
        candidate = detail.candidate
        candidate_payload.append(
            {
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
        )
    candidates_json = json.dumps(candidate_payload, ensure_ascii=False, indent=2)
    return (
        f"需求：{requirement}\n\n"
        f"功能点：\n{feature_lines}\n\n"
        f"页面候选（按召回得分降序）：\n{candidates_json}"
    )


def _parse_json(text: str) -> dict[str, object] | None:
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


def _validate_match(
    entry: dict[str, object],
    *,
    feature_ids: set[str],
    candidate_by_id: dict[str, CandidateDetail],
    graph: IRGraph,
    matches: list[Match],
    dropped: list[Dropped],
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
    if quote not in detail.quote_corpus:
        reject(Reason.quote_not_found)
        return

    model_confidence = float(confidence)
    matches.append(
        Match(
            featureId=feature_id,
            pageNodeId=node_id,
            routeId=detail.candidate.routeId,
            confidence=min(model_confidence, _MAX_CONFIDENCE),
            modelConfidence=model_confidence,
            quote=quote,
            reason=reason if isinstance(reason, str) and reason.strip() else None,
            subgraph=build_subgraph(graph, node_id, detail.candidate.routeId),
            source=Source.ai,
        )
    )
