"""需求定位服务：任务生命周期 + 定位产物装配。

任务流转与分析任务一致（queued → running → succeeded / failed），前端可复用同一套
轮询逻辑。前置校验同步拒绝（无图 / 图是示例数据 / analysisId 不存在 / 模型未配置），
这样用户立刻拿到可读的错误，而不是先看到任务、再等它失败。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.contracts import IRGraph, RequirementMap
from app.core.config import Settings
from app.requirement.llm import LLMClient
from app.requirement.orchestrator import RequirementOrchestrator
from app.schemas import Project, RequirementLocateStart, RequirementTask
from app.storage.repositories import AnalysisRepository
from app.storage.requirement import RequirementRepository
from app.storage.runtime import RuntimeRepository

logger = logging.getLogger(__name__)


class RequirementPreconditionError(Exception):
    """前置条件不满足：无图 / 图为示例数据 / 指定的图不存在 / 模型未配置。"""

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
        llm: LLMClient | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        runtime: RuntimeRepository | None = None,
    ) -> None:
        self._repository = repository
        self._analyses = analyses
        self._settings = settings
        self._llm = llm
        self._checkpointer = checkpointer
        self._runtime = runtime
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
        # 沿用仓储返回的快照：否则后续全字段更新会用 started_at=None 覆盖刚写入的值
        task = await self._repository.update_task(
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
        """编排一次定位：dry-run 只跑确定性召回，非 dry-run 由图接模型判定。

        模型层失败（调用不通 / 输出无效）在图内降级为确定性结果 + 显式告警，
        不炸任务；编排层自身的意外异常仍由 _execute 落成 failed。

        画像按图版本取：图换了版本就拿不到旧画像（`get_page_profiles` 用分析 id
        精确匹配），此时定位照常工作，只是 prompt 里少一句页面描述。
        """
        profile = None
        if not task.dry_run and graph.analysisId is not None:
            try:
                profile = await self._analyses.get_page_profiles(project.id, graph.analysisId)
            except Exception:  # noqa: BLE001 - 读画像失败不该让定位失败
                logger.exception("读取页面画像失败：project=%s", project.id)
        # 运行时采集产物（可选）：trace 的请求 URL + 页面 URL 同时命中时，
        # 接口种子的归属置信度抬到 0.95。读不到就静默跳过。
        trace = None
        if self._runtime is not None:
            try:
                trace = await self._runtime.get_latest_trace(project.id)
            except Exception:  # noqa: BLE001 - 读 trace 失败不该让定位失败
                logger.exception("读取运行时采集产物失败：project=%s", project.id)
        orchestrator = RequirementOrchestrator(self._settings, self._llm, self._checkpointer)
        return await orchestrator.run(project, task, graph, profile, trace)
