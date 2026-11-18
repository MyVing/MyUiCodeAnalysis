"""需求拆解：把一段需求文本切成「功能点」。

两种模式共用同一个返回类型：
  - 确定性模式（本模块）：按行、编号、分句切开。用于 dry-run 与模型不可用时的降级，
    也可作为模型拆解的兜底——切错了顶多是候选变粗，不会丢功能点。
  - 模型模式（阶段 3）：模型只产出文本与关键词，禁止出现 nodeId / 文件名。

确定性模式刻意不追求「理解」：它的价值是可解释、稳定、零依赖。真正需要语义时，
模型给出的功能点会替换这里的结果，但降级路径必须一直可用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.contracts import RequirementWarning
from app.contracts.requirement import Severity
from app.requirement.lexicon import RequirementTerms, expand

# 单个功能点过长时截断：功能点要进 prompt，长度不受控会让上下文失控
MAX_FEATURE_CHARS = 200
DEFAULT_MAX_FEATURES = 8
# 太短的片段基本是「等等」「补充」这类残句，进候选集只会增加噪声
_MIN_FEATURE_CHARS = 3

# 行首的编号 / 项目符号：`1.` `1、` `(1)` `-` `*` `•`
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•+]|\(?\d+[.)、．]|[（(]\d+[）)])\s*")
# 句末标点：中文全角与英文半角都认
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])|\n+")


@dataclass(frozen=True)
class DecomposedFeature:
    """一个功能点及其检索词。"""

    id: str
    text: str
    terms: RequirementTerms

    def keywords(self) -> list[str]:
        """写进 RequirementMap 的关键词：去掉兜底 bigram，只留可解释的检索词。"""
        from app.requirement.lexicon import BIGRAM_TERM_WEIGHT

        return [term.surface for term in self.terms.terms if term.weight > BIGRAM_TERM_WEIGHT][
            :24
        ]


@dataclass(frozen=True)
class DecomposeResult:
    """功能点 + 本次拆解中被丢掉的信息。

    截断不是「拆干净了」——被挤掉的功能点既不会出现在产物里，也不会被用户看见。
    把丢了多少如实带出来，调用方才能把它写进 `RequirementMap.warnings`。
    """

    features: list[DecomposedFeature] = field(default_factory=list)
    warnings: list[RequirementWarning] = field(default_factory=list)


def decompose_with_report(
    requirement: str, *, max_features: int = DEFAULT_MAX_FEATURES
) -> DecomposeResult:
    """拆解功能点，并如实报告截断。

    与 `decompose` 的唯一区别是返回值带告警。两条截断路径都要报：
      - 功能点数量超过 `max_features`：被挤掉的诉求彻底不进候选集；
      - 单个功能点超过 `MAX_FEATURE_CHARS`：功能点后半段的词不再参与召回。
    两者都不改变「已产出的功能点仍然可用」这个事实，所以是 info 而非 warning。
    """
    segments = _split(requirement)
    warnings: list[RequirementWarning] = []
    features: list[DecomposedFeature] = []

    for segment in segments:
        if len(features) >= max_features:
            dropped = len(segments) - max_features
            warnings.append(
                RequirementWarning(
                    code="feature_limit_reached",
                    severity=Severity.info,
                    message=(
                        f"需求切出 {len(segments)} 个功能点，超过上限 {max_features} 个，"
                        f"后 {dropped} 个未参与定位；如需覆盖请分批提交或调大上限"
                    ),
                )
            )
            break

        text = segment[:MAX_FEATURE_CHARS]
        feature = DecomposedFeature(
            id=f"f{len(features) + 1}",
            text=text,
            terms=expand(text),
        )
        if len(segment) > MAX_FEATURE_CHARS:
            warnings.append(
                RequirementWarning(
                    code="feature_truncated",
                    severity=Severity.info,
                    message=(
                        f"功能点 {feature.id} 超过 {MAX_FEATURE_CHARS} 字，"
                        f"超出部分（{len(segment) - MAX_FEATURE_CHARS} 字）未参与检索"
                    ),
                    featureId=feature.id,
                )
            )
        features.append(feature)

    if features:
        return DecomposeResult(features=features, warnings=warnings)

    whole = requirement.strip()[:MAX_FEATURE_CHARS]
    if not whole:
        return DecomposeResult(warnings=warnings)
    return DecomposeResult(
        features=[DecomposedFeature(id="f1", text=whole, terms=expand(whole))],
        warnings=warnings,
    )


def decompose(
    requirement: str, *, max_features: int = DEFAULT_MAX_FEATURES
) -> list[DecomposedFeature]:
    """把需求文本切成功能点。

    切不出可用片段时退化为「整段需求算一个功能点」——宁可粗，不可空：
    空功能点会让整个定位流程直接失败，而粗功能点至少还能召回候选。

    只返回功能点、不带告警，是给「不关心截断」的调用方（测试、单测辅助）留的
    窄口。需要告警请用 `decompose_with_report`：截断信息不能靠调用方自己
    再数一遍才知道。
    """
    return decompose_with_report(requirement, max_features=max_features).features


def _split(requirement: str) -> list[str]:
    segments: list[str] = []
    for raw_line in _SENTENCE_SPLIT_RE.split(requirement):
        if raw_line is None:
            continue
        for part in _split_long_line(_BULLET_PREFIX_RE.sub("", raw_line.strip())):
            cleaned = part.strip(" \t，,、：:。.")
            if len(cleaned) < _MIN_FEATURE_CHARS:
                continue
            segments.append(cleaned)
    return _dedupe(segments)


def _split_long_line(line: str) -> list[str]:
    """一行里塞了多个诉求时按逗号再切一刀。

    只在片段够长时才切：`姓名，邮箱` 这种并列项单独成功能点没有意义，
    反而让每个功能点都召回同一批候选。
    """
    if len(line) < 40:
        return [line]
    parts = re.split(r"[，,]", line)
    if any(len(part.strip()) < _MIN_FEATURE_CHARS for part in parts):
        return [line]
    return parts


def _dedupe(segments: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for segment in segments:
        if segment in seen:
            continue
        seen.add(segment)
        result.append(segment)
    return result
