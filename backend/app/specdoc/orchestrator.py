"""规格文档编排：collect → deterministic → infer → assemble。

与 requirement 链路的分工：RequirementOrchestrator 负责把需求「定位」到
页面与链路；本编排器消费定位产物，把命中的素材装配成规格文档。装配是
单向线性流水线，没有 judge 式的回环——不需要 LangGraph 的重放能力，
用普通的顺序编排 + 服务层状态机（同 requirement.service 的惯例）就够。

确定性章节永远产出（模型不可用只影响三个推断章节，dry-run 同理）：
一次模型抖动不该让用户连要素清单都拿不到。
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import UTC, datetime

from app.contracts import IRGraph, RequirementMap
from app.contracts.requirement import Dropped as RequirementDropped
from app.contracts.requirement import RequirementWarning, Severity
from app.contracts.specdoc import (
    ApiMapping,
    Element,
    Event,
    FieldMapping,
    Rule,
    SpecDocMap,
    Step,
)
from app.contracts.specdoc import (
    Dropped as DocDropped,
)
from app.contracts.specdoc import RequirementWarning as DocWarning
from app.core.config import Settings
from app.schemas import Project, SpecDocTask
from app.specdoc.apimap import build_api_mappings
from app.specdoc.collect import Materials, collect_materials, deterministic_field_mappings
from app.specdoc.elements import build_elements
from app.specdoc.events import build_events
from app.specdoc.infer import InferOutcome, infer_sections
from app.specdoc.llm import OpenAICompatibleToolLLM

SCHEMA_VERSION = "1.0"
logger = logging.getLogger(__name__)


class SpecDocOrchestrator:
    def __init__(
        self,
        settings: Settings,
        llm: OpenAICompatibleToolLLM | None,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._engine = engine

    async def run(
        self,
        project: Project,
        task: SpecDocTask,
        requirement_map: RequirementMap,
        graph: IRGraph,
    ) -> SpecDocMap:
        """跑一次装配。素材解析失败（无命中页面）时返回只带告警的空产物。"""
        materials = collect_materials(requirement_map, graph)
        requirement_warnings: list[RequirementWarning] = materials.warnings
        warnings: list[DocWarning] = _convert_warnings(requirement_warnings)

        events, api_mappings, elements = self._deterministic_sections(
            materials, warnings
        )
        events_by_page: dict[str, list[Event]] = defaultdict(list)
        for event in events:
            events_by_page[event.pageId].append(event)
        infer_outcome = await self._infer_sections(
            task, requirement_map.requirement, requirement_map, materials,
            events_by_page,
            project_id=project.id,
            analysis_id=graph.analysisId,
        )
        warnings.extend(_convert_warnings(infer_outcome.warnings or []))
        shadow_counts = getattr(infer_outcome, "shadow_counts", {})
        if shadow_counts:
            logger.info(
                "Specdoc inference shadow would_drop: task=%s counts=%s",
                task.id,
                shadow_counts,
            )
        warning_counts = Counter(item.code for item in infer_outcome.warnings or [])
        if warning_counts:
            logger.info(
                "Specdoc inference warnings: task=%s counts=%s",
                task.id,
                dict(sorted(warning_counts.items())),
            )
        deterministic_mappings = [] if task.dry_run else deterministic_field_mappings(materials)
        field_mappings = _merge_field_mappings(deterministic_mappings, infer_outcome.field_mappings)

        steps = self._number_steps(infer_outcome.steps)
        rules = self._number_rules(infer_outcome.rules)

        return SpecDocMap(
            schemaVersion=SCHEMA_VERSION,
            specDocId=task.id,
            projectId=project.id,
            requirementId=requirement_map.requirementId,
            analysisId=graph.analysisId,
            requirement=requirement_map.requirement,
            createdAt=datetime.now(UTC),
            dryRun=task.dry_run,
            pages=[material.page for material in materials.pages],
            steps=steps,
            elements=elements,
            events=events,
            rules=rules,
            fieldMappings=field_mappings,
            apiMappings=api_mappings,
            dropped=_convert_dropped(infer_outcome.dropped),
            warnings=warnings,
        )

    def _deterministic_sections(
        self,
        materials: Materials,
        warnings: list[DocWarning],
    ) -> tuple[list[Event], list[ApiMapping], list[Element]]:
        """三个确定性章节。事件先装配（要素清单要引用事件 id），再要素、再接口。

        collect / events 产出的是 requirement 契约的 Warning；产物契约（specdoc
        schema 生成物）有自己的 Warning 类——入产物前统一转换。
        """
        events: list[Event] = []
        api_mappings: list[ApiMapping] = []
        elements: list[Element] = []
        event_ids_by_element: dict[str, list[str]] = defaultdict(list)
        for material in materials.pages:
            page_events, page_warnings = build_events(material)
            for event in page_events:
                events.append(event)
                event_ids_by_element[event.elementId].append(event.id)
            warnings.extend(_convert_warnings(page_warnings))
            api_mappings.extend(build_api_mappings(material, page_events))
        for material in materials.pages:
            elements.extend(build_elements(material, event_ids_by_element))
        return events, api_mappings, elements

    async def _infer_sections(
        self,
        task: SpecDocTask,
        requirement: str,
        requirement_map: RequirementMap,
        materials: Materials,
        events_by_page: dict[str, list[Event]],
        *,
        project_id: str | None = None,
        analysis_id: str | None = None,
    ) -> InferOutcome | _SkippedOutcome:
        """三个推断章节。dry-run / 无模型时留空 + 显式告警，不静默。"""
        if task.dry_run:
            return _SkippedOutcome(
                warnings=_skipped_warning(
                    "dry_run_skipped_llm_sections",
                    "dry-run 模式：步骤 / 业务规则 / 字段映射三个推断章节未运行，"
                    "本次产物只含确定性章节。",
                )
            )
        if self._llm is None:
            return _SkippedOutcome(
                warnings=_skipped_warning(
                    "llm_unavailable",
                    "模型不可用：三个推断章节已留空，本次产物只含确定性章节。",
                )
            )
        if not materials.pages:
            return _SkippedOutcome()
        return await infer_sections(
            requirement,
            requirement_map,
            materials,
            self._llm,
            max_calls=self._settings.llm_max_calls_per_request,
            events_by_page=events_by_page,
            engine=self._engine,
            project_id=project_id,
            analysis_id=analysis_id,
        )

    def _number_steps(self, steps: list[Step]) -> list[Step]:
        """推断阶段按 featureId 顺序生成 step.{featureId}.{n}；装配阶段保留这些稳定 ID。"""
        return list(steps)

    def _number_rules(self, rules: list[Rule]) -> list[Rule]:
        """规则编号 fr.{featureId}.{n}；无 featureId 的页面级规则归 fr.page.{n}。"""
        counters: dict[str, int] = defaultdict(int)
        numbered: list[Rule] = []
        for rule in rules:
            key = rule.featureId or "page"
            counters[key] += 1
            numbered.append(rule.model_copy(update={"id": f"fr.{key}.{counters[key]}"}))
        return numbered


def _merge_field_mappings(
    deterministic: list[FieldMapping], inferred: list[FieldMapping]
) -> list[FieldMapping]:
    """确定性事实优先；模型仅补充静态分析未覆盖的映射。"""
    result = list(deterministic)
    keys = {(item.frontField, item.api, item.pageId) for item in deterministic}
    for item in inferred:
        key = (item.frontField, item.api, item.pageId)
        if key not in keys:
            result.append(item)
            keys.add(key)
    return result
class _SkippedOutcome:
    """推断章节被跳过时的空产物。字段形状与 InferOutcome 对齐，调用方无感知。"""

    def __init__(self, warnings: list[RequirementWarning] | None = None) -> None:
        self.steps: list[Step] = []
        self.rules: list[Rule] = []
        self.field_mappings: list[FieldMapping] = []
        self.dropped: list[RequirementDropped] = []
        self.warnings: list[RequirementWarning] = warnings or []
        self.shadow_counts: dict[str, int] = {}



def _skipped_warning(code: str, message: str) -> list[RequirementWarning]:
    return [
        RequirementWarning(
            code=code,
            severity=Severity.warning,
            message=message,
        )
    ]


def _convert_warnings(warnings: list[RequirementWarning] | None) -> list[DocWarning]:
    """requirement 契约的 Warning → specdoc 契约的 Warning（字段同形）。"""
    return [DocWarning(**item.model_dump()) for item in warnings or []]


def _convert_dropped(dropped: list[RequirementDropped] | None) -> list[DocDropped]:
    """requirement 契约的 Dropped → specdoc 契约的 Dropped（字段同形）。"""
    return [DocDropped(**item.model_dump()) for item in dropped or []]
