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
    Event,
    FieldMapping,
    Location,
    Rule,
    RuleCategory,
    Source,
    Step,
    VerifiedEvidence,
)
from app.requirement.chains import ChainStep
from app.requirement.judge import capped_confidence, match_quote, parse_json
from app.requirement.llm import LLMCallError, LLMClient
from app.specdoc.apimap import all_api_steps
from app.specdoc.collect import Materials, PageMaterial
from app.specdoc.evidence import build_evidence_pack

_STEPS_SYSTEM = """\
你是资深前端规格分析师。根据给定的需求、功能点与页面语料（均为代码原文），\
为每个功能点产出「业务操作步骤」。

只输出 JSON，形状：
{"steps": [{"featureId": "f1", "chainId": "链路id", "nodeId": "节点id",
  "precondition": ["前置条件"], "actions": ["操作1"], "quote": "逐字引文", "confidence": 0.8}]}

要求：
1. 每个功能点最多一组步骤；featureId 必须使用给定 id，不得编造。
2. 必须返回 evidence pack 中真实的 chainId 和该链路内的 nodeId；步骤只能来自链路节点事实。
3. actions 是有序的用户操作，每条一句话，写到「系统给出什么反馈」为止。
4. quote 必须从对应 node 的文本字段逐字复制，禁止改写或拼凑。
5. 禁止编造；无匹配链路或证据不足时省略该功能点。
"""

_RULES_SYSTEM = """\
你是资深前端规格分析师。根据给定的需求、功能点与页面语料（均为代码原文），\
归纳页面上的前端业务规则，按五类输出：validation（交互校验）/ display（展示规则）\
/ linkage（联动规则）/ session（会话状态）/ permission（权限规则）。

只输出 JSON，形状：
{"rules": [{"featureId": "f1", "chainId": "链路id", "elementId": "元素id",
  "category": "validation", "target": "作用对象", "condition": "条件",
  "result": "结果", "quote": "逐字引文", "confidence": 0.8}]}

要求：
1. category 必须是五类之一；功能点规则必须用给定 featureId，页面级规则可省略。
2. 必须返回真实 elementId 或 chainId；引用标识必须属于目标页面。
3. 仅当证据出现校验文案、disabled、条件渲染、权限分支、分页/空态参数或联动行为时输出；
   静态文案不是业务规则。
4. quote 必须从对应节点文本逐字复制，禁止改写或拼凑。
5. 无明确证据时省略，不得编造规则。
"""

_MAPPING_SYSTEM = """\
你是资深前端规格分析师。根据给定的页面语料（均为代码原文），归纳\
「前端字段 → 后端参数」的映射。

只输出 JSON，形状：
{"fieldMappings": [{"frontField": "前端字段名", "backendParam": "后端参数名",
  "api": "POST /api/path", "elementId": "元素id", "quote": "逐字引文", "confidence": 0.8}]}

要求：
1. 映射必须有 elementId，且仅从该页 forms 与 API requestBody 事实归纳；api 必须命中该页 apiFacts。
2. api 用「METHOD url」或 url；不得将其他页面的字段、请求或接口拼接。
3. quote 必须是 evidence pack 中连续原文，逐字复制；禁止改写或拼凑。
4. 缺少证据的映射不要输出。
"""


@dataclass
class _PageContext:
    """送入 prompt 与引文校验的页面级上下文。"""

    material: PageMaterial
    # 引文命中的原文 → 证据位置（node/file/loc），只覆盖已进语料的文本
    evidence_by_text: dict[str, VerifiedEvidence] = field(default_factory=dict)
    # 该页面的接口集合（method, url），字段映射按 api 反查页面用
    api_urls: tuple[tuple[str, str], ...] = ()
    # 页面内可用节点文本与节点所属链路，用于锚点范围校验
    texts_by_node: dict[str, tuple[str, ...]] = field(default_factory=dict)
    chain_nodes: dict[str, frozenset[str]] = field(default_factory=dict)
    feature_chains: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def corpus(self) -> tuple[str, ...]:
        return self.material.corpus

    @property
    def prompt_corpus(self) -> tuple[str, ...]:
        return tuple(text for values in self.texts_by_node.values() for text in values)


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
    *,
    events_by_page: dict[str, list[Event]] | None = None,
) -> InferOutcome:
    """跑三个推断章节。模型彻底不可用时不抛错：章节留空 + llm_unavailable 告警。"""
    contexts = _page_contexts(materials)
    evidence_pack = build_evidence_pack(materials, events_by_page)
    evidence_json, exposed_texts = evidence_pack.prompt_view()
    for context in contexts:
        context.texts_by_node = exposed_texts.get(
            context.material.page.pageId, {}
        )
        context.chain_nodes = {
            chain.id: frozenset(chain.node_ids)
            for chain in context.material.chains
            if chain.id in {
                item.get("chainId")
                for page in evidence_pack.pages
                if page.get("pageId") == context.material.page.pageId
                for item in page.get("chains", [])
            }
        }
        context.feature_chains = {
            feature_id: frozenset(
                match.chainId for match in (requirement_map.matches or [])
                if match.featureId == feature_id
                and match.pageNodeId == context.material.page.pageId
                and match.chainId is not None
                and match.chainId in context.chain_nodes
            )
            for feature_id in (context.material.page.featureIds or [])
        }
    feature_pages = _feature_pages(requirement_map, contexts)
    features = list(requirement_map.features)

    outcome = InferOutcome(
        warnings=[
            RequirementWarning(
                code=warning,
                severity=Severity.warning,
                message="结构化证据包超过预算，已按确定性顺序裁剪。",
            )
            for warning in evidence_pack.warnings
        ]
    )
    steps, dropped, warnings = await _infer_steps(
        requirement, features, feature_pages, contexts, llm, max_calls, evidence_json
    )
    outcome.steps, outcome.dropped = steps, dropped
    outcome.warnings.extend(warnings or [])
    rules, dropped, warnings = await _infer_rules(
        requirement, features, feature_pages, contexts, llm, max_calls, evidence_json
    )
    outcome.rules.extend(rules)
    outcome.dropped.extend(dropped)
    outcome.warnings.extend(warnings or [])
    mappings, dropped, warnings = await _infer_mappings(
        requirement, contexts, llm, max_calls, evidence_json
    )
    outcome.field_mappings.extend(mappings)
    outcome.dropped.extend(dropped)
    outcome.warnings.extend(warnings or [])
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
            field_info = source.node.attrs.get("formField")
            if isinstance(field_info, dict):
                quote = field_info.get("quote")
                if isinstance(quote, str) and quote.strip():
                    evidence_by_text.setdefault(
                        quote, _evidence_from_step(source.node.id, quote, None)
                    )
            columns = source.node.attrs.get("columns")
            if isinstance(columns, list):
                for column in columns:
                    title = column.get("title") if isinstance(column, dict) else None
                    if isinstance(title, str) and title.strip():
                        evidence_by_text.setdefault(
                            title, _evidence_from_step(source.node.id, title, None)
                        )
            events = source.node.attrs.get("events")
            if not isinstance(events, list):
                continue
            steps_by_id = {
                step.node_id: step
                for chain in source.chains
                for step in chain.steps
            }
            for binding in events:
                if not isinstance(binding, dict):
                    continue
                handler_source = binding.get("handler")
                if not isinstance(handler_source, str) or not handler_source.strip():
                    continue
                targets = binding.get("targets")
                handler_step = None
                if isinstance(targets, list):
                    handler_step = next(
                        (
                            steps_by_id[target["id"]]
                            for target in targets
                            if isinstance(target, dict)
                            and isinstance(target.get("id"), str)
                            and target["id"] in steps_by_id
                        ),
                        None,
                    )
                evidence_by_text.setdefault(
                    handler_source,
                    _evidence_from_step(
                        handler_step.node_id if handler_step is not None else source.node.id,
                        handler_source,
                        handler_step,
                    ),
                )
        contexts.append(
            _PageContext(
                material=material,
                evidence_by_text=evidence_by_text,
                api_urls=tuple(all_api_steps(material.chains)),
            )
        )
    return contexts


def _evidence_from_step(
    node_id: str, text: str, step: ChainStep | None
) -> VerifiedEvidence:
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
    evidence_json: str,
) -> str:
    lines: list[str] = [f"需求：{requirement}"]
    if with_features:
        lines.append("功能点：")
        lines.extend(f"- {item.id}: {item.text}" for item in features)
    lines.append("结构化证据包（唯一事实来源；引文只能从其中的文本字段逐字复制）：")
    lines.append(evidence_json)
    return "\n".join(lines)


def _verified(
    quote: str, context: _PageContext, *, node_id: str | None = None,
    chain_id: str | None = None, element_id: str | None = None,
) -> VerifiedEvidence | None:
    """逐字校验引文，并在提供引用锚点时限制到对应节点/链路。"""
    if node_id is not None and node_id not in context.texts_by_node:
        return None
    allowed_nodes: set[str] | None = None
    if chain_id is not None:
        chain_nodes = context.chain_nodes.get(chain_id)
        if chain_nodes is None:
            return None
        allowed_nodes = set(chain_nodes)
    if element_id is not None:
        if (
            element_id not in {source.node.id for source in context.material.elements}
            and (allowed_nodes is None or element_id not in allowed_nodes)
        ):
            return None
        if allowed_nodes is not None and element_id not in allowed_nodes:
            return None
        allowed_nodes = {element_id} if allowed_nodes is None else allowed_nodes
    if node_id is not None:
        if allowed_nodes is not None and node_id not in allowed_nodes:
            return None
        allowed_nodes = {node_id}

    corpus = context.prompt_corpus
    evidence_index = context.evidence_by_text
    if allowed_nodes is not None:
        corpus = tuple(
            text for current in sorted(allowed_nodes)
            for text in context.texts_by_node.get(current, ())
        )
        evidence_index = {
            text: context.evidence_by_text[text]
            for text in corpus if text in context.evidence_by_text
        }
    match = match_quote(quote, corpus)
    if match is None:
        return None
    evidence = evidence_index.get(match.corpus_text)
    if allowed_nodes is not None:
        for current in sorted(allowed_nodes):
            if match.corpus_text not in context.texts_by_node.get(current, ()):
                continue
            step = next(
                (
                    step for chain in context.material.chains
                    for step in chain.steps if step.node_id == current
                ),
                None,
            )
            evidence = _evidence_from_step(current, match.corpus_text, step)
            break
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
    evidence_json: str,
) -> tuple[list[Step], list[Dropped], list[RequirementWarning] | None]:
    if not features:
        return [], [], []
    parsed = await _call_json(
        llm,
        _STEPS_SYSTEM,
        _user_prompt(
            requirement, features, contexts, with_features=True,
            evidence_json=evidence_json,
        ),
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
        chain_id = _str_or_none(entry.get("chainId"))
        node_id = _str_or_none(entry.get("nodeId"))
        valid_chains = context.feature_chains.get(feature_id, frozenset())
        if chain_id is not None and chain_id not in valid_chains:
            dropped.append(
                Dropped(
                    reason=Reason.chain_not_in_candidates,
                    featureId=feature_id,
                    chainId=chain_id,
                    nodeId=node_id,
                    quote=_str_or_none(quote),
                )
            )
            continue
        if (
            chain_id is not None
            and node_id is not None
            and node_id not in context.chain_nodes.get(chain_id, frozenset())
        ):
            dropped.append(
                Dropped(
                    reason=Reason.node_not_in_candidates,
                    featureId=feature_id,
                    chainId=chain_id,
                    nodeId=node_id,
                    quote=_str_or_none(quote),
                )
            )
            continue
        evidence = (
            _verified(quote, context, node_id=node_id, chain_id=chain_id)
            if isinstance(quote, str) and chain_id is not None and node_id is not None
            else None
        )
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
    evidence_json: str,
) -> tuple[list[Rule], list[Dropped], list[RequirementWarning] | None]:
    parsed = await _call_json(
        llm,
        _RULES_SYSTEM,
        _user_prompt(
            requirement, features, contexts, with_features=True,
            evidence_json=evidence_json,
        ),
        max_calls,
    )
    if parsed is None:
        return [], [], [_unavailable_warning("业务规则推断")]
    context_by_page = {ctx.material.page.pageId: ctx for ctx in contexts}
    valid_categories = {item.value for item in RuleCategory}
    rules: list[Rule] = []
    dropped: list[Dropped] = []
    warnings: list[RequirementWarning] = []
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
            chain_id = _str_or_none(entry.get("chainId"))
            element_id = _str_or_none(entry.get("elementId"))
            node_id = _str_or_none(entry.get("nodeId"))
            valid_chains = context.feature_chains.get(feature_id, frozenset())
            if chain_id is not None and chain_id not in valid_chains:
                dropped.append(
                    Dropped(
                        reason=Reason.chain_not_in_candidates,
                        featureId=feature_id,
                        chainId=chain_id,
                        nodeId=node_id,
                        quote=_str_or_none(quote),
                    )
                )
                continue
            if node_id is not None and (
                node_id not in context.texts_by_node
                or (
                    chain_id is not None
                    and node_id not in context.chain_nodes.get(chain_id, frozenset())
                )
            ):
                dropped.append(
                    Dropped(
                        reason=Reason.node_not_in_candidates,
                        featureId=feature_id,
                        chainId=chain_id,
                        nodeId=node_id,
                        quote=_str_or_none(quote),
                    )
                )
                continue
            if isinstance(quote, str) and chain_id is not None:
                evidence = _verified(
                    quote, context, chain_id=chain_id, element_id=element_id,
                    node_id=node_id,
                )
                if evidence is not None:
                    page_id = context.material.page.pageId
            elif isinstance(quote, str):
                evidence = _verified(quote, context)
                warnings.append(
                    RequirementWarning(
                        code="evidence_ref_missing",
                        severity=Severity.warning,
                        message="功能点规则缺少引用锚点，已使用页面级逐字校验。",
                    )
                )
            page_id = page_id or context.material.page.pageId
        else:
            # 页面级规则仍要求跨页面唯一命中；若给出锚点，还必须在锚定范围内命中。
            chain_id = _str_or_none(entry.get("chainId"))
            element_id = _str_or_none(entry.get("elementId"))
            node_id = _str_or_none(entry.get("nodeId"))
            if chain_id or element_id or node_id:
                hits = [
                    (evidence, context.material.page.pageId)
                    for context in contexts
                    if (evidence := _verified(
                        quote, context, chain_id=chain_id,
                        element_id=element_id, node_id=node_id,
                    )) is not None
                ] if isinstance(quote, str) else []
                evidence, page_id = hits[0] if len(hits) == 1 else (None, None)
            else:
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
    return rules, dropped, warnings


async def _infer_mappings(
    requirement: str,
    contexts: list[_PageContext],
    llm: LLMClient,
    max_calls: int,
    evidence_json: str,
) -> tuple[list[FieldMapping], list[Dropped], list[RequirementWarning] | None]:
    parsed = await _call_json(
        llm,
        _MAPPING_SYSTEM,
        _user_prompt(requirement, [], contexts, with_features=False, evidence_json=evidence_json),
        max_calls,
    )
    if parsed is None:
        return [], [], [_unavailable_warning("字段映射推断")]
    mappings: list[FieldMapping] = []
    dropped: list[Dropped] = []
    warnings: list[RequirementWarning] = []
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
        element_id = _str_or_none(entry.get("elementId"))
        if element_id is None:
            warnings.append(
                RequirementWarning(
                    code="evidence_ref_missing",
                    severity=Severity.warning,
                    message="字段映射缺少 elementId，已拒绝该条映射。",
                )
            )
            evidence, page_id = None, None
        else:
            evidence, page_id = _locate_mapping_quote_tolerant(
                api, quote, contexts, element_id=element_id
            )
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
    return mappings, dropped, warnings


def _mapping_quote_candidates(quote: str | None) -> list[str]:
    """引文候选：整条优先；列表式引文拆行兜底。

    模型不稳定时会把 quote 写成关键词罗列（`- Your Name\\n- useRegister`），
    多行整体逐字匹配必然失败。先按原样校验，失败后拆行去掉列表标记逐行再试。
    拆出的每一行仍走同一套逐字校验——防幻觉纪律不变，命中不了照样拒绝。
    """
    if not isinstance(quote, str) or not quote:
        return []
    if "\n" not in quote:
        return [quote]
    candidates = [quote]
    for line in quote.splitlines():
        stripped = line.strip().lstrip("-*·•").strip()
        if stripped and stripped not in candidates:
            candidates.append(stripped)
    return candidates


def _locate_mapping_quote_tolerant(
    api: str, quote: str | None, contexts: list[_PageContext], *, element_id: str | None
) -> tuple[VerifiedEvidence | None, str | None]:
    """逐个候选引文尝试定位，且限制到模型声明的表单元素。"""
    if element_id is None:
        return None, None
    for candidate in _mapping_quote_candidates(quote):
        evidence, page_id = _locate_mapping_quote(api, candidate, contexts, element_id=element_id)
        if evidence is not None:
            return evidence, page_id
    return None, None


def _locate_mapping_quote(
    api: str, quote: str | None, contexts: list[_PageContext], *, element_id: str
) -> tuple[VerifiedEvidence | None, str | None]:
    """按 api 反查页面，再在该页语料里校验引文。

    api 是「METHOD url」或 url：先用 url 匹配页面接口集合；方法不一致时
    仍按 url 归页（模型可能省略 method）。反查不到页面时退化为全页唯一
    命中策略。
    """
    if not isinstance(quote, str):
        return None, None
    url = api.split(None, 1)[1] if " " in api else api
    for context in contexts:
        if any(candidate == url or candidate[1] == url for candidate in context.api_urls):
            if any(source.node.id == element_id for source in context.material.elements):
                api_node_id = next(
                    (
                        node_id for node_id, api_node in context.material.api_nodes.items()
                        if api_node.attrs.get("url") == url
                    ),
                    None,
                )
                if api_node_id is not None:
                    evidence = _verified(quote, context, node_id=api_node_id)
                    if evidence is not None:
                        return evidence, context.material.page.pageId
            return None, None
    return None, None


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


def _str_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = [_unwrap_quoted(item) for item in value if isinstance(item, str) and item.strip()]
    cleaned = [item for item in items if item]
    return cleaned or None


def _unwrap_quoted(text: str) -> str:
    """剥掉模型偶尔带上的 `key='…'` 包装（glm 系输出 quirk），保留正文。"""
    match = re.fullmatch(r"\w+\s*=\s*(['\"])(.+)\1", text.strip())
    return match.group(2).strip() if match else text.strip()


def _str_or_none(value: object) -> str | None:
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
