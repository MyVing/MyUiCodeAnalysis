"""规格文档装配服务：任务生命周期 + 产物落库。

任务流转与需求定位一致（queued → running → succeeded / failed），前端复用
同一套轮询逻辑。前置校验同步拒绝（定位产物不存在 / 产物对应的图已不在），
让用户立刻拿到可读错误，而不是先看任务再等它失败。

与 RequirementLocateService 的一个刻意差异：模型未配置不拒绝启动——
确定性章节（要素 / 事件 / 接口映射）不依赖模型，推断章节留空 + 告警
比「整单拒绝」有用得多。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from app.contracts import IRGraph
from app.contracts.requirement import RequirementMap
from app.contracts.specdoc import SpecDocMap
from app.core.config import Settings
from app.requirement.llm import LLMClient
from app.schemas import Project, SpecDocStart, SpecDocTask
from app.specdoc.orchestrator import SpecDocOrchestrator
from app.storage.repositories import AnalysisRepository
from app.storage.requirement import RequirementRepository
from app.storage.specdoc import SpecDocRepository

logger = logging.getLogger(__name__)


class SpecDocPreconditionError(Exception):
    """前置条件不满足：定位产物不存在 / 产物对应的图已不可用。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SpecDocService:
    def __init__(
        self,
        repository: SpecDocRepository,
        requirements: RequirementRepository,
        analyses: AnalysisRepository,
        settings: Settings,
        llm: LLMClient | None = None,
    ) -> None:
        self._repository = repository
        self._requirements = requirements
        self._analyses = analyses
        self._settings = settings
        self._llm = llm
        # asyncio.Task 弱引用会被事件循环 GC，必须持有强引用直到完成
        self._background: set[asyncio.Task[None]] = set()

    async def list_tasks(self, project_id: str) -> list[SpecDocTask]:
        return await self._repository.list_by_project(project_id)

    async def get_task(self, task_id: str) -> SpecDocTask | None:
        return await self._repository.get_task(task_id)

    async def get_doc_by_task(self, task_id: str) -> SpecDocMap | None:
        return await self._repository.get_doc_by_task(task_id)

    async def start(self, project: Project, payload: SpecDocStart) -> SpecDocTask:
        # 前置校验必须在建任务之前：否则用户要先等一个必然失败的任务
        requirement_map = await self._requirements.get_map(payload.requirement_id)
        if requirement_map is None:
            raise SpecDocPreconditionError(
                "specdoc_map_not_found",
                f"需求定位产物 {payload.requirement_id} 不存在，请先完成一次需求定位。",
            )
        if requirement_map.projectId != project.id:
            raise SpecDocPreconditionError(
                "specdoc_map_not_found",
                f"需求定位产物 {payload.requirement_id} 不属于该项目。",
            )
        graph = await self._require_graph(project, requirement_map)

        task = SpecDocTask(
            id=f"stask_{uuid4().hex[:12]}",
            project_id=project.id,
            requirement_id=payload.requirement_id,
            status="queued",
            dry_run=payload.dry_run,
            created_at=datetime.now(UTC),
        )
        await self._repository.add_task(task)

        background = asyncio.get_running_loop().create_task(
            self._execute(project, task, requirement_map, graph)
        )
        self._background.add(background)
        background.add_done_callback(self._background.discard)
        return task

    async def _require_graph(
        self, project: Project, requirement_map: RequirementMap
    ) -> IRGraph:
        """定位产物必须能对回它的分析图：图换了/丢了，元素与链路无从核对。"""
        analysis_id = requirement_map.analysisId
        graph = (
            await self._analyses.get_graph(project.id, analysis_id)
            if analysis_id is not None
            else None
        )
        if graph is None:
            raise SpecDocPreconditionError(
                "specdoc_map_stale",
                "定位产物对应的分析图已不可用（图已更新或被清理），"
                "请先重新执行需求定位，再生成规格文档。",
            )
        return graph

    async def _execute(
        self,
        project: Project,
        task: SpecDocTask,
        requirement_map: RequirementMap,
        graph: IRGraph,
    ) -> None:
        # 沿用仓储返回的快照：否则后续全字段更新会用 started_at=None 覆盖刚写入的值
        task = await self._repository.update_task(
            task.model_copy(update={"status": "running", "started_at": datetime.now(UTC)})
        )
        try:
            spec_doc = await self._assemble(project, task, requirement_map, graph)
        except Exception as error:  # noqa: BLE001 - 任何异常都要落成可见的失败任务
            logger.exception("规格文档装配任务执行异常：%s", task.id)
            await self._repository.update_task(
                task.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": datetime.now(UTC),
                        "error_code": getattr(error, "code", "specdoc_failed"),
                        "error_message": str(error),
                    }
                )
            )
            return

        await self._repository.save_doc(spec_doc)
        await self._repository.link_task(task.id, spec_doc.specDocId)
        await self._repository.update_task(
            task.model_copy(
                update={
                    "status": "succeeded",
                    "finished_at": datetime.now(UTC),
                    "element_count": len(spec_doc.elements),
                    "event_count": len(spec_doc.events),
                    "rule_count": len(spec_doc.rules),
                    "warning_count": len(spec_doc.warnings or []),
                }
            )
        )

    async def _assemble(
        self,
        project: Project,
        task: SpecDocTask,
        requirement_map: RequirementMap,
        graph: IRGraph,
    ) -> SpecDocMap:
        orchestrator = SpecDocOrchestrator(self._settings, self._llm)
        return await orchestrator.run(project, task, requirement_map, graph)
