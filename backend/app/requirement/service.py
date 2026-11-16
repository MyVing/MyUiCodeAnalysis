"""需求定位服务：任务生命周期 + 定位产物装配。

任务流转与分析任务一致（queued → running → succeeded / failed），前端可复用同一套
轮询逻辑。前置校验同步拒绝（无图 / 图是示例数据 / analysisId 不存在），这样用户
立刻拿到可读的错误，而不是先看到任务、再等它失败。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from app.contracts import (
    Candidate,
    Feature,
    IRGraph,
    RequirementMap,
    RequirementWarning,
)

# Severity 用 requirement 契约自己的枚举：IR 契约里有同名但不同的类型，混用会让
# 等值比较莫名其妙地失败。
from app.contracts.requirement import Severity
from app.core.config import Settings
from app.requirement.features import decompose
from app.requirement.recall import recall
from app.schemas import Project, RequirementLocateStart, RequirementTask
from app.storage.repositories import AnalysisRepository
from app.storage.requirement import RequirementRepository

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


class RequirementPreconditionError(Exception):
    """前置条件不满足：无图 / 图为示例数据 / 指定的图不存在。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RequirementLocateService:
    def __init__(
        self,
        repository: RequirementRepository,
        analyses: AnalysisRepository,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._analyses = analyses
        self._settings = settings
        # asyncio.Task 弱引用会被事件循环 GC，必须持有强引用直到完成
        self._background: set[asyncio.Task[None]] = set()

    async def list_tasks(self, project_id: str) -> list[RequirementTask]:
        return await self._repository.list_by_project(project_id)

    async def get_task(self, task_id: str) -> RequirementTask | None:
        return await self._repository.get_task(task_id)

    async def get_map(self, requirement_id: str) -> RequirementMap | None:
        return await self._repository.get_map(requirement_id)

    async def get_map_by_task(self, task_id: str) -> RequirementMap | None:
        return await self._repository.get_map_by_task(task_id)

    async def start(self, project: Project, payload: RequirementLocateStart) -> RequirementTask:
        # 前置校验必须在建任务之前：否则用户要先等一个必然失败的任务
        graph = await self._require_graph(project, payload.analysis_id)
        if payload.dry_run is False and not self._settings.llm_ready:
            raise RequirementPreconditionError(
                "llm_unavailable",
                "未配置模型，当前只能使用 dryRun（只跑确定性召回）。"
                "请设置 APP_LLM_ENABLED=true 与 APP_LLM_API_KEY 后重试。",
            )

        task = RequirementTask(
            id=f"rtask_{uuid4().hex[:12]}",
            project_id=project.id,
            requirement_id=f"req_{uuid4().hex[:12]}",
            status="queued",
            dry_run=payload.dry_run,
            requirement=payload.requirement,
            analysis_id=graph.analysisId,
            created_at=datetime.now(UTC),
        )
        await self._repository.add_task(task)

        background = asyncio.get_running_loop().create_task(self._execute(project, task, graph))
        self._background.add(background)
        background.add_done_callback(self._background.discard)
        return task

    async def _require_graph(
        self, project: Project, analysis_id: str | None
    ) -> IRGraph:
        if analysis_id is not None:
            graph = await self._analyses.get_graph(project.id, analysis_id)
            if graph is None:
                raise RequirementPreconditionError(
                    "analysis_not_found",
                    f"分析产物 {analysis_id} 不存在，请先完成一次分析。",
                )
        else:
            graph = await self._analyses.get_latest_graph(project.id)
            if graph is None:
                raise RequirementPreconditionError(
                    "graph_missing",
                    "该项目还没有分析产物，请先执行一次分析。",
                )

        if graph.meta is not None and graph.meta.sample is True:
            raise RequirementPreconditionError(
                "graph_is_sample",
                "当前图是示例数据，无法用于需求定位。请先执行一次真实分析。",
            )
        return graph

    async def _execute(
        self, project: Project, task: RequirementTask, graph: IRGraph
    ) -> None:
        await self._repository.update_task(
            task.model_copy(update={"status": "running", "started_at": datetime.now(UTC)})
        )
        try:
            requirement_map = await self._locate(project, task, graph)
        except Exception as error:  # noqa: BLE001 - 任何异常都要落成可见的失败任务
            logger.exception("需求定位任务执行异常：%s", task.id)
            await self._repository.update_task(
                task.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": datetime.now(UTC),
                        "error_code": getattr(error, "code", "requirement_failed"),
                        "error_message": str(error),
                    }
                )
            )
            return

        await self._repository.save_map(requirement_map)
        await self._repository.link_task(task.id, requirement_map.requirementId)
        await self._repository.update_task(
            task.model_copy(
                update={
                    "status": "succeeded",
                    "finished_at": datetime.now(UTC),
                    "candidate_count": len(requirement_map.candidates or []),
                    "match_count": len(requirement_map.matches),
                    "warning_count": len(requirement_map.warnings or []),
                }
            )
        )

    async def _locate(
        self, project: Project, task: RequirementTask, graph: IRGraph
    ) -> RequirementMap:
        """dry-run 路径：纯确定性召回，不调模型。判定路径在阶段 3 接入。"""
        if not task.dry_run:
            raise RequirementPreconditionError(
                "llm_unavailable",
                "模型判定路径尚未接入（阶段 3），当前请使用 dryRun。",
            )

        features = decompose(task.requirement)
        warnings: list[RequirementWarning] = []
        top_k = self._settings.llm_max_candidates

        # 按功能点分别召回再合并：先合并需求文本会让「详情」与「列表」互相稀释，
        # 后排的页面被前一个功能点的高分挤掉。
        merged: dict[str, Candidate] = {}
        for feature in features:
            result = recall(graph, feature.terms, top_k=top_k)
            warnings.extend(result.warnings)
            for detail in result.candidates:
                existing = merged.get(detail.candidate.nodeId)
                if existing is None or detail.candidate.score > existing.score:
                    merged[detail.candidate.nodeId] = detail.candidate

        candidates = sorted(merged.values(), key=lambda item: (-item.score, item.nodeId))[:top_k]

        return RequirementMap(
            schemaVersion=SCHEMA_VERSION,
            requirementId=task.requirement_id,
            projectId=project.id,
            analysisId=graph.analysisId,
            requirement=task.requirement,
            createdAt=datetime.now(UTC),
            dryRun=True,
            features=[
                Feature(id=feature.id, text=feature.text, keywords=feature.keywords())
                for feature in features
            ],
            matches=[],
            candidates=candidates,
            warnings=_sorted_warnings(warnings),
        )


def _dedupe_warnings(warnings: list[RequirementWarning]) -> list[RequirementWarning]:
    """同一告警在同一份产物里只保留一条：按功能点重复召回会产生完全相同的告警。"""
    seen: set[tuple[str, str]] = set()
    result: list[RequirementWarning] = []
    for warning in warnings:
        key = (warning.code, warning.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(warning)
    return result


def _sorted_warnings(warnings: list[RequirementWarning]) -> list[RequirementWarning]:
    order = {Severity.error: 0, Severity.warning: 1, Severity.info: 2}
    return sorted(
        _dedupe_warnings(warnings),
        key=lambda warning: (order[warning.severity], warning.code, warning.message),
    )
