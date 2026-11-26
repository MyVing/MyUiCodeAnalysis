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

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncEngine

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
from app.specdoc.evidence import build_evidence_pack, build_skeleton
from app.specdoc.evidence_query import EvidenceQuery, graph_rows_exist
from app.specdoc.llm import OpenAICompatibleToolLLM
from app.specdoc.retrieval import EVIDENCE_TOOL, EvidenceLedger, EvidenceSearch

logger = logging.getLogger(__name__)

_STEPS_SYSTEM = """\
你是资深前端规格分析师。根据给定的需求、功能点与页面语料（均为代码原文），\
为每个功能点产出「业务操作步骤」。

只输出 JSON，形状：
{"steps": [{"featureId": "f1", "pageId": "页面id（可选）",
  "chainId": "链路id（可选）", "nodeId": "节点id（可选）",
  "precondition": ["前置条件"], "actions": ["操作1"], "quote": "逐字引文", "confidence": 0.8}]}

要求：
1. 每个功能点可产出多组步骤；featureId 必须使用给定 id，不得编造。
2. 有链路锚点时必须使用该功能点在目标页面的真实 chainId，并将 nodeId 限制在链路内；
   无链路锚点时可使用目标页面真实节点文本，但不得编造引用标识。可提供 pageId 指明目标页面。
3. actions 是有序的用户操作，每条一句话，写到「系统给出什么反馈」为止。
4. quote 必须从对应 node 的文本字段逐字复制，禁止改写或拼凑。
5. 禁止编造；无匹配链路或证据不足时省略该功能点。
"""

_RULES_SYSTEM = """\
你是资深前端规格分析师。根据给定的需求、功能点与页面语料（均为代码原文），\
归纳页面上的前端业务规则，按五类输出：validation（交互校验）/ display（展示规则）\
/ linkage（联动规则）/ session（会话状态）/ permission（权限规则）。

只输出 JSON，形状：
{"rules": [{"featureId": "f1", "pageId": "页面id（可选）", "chainId": "链路id",
  "elementId": "元素id", "category": "validation", "target": "作用对象", "condition": "条件",
  "result": "结果", "quote": "逐字引文", "confidence": 0.8}]}

要求：
1. category 必须是五类之一；功能点规则必须用给定 featureId，页面级规则可省略。可选 pageId 只可指向
   该功能点命中的页面。
2. 必须返回真实 elementId 或 chainId。display 规则可引用页面级 uiText 并提供 pageId；
   即使同时有有效 chainId，页面文案也按页面节点校验。其他类别应使用链路或元素范围内的事实。
   所有显式引用标识必须属于目标页面。所有显式引用标识必须属于目标页面。
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
    ledger: EvidenceLedger | None = None
    texts_by_node: dict[str, tuple[str, ...]] = field(default_factory=dict)
    chain_nodes: dict[str, frozenset[str]] = field(default_factory=dict)
    feature_chains: dict[str, frozenset[str]] = field(default_factory=dict)
    ui_text: tuple[str, ...] = ()

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
    shadow_counts: dict[str, int] = field(default_factory=dict)


async def infer_sections(
    requirement: str,
    requirement_map: RequirementMap,
    materials: Materials,
    llm: OpenAICompatibleToolLLM,
    max_calls: int,
    *,
    events_by_page: dict[str, list[Event]] | None = None,
    engine: AsyncEngine | None = None,
    project_id: str | None = None,
    analysis_id: str | None = None,
) -> InferOutcome:
    """跑三个推断章节。模型彻底不可用时不抛错：章节留空 + llm_unavailable 告警。

    当 engine + project_id + analysis_id 三者齐全时，使用 EvidenceQuery（SQL 层按需检索，
    直接查 graph_nodes / graph_edges）；否则 fallback EvidenceSearch（全量 flatten 内存过滤）。
    后者是老链路——Memory 实现、单测、或数据库 schema 未升级时自动走这条。
    """
    contexts = _page_contexts(materials)
    tool_mode = callable(getattr(llm, "chat_with_tools", None))
    use_sql_query = (
        tool_mode
        and engine is not None
        and project_id is not None
        and analysis_id is not None
    )
    sql_fallback_warning: RequirementWarning | None = None
    if use_sql_query:
        # P0-5：老图没有行式投影时回退内存检索，避免 MySQL 路径静默全空
        use_sql_query = await graph_rows_exist(
            engine, project_id, analysis_id, "static"  # type: ignore[arg-type]
        )
        if use_sql_query:
            logger.info(
                "specdoc 推断使用 EvidenceQuery（SQL 层按需检索）："
                "project=%s analysis=%s", project_id, analysis_id,
            )
        else:
            logger.warning(
                "graph_nodes 行表为空（历史图未回填），证据检索回退内存实现："
                "project=%s analysis=%s", project_id, analysis_id,
            )
            sql_fallback_warning = RequirementWarning(
                code="evidence_sql_fallback",
                severity=Severity.warning,
                message="图行式索引为空（历史图未回填），本次证据检索已回退内存检索。",
            )

    if use_sql_query:
        # P1-3：SQL 路径的校验边界直接取 materials，不经过证据包裁剪
        evidence_pack = None
        evidence_json = ""
        exposed_texts = {}
        exposed_pages = ()
        for context in contexts:
            _apply_material_scope(context, requirement_map)
    else:
        evidence_pack = build_evidence_pack(materials, events_by_page)
        evidence_json, exposed_texts = evidence_pack.prompt_view()
        exposed_pages = evidence_pack.prompt_pages
        for context in contexts:
            context.texts_by_node = exposed_texts.get(
                context.material.page.pageId, {}
            )
            exposed_page = next(
                (
                    page for page in exposed_pages
                    if page.get("pageId") == context.material.page.pageId
                ),
                {},
            )
            ui_texts = set(exposed_page.get("uiText", []))
            context.ui_text = tuple(
                text for text in context.texts_by_node.get(context.material.page.pageId, ())
                if text in ui_texts
            )
            exposed_chains = {
                item.get("chainId") for item in exposed_page.get("chains", [])
            }
            context.chain_nodes = {
                chain.id: frozenset(chain.node_ids)
                for chain in context.material.chains
                if chain.id in exposed_chains
            }
            context.feature_chains = {
                feature_id: frozenset(
                    match.chainId for match in (requirement_map.matches or [])
                    if match.featureId == feature_id
                    and match.pageNodeId == context.material.page.pageId
                    and match.chainId is not None
                    and match.chainId in context.chain_nodes
                )
                for feature_id in {
                    match.featureId for match in (requirement_map.matches or [])
                    if match.pageNodeId == context.material.page.pageId
                }
            }
    pack_warnings = evidence_pack.prompt_warnings if evidence_pack is not None else ()
    feature_pages = _feature_pages(requirement_map, contexts)
    page_nodes, chain_nodes, chain_pages = _scope_maps(materials)
    # 直接复用 collect 已按页面校验过的命中链路，避免从 matches 重算时
    # 把旧图上已失效的 chainId 带进来（会让该页骨架的链路被误删干净）。
    chain_ids_by_page = {
        material.page.pageId: material.matched_chain_ids
        for material in materials.pages
    }
    skeleton_json = json.dumps(
        build_skeleton(materials, feature_pages, chain_ids_by_page),
        ensure_ascii=False,
    )
    features = list(requirement_map.features)
    ledger = EvidenceLedger()

    def _build_search(ledger_arg: EvidenceLedger, allowed_page_ids: set[str]):
        """有 engine 时走 EvidenceQuery（SQL 层），否则 fallback EvidenceSearch。"""
        if use_sql_query:
            return EvidenceQuery(
                engine=engine,  # type: ignore[arg-type]
                project_id=project_id,  # type: ignore[arg-type]
                analysis_id=analysis_id,  # type: ignore[arg-type]
                graph_kind="static",
                ledger=ledger_arg,
                page_nodes=page_nodes,
                chain_nodes=chain_nodes,
                chain_pages=chain_pages,
            )
        return EvidenceSearch(exposed_pages, ledger_arg, allowed_page_ids)

    search = _build_search(
        ledger, {pid for pids in feature_pages.values() for pid in pids}
    )
    shadow_counts: Counter[str] = Counter()

    outcome = InferOutcome(
        warnings=[
            RequirementWarning(
                code=warning,
                severity=Severity.warning,
                message=f"结构化证据包已裁剪类别：{warning.removeprefix('evidence_').removesuffix('_truncated')}。",
            )
            for warning in pack_warnings
        ]
    )
    if not tool_mode:
        logger.warning(
            "specdoc 推断客户端不支持工具调用，回退到完整证据包提示：pages=%d chars=%d",
            len(evidence_pack.pages) if evidence_pack is not None else 0,
            len(evidence_json),
        )
        outcome.warnings.append(
            RequirementWarning(
                code="evidence_full_prompt_fallback",
                severity=Severity.warning,
                message="当前模型客户端不支持证据工具调用，推断使用兼容模式并将结构化证据包直接放入提示词。",
            )
        )
    if sql_fallback_warning is not None:
        outcome.warnings.append(sql_fallback_warning)
    try:
        if tool_mode:
            parsed_text = await llm.chat_with_tools(
                _STEPS_SYSTEM,
                "需求：" + requirement + "\n功能点：\n"
                + "\n".join(f"- {item.id}: {item.text}" for item in features)
                + "\n页面与链路骨架：\n"
                + skeleton_json
                + "\n请先按需调用 search_evidence 获取引文，再输出步骤 JSON。",
                tools=[EVIDENCE_TOOL],
                executor=search.execute,
                max_tool_calls=max(1, max_calls * 2),
            )
            parsed = parse_json(parsed_text)
        else:
            parsed = None
    except LLMCallError as error:
        parsed = None
        tool_warning = RequirementWarning(
            code=getattr(error, "code", "llm_unavailable"),
            severity=Severity.warning,
            message="步骤按需证据检索失败或预算耗尽，步骤章节已留空。",
        )
    else:
        tool_warning = None
    for context in contexts:
        context.ledger = ledger if tool_mode else None
        context.texts_by_node = ledger.texts_by_node() if tool_mode else exposed_texts.get(
            context.material.page.pageId, {}
        )
    if tool_mode and parsed is None:
        steps, dropped, warnings = [], [], [tool_warning or _unavailable_warning("步骤推断")]
    elif tool_mode:
        steps, dropped, warnings = await _infer_steps(
            requirement, features, feature_pages, contexts, llm, max_calls, "",
            parsed_override=parsed,
        )
        if tool_warning is not None:
            warnings = [*(warnings or []), tool_warning]
    else:
        steps, dropped, warnings = await _infer_steps(
            requirement, features, feature_pages, contexts, llm, max_calls, evidence_json
        )
    outcome.steps, outcome.dropped = steps, dropped
    outcome.warnings.extend(warnings or [])
    shadow_counts.update(_shadow_drop_counts(dropped))
    for context in contexts:
        context.ledger = EvidenceLedger() if tool_mode else None
    if tool_mode:
        rules_ledger = EvidenceLedger()
        rules_search = _build_search(
            rules_ledger,
            {context.material.page.pageId for context in contexts},
        )
        try:
            rules_text = await llm.chat_with_tools(
                _RULES_SYSTEM,
                "需求：" + requirement + "\n功能点：\n"
                + "\n".join(f"- {item.id}: {item.text}" for item in features)
                + "\n页面与链路骨架：\n"
                + skeleton_json
                + "\n请按需检索链路、界面文本或事件事实，再输出规则 JSON。",
                tools=[EVIDENCE_TOOL], executor=rules_search.execute,
                max_tool_calls=max(1, max_calls * 2),
            )
            rules_parsed = parse_json(rules_text)
            rules_warning = None
        except LLMCallError as error:
            rules_parsed = None
            rules_warning = RequirementWarning(
                code=getattr(error, "code", "llm_unavailable"),
                severity=Severity.warning,
                message="业务规则按需证据检索失败或预算耗尽，规则章节已留空。",
            )
        for context in contexts:
            context.ledger = rules_ledger
            context.texts_by_node = rules_ledger.texts_by_node()
        rules, dropped, warnings = await _infer_rules(
            requirement, features, feature_pages, contexts, llm, max_calls, "",
            parsed_override=rules_parsed,
        ) if rules_parsed is not None else ([], [], [_unavailable_warning("业务规则推断")])
        if rules_warning is not None:
            warnings = [*(warnings or []), rules_warning]
    else:
        rules, dropped, warnings = await _infer_rules(
            requirement, features, feature_pages, contexts, llm, max_calls, evidence_json
        )
    outcome.rules.extend(rules)
    outcome.dropped.extend(dropped)
    outcome.warnings.extend(warnings or [])
    shadow_counts.update(_shadow_drop_counts(dropped))
    if tool_mode:
        mappings_ledger = EvidenceLedger()
        mapping_search = _build_search(
            mappings_ledger,
            {context.material.page.pageId for context in contexts},
        )
        try:
            mappings_text = await llm.chat_with_tools(
                _MAPPING_SYSTEM,
                "需求：" + requirement + "\n页面、表单与接口范围：\n"
                + skeleton_json
                + "\n仅为 fieldMappings 引用 forms 与 apiFacts；"
                "请先检索 category=form 与 category=apiFact 所需事实，再输出映射 JSON。",
                tools=[EVIDENCE_TOOL], executor=mapping_search.execute,
                max_tool_calls=max(1, max_calls * 2),
            )
            mappings_parsed = parse_json(mappings_text)
            mappings_warning = None
        except LLMCallError as error:
            mappings_parsed = None
            mappings_warning = RequirementWarning(
                code=getattr(error, "code", "llm_unavailable"),
                severity=Severity.warning,
                message="字段映射按需证据检索失败或预算耗尽，字段映射章节已留空。",
            )
        for context in contexts:
            context.ledger = mappings_ledger
            context.texts_by_node = mappings_ledger.texts_by_node()
        mappings, dropped, warnings = await _infer_mappings(
            requirement, contexts, llm, max_calls, "", parsed_override=mappings_parsed,
            available=not any(
                code in pack_warnings
                for code in ("evidence_forms_truncated", "evidence_api_facts_truncated")
            ),
        ) if mappings_parsed is not None else ([], [], [_unavailable_warning("字段映射推断")])
        if mappings_warning is not None:
            warnings = [*(warnings or []), mappings_warning]
    else:
        mappings, dropped, warnings = await _infer_mappings(
            requirement, contexts, llm, max_calls, evidence_json,
            available=not any(
                code in pack_warnings
                for code in ("evidence_forms_truncated", "evidence_api_facts_truncated")
            ),
        )
    outcome.field_mappings.extend(mappings)
    outcome.dropped.extend(dropped)
    outcome.warnings.extend(warnings or [])
    shadow_counts.update(_shadow_drop_counts(dropped))
    outcome.shadow_counts = dict(sorted(shadow_counts.items()))
    return outcome


def _shadow_drop_counts(dropped: list[Dropped]) -> Counter[str]:
    """Collect privacy-safe reason counts without changing validation outcomes."""
    return Counter(item.reason.value for item in dropped)


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
        for api_id, api_node in material.api_nodes.items():
            request_body = api_node.attrs.get("requestBody")
            if isinstance(request_body, str):
                request_texts = [request_body]
            elif isinstance(request_body, dict):
                request_texts = [
                    f"{key}: {value}" for key, value in sorted(request_body.items())
                ]
            else:
                request_texts = []
            for text in request_texts:
                if isinstance(text, str) and text.strip():
                    evidence_by_text.setdefault(
                        text, _evidence_from_step(api_id, text, None)
                    )
        contexts.append(
            _PageContext(
                material=material,
                evidence_by_text=evidence_by_text,
                api_urls=tuple(dict.fromkeys(
                    [
                        (str(method or ""), str(url or ""))
                        for method, url in all_api_steps(material.chains)
                    ]
                    + [
                        (str(api.attrs.get("method") or ""), str(api.attrs.get("url") or ""))
                        for api in material.api_nodes.values()
                    ]
                )),
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


def _apply_material_scope(
    context: _PageContext, requirement_map: RequirementMap
) -> None:
    """SQL 模式：校验边界直接取 materials，不受证据包裁剪影响。

    - ui_text：页面节点的 uiText 全量（display 规则按页校验）
    - chain_nodes：该页全部链路（不经 60k 裁剪）
    - feature_chains：featureId → 命中链路，供锚点校验
    """
    material = context.material
    page_id = material.page.pageId
    ui_texts: list[str] = []
    ui_attr = material.node.attrs.get("uiText")
    if isinstance(ui_attr, dict):
        for entry in ui_attr.get("texts") or []:
            text = entry.get("text") if isinstance(entry, dict) else None
            if isinstance(text, str) and text.strip() and text not in ui_texts:
                ui_texts.append(text)
    context.ui_text = tuple(ui_texts)
    context.chain_nodes = {
        chain.id: frozenset(chain.node_ids) for chain in material.chains
    }
    context.feature_chains = {
        feature_id: frozenset(
            match.chainId for match in (requirement_map.matches or [])
            if match.featureId == feature_id
            and match.pageNodeId == page_id
            and match.chainId is not None
            and match.chainId in context.chain_nodes
        )
        for feature_id in {
            match.featureId for match in (requirement_map.matches or [])
            if match.pageNodeId == page_id
        }
    }


def _scope_maps(
    materials: Materials,
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
    dict[str, str],
]:
    """构造 SQL 证据检索的授权映射。

    页面边界与 EvidenceSearch 保持一致：页面节点、元素、接口、链路节点
    都归属其所在页面；链路节点（handler / api）不在元素清单里，但仍需能按
    链路检索，因此单独保留 chain_nodes / chain_pages。
    """
    page_nodes: dict[str, tuple[str, ...]] = {}
    chain_nodes: dict[str, tuple[str, ...]] = {}
    chain_pages: dict[str, str] = {}
    for material in materials.pages:
        page_id = material.page.pageId
        node_ids = [material.node.id]
        node_ids.extend(source.node.id for source in material.elements)
        node_ids.extend(material.api_nodes.keys())
        for chain in material.chains:
            chain_nodes[chain.id] = tuple(chain.node_ids)
            chain_pages[chain.id] = page_id
            node_ids.extend(chain.node_ids)
        page_nodes[page_id] = tuple(dict.fromkeys(node_ids))
    return page_nodes, chain_nodes, chain_pages


def _feature_pages(
    requirement_map: RequirementMap, contexts: list[_PageContext]
) -> dict[str, tuple[str, ...]]:
    """featureId → 全部有效命中页面，按 contexts 顺序保留兼容默认项。"""
    available = {ctx.material.page.pageId for ctx in contexts}
    result: dict[str, list[str]] = {}
    for match in requirement_map.matches:
        if match.pageNodeId in available:
            pages = result.setdefault(match.featureId, [])
            if match.pageNodeId not in pages:
                pages.append(match.pageNodeId)
    return {feature_id: tuple(pages) for feature_id, pages in result.items()}


def _select_feature_context(
    feature_id: str,
    page_id: str | None,
    chain_id: str | None,
    feature_pages: dict[str, tuple[str, ...]],
    context_by_page: dict[str, _PageContext],
) -> tuple[_PageContext | None, Reason | None]:
    """选择 feature 页面；显式引用冲突或无效时拒绝而不回退。"""
    pages = feature_pages.get(feature_id, ())
    if page_id is not None and page_id not in pages:
        return None, Reason.node_not_in_candidates

    chain_pages = tuple(
        candidate for candidate in pages
        if chain_id is not None
        and chain_id in (
            context_by_page[candidate].feature_chains.get(feature_id, frozenset())
            or frozenset(context_by_page[candidate].chain_nodes)
        )
    )
    if chain_id is not None:
        if len(chain_pages) != 1:
            return None, Reason.chain_not_in_candidates
        chain_page = chain_pages[0]
        if page_id is not None and page_id != chain_page:
            return None, Reason.chain_not_in_candidates
        return context_by_page[chain_page], None

    selected_page = page_id or (pages[0] if pages else None)
    if selected_page is None:
        return None, Reason.node_not_in_candidates
    return context_by_page[selected_page], None


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
    ui_text_only: bool = False,
    required_category: str | None = None,
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

    corpus = context.ui_text if ui_text_only else context.prompt_corpus
    evidence_index = context.evidence_by_text
    if ui_text_only:
        allowed_nodes = {context.material.page.pageId}
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
    if context.ledger is not None:
        ledger_fact = next(
            (
                fact for fact in context.ledger.facts.values()
                if fact.page_id == context.material.page.pageId
                and fact.text == match.corpus_text
                and (allowed_nodes is None or fact.node_id in allowed_nodes)
                and (chain_id is None or fact.chain_id == chain_id)
                and (node_id is None or fact.node_id == node_id)
                and (element_id is None or fact.node_id == element_id)
                and (required_category is None or fact.category == required_category)
            ),
            None,
        )
        if ledger_fact is None:
            return None
        step = next(
            (
                step for chain in context.material.chains
                if chain.id == ledger_fact.chain_id
                for step in chain.steps if step.node_id == ledger_fact.node_id
            ),
            None,
        )
        evidence = _evidence_from_step(ledger_fact.node_id, match.corpus_text, step)
    elif allowed_nodes is not None:
        current = next(
            (item for item in sorted(allowed_nodes)
             if match.corpus_text in context.texts_by_node.get(item, ())),
            None,
        )
        if current is not None:
            step = next(
                (
                    step for chain in context.material.chains
                    for step in chain.steps if step.node_id == current
                ),
                None,
            )
            evidence = _evidence_from_step(current, match.corpus_text, step)
    if evidence is None:
        return None
    confidence = capped_confidence(1.0, quote_ambiguous=match.partial)
    return evidence.model_copy(update={"confidence": confidence})


async def _infer_steps(
    requirement: str,
    features: list[Feature],
    feature_pages: dict[str, tuple[str, ...]],
    contexts: list[_PageContext],
    llm: LLMClient,
    max_calls: int,
    evidence_json: str,
    *,
    parsed_override: dict[str, object] | None = None,
) -> tuple[list[Step], list[Dropped], list[RequirementWarning] | None]:
    if not features:
        return [], [], []
    parsed = parsed_override
    if parsed is None:
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
    step_counters: dict[str, int] = {}
    dropped: list[Dropped] = []
    warnings: list[RequirementWarning] = []
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
        page_id = _str_or_none(entry.get("pageId"))
        chain_id = _str_or_none(entry.get("chainId"))
        node_id = _str_or_none(entry.get("nodeId"))
        context, selection_error = _select_feature_context(
            feature_id, page_id, chain_id, feature_pages, context_by_page
        )
        if selection_error is not None:
            dropped.append(
                Dropped(
                    reason=selection_error,
                    featureId=feature_id,
                    chainId=chain_id,
                    nodeId=node_id,
                    quote=_str_or_none(entry.get("quote")),
                )
            )
            continue
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
        evidence = None
        if isinstance(quote, str):
            evidence = _verified(quote, context, node_id=node_id, chain_id=chain_id)
        if evidence is None:
            dropped.append(
                Dropped(
                    reason=Reason.quote_not_found,
                    featureId=feature_id,
                    quote=_str_or_none(quote),
                )
            )
            continue
        step_counters[feature_id] = step_counters.get(feature_id, 0) + 1
        steps.append(
            Step(
                id=f"step.{feature_id}.{step_counters[feature_id]}",
                featureId=feature_id,
                pageId=context.material.page.pageId,
                precondition=_str_list(entry.get("precondition")) or None,
                actions=actions,
                quote=evidence.quote,
                evidence=[evidence],
            )
        )
        if chain_id is None:
            warnings.append(
                RequirementWarning(
                    code="evidence_ref_missing",
                    severity=Severity.warning,
                    message="步骤缺少链路锚点，已使用页面范围逐字校验。",
                    featureId=feature_id,
                )
            )
    return steps, dropped, warnings


async def _infer_rules(
    requirement: str,
    features: list[Feature],
    feature_pages: dict[str, tuple[str, ...]],
    contexts: list[_PageContext],
    llm: LLMClient,
    max_calls: int,
    evidence_json: str,
    *,
    parsed_override: dict[str, object] | None = None,
) -> tuple[list[Rule], list[Dropped], list[RequirementWarning] | None]:
    parsed = parsed_override if parsed_override is not None else await _call_json(
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
            page_id_hint = _str_or_none(entry.get("pageId"))
            chain_id = _str_or_none(entry.get("chainId"))
            element_id = _str_or_none(entry.get("elementId"))
            node_id = _str_or_none(entry.get("nodeId"))
            context, selection_error = _select_feature_context(
                feature_id, page_id_hint, chain_id, feature_pages, context_by_page
            )
            if selection_error is not None:
                dropped.append(
                    Dropped(
                        reason=selection_error,
                        featureId=feature_id,
                        chainId=chain_id,
                        nodeId=node_id,
                        quote=_str_or_none(quote),
                    )
                )
                continue
            if context is None:
                dropped.append(Dropped(reason=Reason.node_not_in_candidates,
                                       featureId=feature_id, quote=_str_or_none(quote)))
                continue
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
                if element_id is not None and (
                    element_id not in {source.node.id for source in context.material.elements}
                    and element_id not in context.chain_nodes.get(chain_id, frozenset())
                ):
                    dropped.append(
                        Dropped(
                            reason=Reason.node_not_in_candidates,
                            featureId=feature_id,
                            chainId=chain_id,
                            nodeId=element_id,
                            quote=_str_or_none(quote),
                        )
                    )
                    continue
                if category == RuleCategory.display.value and quote in context.ui_text:
                    if element_id is not None or (
                        node_id not in (None, context.material.page.pageId)
                    ):
                        dropped.append(
                            Dropped(reason=Reason.node_not_in_candidates, featureId=feature_id,
                                    chainId=chain_id, nodeId=node_id, quote=_str_or_none(quote))
                        )
                        continue
                    evidence = _verified(
                        quote, context, node_id=context.material.page.pageId, ui_text_only=True
                    )
                else:
                    evidence = _verified(
                        quote, context, chain_id=chain_id, element_id=element_id,
                        node_id=node_id,
                    )
                if evidence is not None:
                    page_id = context.material.page.pageId
            elif isinstance(quote, str):
                display_without_chain = category == RuleCategory.display.value
                if (
                    not display_without_chain
                    and quote in context.ui_text
                    and node_id is None
                    and element_id is None
                ):
                    evidence = None
                elif display_without_chain and (
                    node_id not in (None, context.material.page.pageId)
                    or element_id is not None
                ):
                    dropped.append(
                        Dropped(
                            reason=Reason.node_not_in_candidates,
                            featureId=feature_id,
                            nodeId=node_id or element_id,
                            quote=_str_or_none(quote),
                        )
                    )
                    continue
                else:
                    evidence = _verified(
                        quote,
                        context,
                        node_id=node_id,
                        element_id=element_id,
                        ui_text_only=display_without_chain,
                    )
                if chain_id is None:
                    warnings.append(
                        RequirementWarning(
                            code="evidence_ref_missing",
                            severity=Severity.warning,
                            message="功能点规则缺少链路锚点，已使用页面级逐字校验。",
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
    *,
    available: bool = True,
    parsed_override: dict[str, object] | None = None,
) -> tuple[list[FieldMapping], list[Dropped], list[RequirementWarning] | None]:
    if not available:
        return [], [], [
            RequirementWarning(
                code="field_mapping_evidence_truncated",
                severity=Severity.warning,
                message="字段映射推断已跳过：证据预算裁剪后 forms 或 apiFacts 不完整。",
            )
        ]
    parsed = parsed_override if parsed_override is not None else await _call_json(
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
                api, quote, contexts, element_id=element_id,
                ledger_only=any(context.ledger is not None for context in contexts),
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
    api: str, quote: str | None, contexts: list[_PageContext], *, element_id: str | None,
    ledger_only: bool = False,
) -> tuple[VerifiedEvidence | None, str | None]:
    """逐个候选引文尝试定位，且限制到模型声明的表单元素。"""
    if element_id is None:
        return None, None
    for candidate in _mapping_quote_candidates(quote):
        evidence, page_id = _locate_mapping_quote(
            api, candidate, contexts, element_id=element_id, ledger_only=ledger_only
        )
        if evidence is not None:
            return evidence, page_id
    return None, None


def _locate_mapping_quote(
    api: str, quote: str | None, contexts: list[_PageContext], *, element_id: str,
    ledger_only: bool = False,
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
        if not any(candidate_url == url for _, candidate_url in context.api_urls):
            continue
        if not any(source.node.id == element_id for source in context.material.elements):
            continue
        api_node_ids = [
            node_id for node_id, api_node in context.material.api_nodes.items()
            if api_node.attrs.get("url") == url
        ]
        # 表单字段引文必须归属于模型声明的元素节点。
        evidence = _verified(
            quote, context, node_id=element_id,
            required_category="form" if ledger_only else None,
        )
        if evidence is not None:
            return evidence, context.material.page.pageId
        # requestBody / API 文本必须归属于该 URL 对应的 API 节点。
        if ledger_only and not any(
            fact.page_id == context.material.page.pageId
            and fact.node_id in api_node_ids
            and fact.category == "apiFact"
            and fact.text == quote
            for fact in context.ledger.facts.values()
        ) if context.ledger is not None else False:
            continue
        for api_node_id in api_node_ids:
            evidence = _verified(
                quote, context, node_id=api_node_id,
                required_category="apiFact" if ledger_only else None,
            )
            if evidence is not None:
                return evidence, context.material.page.pageId
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
