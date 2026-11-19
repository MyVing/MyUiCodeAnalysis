"""LLM 推断章节：2.x.3 业务操作步骤 / 2.x.4.3 前端业务规则 / 2.x.4.4 字段映射。

与 requirement.judge 同一条纪律：模型只产出「文字结论 + 引文」，引文必须
逐字命中页面语料，命中失败进 dropped（审计可见，不静默丢弃）；AI 证据
置信度封顶 0.6。三个章节各一次独立调用——每次调用聚焦单一章节，prompt
短、校验形状单一，一次输出失效只重试本章节。

页面归属是服务端算的，不让模型猜：steps / rules 按 featureId 反查它的
定位页面；字段映射按 api url 反查。模型给不了 featureId 的规则按引文
在全部页面语料里定位（唯一命中才收）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.contracts.requirement import (
    Dropped,
    Feature,
    Reason,
    RequirementMap,
    RequirementWarning,
    Severity,
)
from app.contracts.specdoc import (
    FieldMapping,
    Location,
    Rule,
    RuleCategory,
    Source,
    Step,
    VerifiedEvidence,
)
from app.requirement.judge import capped_confidence, match_quote, parse_json
from app.requirement.llm import LLMCallError, LLMClient
from app.specdoc.apimap import all_api_steps
from app.specdoc.collect import Materials, PageMaterial

# 每页语料送入 prompt 的行数上限：语料过长会把输出预算挤占掉，模型开始
# 省略引文。超出部分不送——确定性章节覆盖要素事实，LLM 章节允许按主要
# 交互推断，漏掉的由告警说明。
_MAX_CORPUS_LINES = 120

_STEPS_SYSTEM = """\
你是资深前端规格分析师。根据给定的需求、功能点与页面语料（均为代码原文），\
为每个功能点产出「业务操作步骤」。

只输出 JSON，形状：
{"steps": [{"featureId": "f1", "precondition": ["前置条件"],
  "actions": ["操作1", "操作2"], "quote": "引文", "confidence": 0.8}]}

要求：
1. 每个功能点恰好一组步骤；featureId 必须使用给定的 id，不得编造。
2. actions 是有序的用户操作，每条一句话，写到「系统给出什么反馈」为止
   （例：选择付款账户，系统展示该账户可用余额）。
3. quote 必须从对应页面的语料里逐字复制，作为该组步骤的判断依据；
   禁止改写、拼凑或使用语料里没有的文本。
4. 禁止编造页面、按钮、接口。语料不足以支撑某个功能点的步骤时，
   省略该功能点，不要编。
"""

_RULES_SYSTEM = """\
你是资深前端规格分析师。根据给定的需求、功能点与页面语料（均为代码原文），\
归纳页面上的前端业务规则，按五类输出：validation（交互校验）/ display（展示规则）\
/ linkage（联动规则）/ session（会话状态）/ permission（权限规则）。

只输出 JSON，形状：
{"rules": [{"featureId": "f1", "category": "validation", "target": "作用对象",
  "condition": "条件", "result": "结果", "quote": "引文", "confidence": 0.8}]}

要求：
1. category 必须是五类之一；featureId 用给定 id，页面级规则可省略。
2. condition 写「什么情况下」，result 写「页面/系统做什么」。
3. quote 必须从对应页面的语料里逐字复制，禁止改写或拼凑。
4. 禁止编造规则。语料里没有依据的规则不要输出。
"""

_MAPPING_SYSTEM = """\
你是资深前端规格分析师。根据给定的页面语料（均为代码原文），归纳\
「前端字段 → 后端参数」的映射。

只输出 JSON，形状：
{"fieldMappings": [{"frontField": "前端字段名", "backendParam": "后端参数名",
  "api": "接口", "quote": "引文", "confidence": 0.8}]}

要求：
1. 只归纳语料里有依据的映射（请求体字段名、接口 url、参数名）。
2. api 用「METHOD url」或 url。
3. quote 必须从对应页面的语料里逐字复制，禁止改写或拼凑。
4. 语料没有依据的映射不要输出。
"""


@dataclass(frozen=True)
class _PageContext:
    """送入 prompt 与引文校验的页面级上下文。"""

    material: PageMaterial
    # 引文命中的原文 → 证据位置（node/file/loc），只覆盖已进语料的文本
    evidence_by_text: dict[str, VerifiedEvidence] = field(default_factory=dict)
    # 该页面的接口集合（method, url），字段映射按 api 反查页面用
    api_urls: tuple[tuple[str, str], ...] = ()

    @property
    def corpus(self) -> tuple[str, ...]:
        return self.material.corpus


@dataclass
class InferOutcome:
    """三章节推断结果 + 校验副产物。"""

    steps: list[Step] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    field_mappings: list[FieldMapping] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    warnings: list[RequirementWarning] = field(default_factory=list)


async def infer_sections(
    requirement: str,
    requirement_map: RequirementMap,
    materials: Materials,
    llm: LLMClient,
    max_calls: int,
) -> InferOutcome:
    """跑三个推断章节。模型彻底不可用时不抛错：章节留空 + llm_unavailable 告警。"""
    contexts = _page_contexts(materials)
    feature_pages = _feature_pages(requirement_map, contexts)
    features = list(requirement_map.features)

    outcome = InferOutcome()
    steps, dropped, warnings = await _infer_steps(
        requirement, features, feature_pages, contexts, llm, max_calls
    )
    outcome.steps, outcome.dropped, outcome.warnings = steps, dropped, warnings
    rules, dropped, warnings = await _infer_rules(
        requirement, features, feature_pages, contexts, llm, max_calls
    )
    outcome.rules.extend(rules)
    outcome.dropped.extend(dropped)
    outcome.warnings.extend(warnings)
    mappings, dropped, warnings = await _infer_mappings(
        requirement, contexts, llm, max_calls
    )
    outcome.field_mappings.extend(mappings)
    outcome.dropped.extend(dropped)
    outcome.warnings.extend(warnings)
    return outcome


async def _call_json(
    llm: LLMClient, system: str, user: str, max_calls: int
) -> dict[str, object] | None:
    """带重试的一次调用。不可重试错误 / 预算耗尽 / 持续输出非法 JSON → None。"""
    for _ in range(max(1, max_calls)):
        try:
            text = await llm.chat(system, user)
        except LLMCallError as error:
            if not error.retryable:
                return None
            continue
        parsed = parse_json(text)
        if parsed is not None:
            return parsed
    return None


def _page_contexts(materials: Materials) -> list[_PageContext]:
    """构造页面上下文：语料 + 引文 → 证据索引 + 接口集合。

    证据索引的 node/file/loc 来自链路各段（ChainStep 自带真实定位）：
    引文命中「handler 源码文本」时证据指向那个 handler 节点，而不是页面。
    """
    contexts: list[_PageContext] = []
    for material in materials.pages:
        evidence_by_text: dict[str, VerifiedEvidence] = {}
        # 页面级文案（页面节点的 uiText）也进证据索引：引文指回页面节点
        ui_text = material.node.attrs.get("uiText")
        if isinstance(ui_text, dict):
            for entry in ui_text.get("texts") or []:
                if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                    evidence_by_text.setdefault(
                        entry["text"],
                        _evidence_from_step(material.node.id, entry["text"], None),
                    )
        for chain in material.chains:
            step_of = {step.node_id: step for step in chain.steps}
            for node_id, texts in chain.step_corpus:
                step = step_of.get(node_id)
                for text in texts:
                    if text not in evidence_by_text:
                        evidence_by_text[text] = _evidence_from_step(node_id, text, step)
        for source in material.elements:
            view_text = source.node.attrs.get("viewText")
            if isinstance(view_text, str) and view_text.strip():
                evidence_by_text.setdefault(
                    view_text, _evidence_from_step(source.node.id, view_text, None)
                )
        contexts.append(
            _PageContext(
                material=material,
                evidence_by_text=evidence_by_text,
                api_urls=tuple(all_api_steps(material.chains)),
            )
        )
    return contexts


def _evidence_from_step(node_id: str, text: str, step) -> VerifiedEvidence:
    """AI 证据：source=ai，置信度在引文校验后按封顶规则填。"""
    return VerifiedEvidence(
        nodeId=node_id,
        quote=text,
        file=step.file if step is not None else None,
        loc=(
            Location(line=step.line, col=step.col)
            if step is not None and (step.line is not None or step.col is not None)
            else None
        ),
        via="ai_quote",
        source=Source.ai,
        confidence=0.0,  # 占位：resolve 后按 capped_confidence 覆盖
    )


def _feature_pages(
    requirement_map: RequirementMap, contexts: list[_PageContext]
) -> dict[str, str]:
    """featureId → 定位页面 id（首个命中页）。"""
    context_by_page = {ctx.material.page.pageId: ctx for ctx in contexts}
    result: dict[str, str] = {}
    for match in requirement_map.matches:
        if match.featureId in result:
            continue
        if match.pageNodeId in context_by_page:
            result[match.featureId] = match.pageNodeId
    return result


def _user_prompt(
    requirement: str,
    features: list[Feature],
    contexts: list[_PageContext],
    *,
    with_features: bool,
) -> str:
    lines: list[str] = [f"需求：{requirement}"]
    if with_features:
        lines.append("功能点：")
        lines.extend(f"- {item.id}: {item.text}" for item in features)
    lines.append("页面语料（引文只能从这里逐字复制）：")
    for context in contexts:
        page = context.material.page
        lines.append(f"## 页面 {page.pageNo}：{page.name}（{page.file or '未知文件'}）")
        corpus = context.corpus[:_MAX_CORPUS_LINES]
        lines.extend(f"- {text}" for text in corpus)
    return "\n".join(lines)


def _verified(quote: str, context: _PageContext) -> VerifiedEvidence | None:
    """引文校验 + 证据解析。命中后按 AI 封顶规则重设置信度。"""
    match = match_quote(quote, context.corpus)
    if match is None:
        return None
    evidence = context.evidence_by_text.get(match.corpus_text)
    if evidence is None:
        return None
    confidence = capped_confidence(1.0, quote_ambiguous=match.partial)
    return evidence.model_copy(update={"confidence": confidence})


async def _infer_steps(
    requirement: str,
    features: list[Feature],
    feature_pages: dict[str, str],
    contexts: list[_PageContext],
    llm: LLMClient,
    max_calls: int,
) -> tuple[list[Step], list[Dropped], list[RequirementWarning] | None]:
    if not features:
        return [], [], []
    parsed = await _call_json(
        llm,
        _STEPS_SYSTEM,
        _user_prompt(requirement, features, contexts, with_features=True),
        max_calls,
    )
    if parsed is None:
        return [], [], [_unavailable_warning("步骤推断")]
    context_by_page = {ctx.material.page.pageId: ctx for ctx in contexts}
    steps: list[Step] = []
    dropped: list[Dropped] = []
    entries = parsed.get("steps")
    if not isinstance(entries, list):
        return [], [], [_shape_warning("steps")]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        feature_id = entry.get("featureId")
        if (
            not isinstance(feature_id, str)
            or feature_id not in {item.id for item in features}
        ):
            dropped.append(
                Dropped(
                    reason=Reason.unknown_feature,
                    featureId=_str_or_none(feature_id),
                    quote=_str_or_none(entry.get("quote")),
                )
            )
            continue
        context = context_by_page.get(feature_pages.get(feature_id, ""))
        actions = _str_list(entry.get("actions"))
        if context is None or not actions:
            dropped.append(
                Dropped(
                    reason=Reason.invalid_shape,
                    featureId=feature_id,
                    quote=_str_or_none(entry.get("quote")),
                )
            )
            continue
        quote = entry.get("quote")
        evidence = _verified(quote, context) if isinstance(quote, str) else None
        if evidence is None:
            dropped.append(
                Dropped(
                    reason=Reason.quote_not_found,
                    featureId=feature_id,
                    quote=_str_or_none(quote),
                )
            )
            continue
        steps.append(
            Step(
                id=f"step.{feature_id}",
                featureId=feature_id,
                pageId=context.material.page.pageId,
                precondition=_str_list(entry.get("precondition")) or None,
                actions=actions,
                quote=evidence.quote,
                evidence=[evidence],
            )
        )
    return steps, dropped, []


async def _infer_rules(
    requirement: str,
    features: list[Feature],
    feature_pages: dict[str, str],
    contexts: list[_PageContext],
    llm: LLMClient,
    max_calls: int,
) -> tuple[list[Rule], list[Dropped], list[RequirementWarning] | None]:
    parsed = await _call_json(
        llm,
        _RULES_SYSTEM,
        _user_prompt(requirement, features, contexts, with_features=True),
        max_calls,
    )
    if parsed is None:
        return [], [], [_unavailable_warning("业务规则推断")]
    context_by_page = {ctx.material.page.pageId: ctx for ctx in contexts}
    valid_categories = {item.value for item in RuleCategory}
    rules: list[Rule] = []
    dropped: list[Dropped] = []
    entries = parsed.get("rules")
    if not isinstance(entries, list):
        return [], [], [_shape_warning("rules")]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        category = entry.get("category")
        condition = entry.get("condition")
        result = entry.get("result")
        if (
            not isinstance(category, str)
            or category not in valid_categories
            or not isinstance(condition, str)
            or not condition
            or not isinstance(result, str)
            or not result
        ):
            dropped.append(
                Dropped(reason=Reason.invalid_shape, quote=_str_or_none(entry.get("quote")))
            )
            continue
        feature_id = _str_or_none(entry.get("featureId"))
        quote = entry.get("quote")
        evidence: VerifiedEvidence | None = None
        page_id: str | None = None
        if feature_id is not None:
            if feature_id not in feature_pages:
                dropped.append(
                    Dropped(
                        reason=Reason.unknown_feature,
                        featureId=feature_id,
                        quote=_str_or_none(quote),
                    )
                )
                continue
            context = context_by_page[feature_pages[feature_id]]
            if isinstance(quote, str):
                evidence = _verified(quote, context)
            page_id = context.material.page.pageId
        else:
            # 页面级规则：引文在全部页面里唯一命中才收，避免「暂无数据」式
            # 共享文案把规则挂到错误页面上
            evidence, page_id = _locate_by_quote(quote, contexts)
        if evidence is None or page_id is None:
            dropped.append(
                Dropped(
                    reason=Reason.quote_not_found,
                    featureId=feature_id,
                    quote=_str_or_none(quote),
                )
            )
            continue
        rules.append(
            Rule(
                id="fr.pending",  # 编号在 assemble 阶段按 featureId 统一分配
                pageId=page_id,
                featureId=feature_id,
                category=RuleCategory(category),
                target=_str_or_none(entry.get("target")),
                condition=condition,
                result=result,
                quote=evidence.quote,
                evidence=[evidence],
            )
        )
    return rules, dropped, []


async def _infer_mappings(
    requirement: str,
    contexts: list[_PageContext],
    llm: LLMClient,
    max_calls: int,
) -> tuple[list[FieldMapping], list[Dropped], list[RequirementWarning] | None]:
    parsed = await _call_json(
        llm,
        _MAPPING_SYSTEM,
        _user_prompt(requirement, [], contexts, with_features=False),
        max_calls,
    )
    if parsed is None:
        return [], [], [_unavailable_warning("字段映射推断")]
    mappings: list[FieldMapping] = []
    dropped: list[Dropped] = []
    entries = parsed.get("fieldMappings")
    if not isinstance(entries, list):
        return [], [], [_shape_warning("fieldMappings")]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        front = _str_or_none(entry.get("frontField"))
        backend = _str_or_none(entry.get("backendParam"))
        api = _str_or_none(entry.get("api"))
        quote = _str_or_none(entry.get("quote"))
        if front is None or backend is None or api is None:
            dropped.append(Dropped(reason=Reason.invalid_shape, quote=quote))
            continue
        evidence, page_id = _locate_mapping_quote(api, quote, contexts)
        if evidence is None:
            dropped.append(Dropped(reason=Reason.quote_not_found, quote=quote))
            continue
        mappings.append(
            FieldMapping(
                frontField=front,
                backendParam=backend,
                api=api,
                pageId=page_id,
                quote=evidence.quote,
                evidence=[evidence],
            )
        )
    return mappings, dropped, []


def _locate_mapping_quote(
    api: str, quote: str | None, contexts: list[_PageContext]
) -> tuple[VerifiedEvidence | None, str | None]:
    """按 api 反查页面，再在该页语料里校验引文。

    api 是「METHOD url」或 url：先用 url 匹配页面接口集合；方法不一致时
    仍按 url 归页（模型可能省略 method）。反查不到页面时退化为全页唯一
    命中策略。
    """
    url = api.split(None, 1)[1] if " " in api else api
    for context in contexts:
        if any(candidate == url or candidate[1] == url for candidate in context.api_urls):
            evidence = _verified(quote, context) if quote is not None else None
            if evidence is not None:
                return evidence, context.material.page.pageId
            return None, None
    return _locate_by_quote(quote, contexts)


def _locate_by_quote(
    quote: str | None, contexts: list[_PageContext]
) -> tuple[VerifiedEvidence | None, str | None]:
    """引文跨页定位：恰好一个页面命中才认领。"""
    if not isinstance(quote, str) or not quote:
        return None, None
    hits = []
    for context in contexts:
        evidence = _verified(quote, context)
        if evidence is not None:
            hits.append((evidence, context.material.page.pageId))
    if len(hits) == 1:
        return hits[0]
    return None, None


def _str_list(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = [_unwrap_quoted(item) for item in value if isinstance(item, str) and item.strip()]
    cleaned = [item for item in items if item]
    return cleaned or None


def _unwrap_quoted(text: str) -> str:
    """剥掉模型偶尔带上的 `key='…'` 包装（glm 系输出 quirk），保留正文。"""
    match = re.fullmatch(r"\w+\s*=\s*(['\"])(.+)\1", text.strip())
    return match.group(2).strip() if match else text.strip()


def _str_or_none(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _unavailable_warning(section: str) -> RequirementWarning:
    return RequirementWarning(
        code="llm_unavailable",
        severity=Severity.warning,
        message=f"{section}不可用（模型不可用或多次输出无效），该章节已留空。确定性章节不受影响。",
    )


def _shape_warning(section: str) -> RequirementWarning:
    return RequirementWarning(
        code="llm_invalid_output",
        severity=Severity.warning,
        message=f"{section}输出形状不符合预期（缺少列表字段），该章节已留空。",
    )
