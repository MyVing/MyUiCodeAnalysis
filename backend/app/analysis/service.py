"""分析编排服务：管理任务生命周期并调度 Worker 子进程。

任务流转：queued -> running -> succeeded / failed。
失败不静默：worker 报错、超时、根路径缺失都落成可见的 failed 任务。
"""

import asyncio
import logging
import os
from datetime import UTC, datetime
from uuid import uuid4

from app.analysis.runner import WorkerRequest, WorkerRunner
from app.contracts import IRGraph
from app.core.config import Settings
from app.requirement.llm import LLMClient
from app.requirement.profile import profile_pages
from app.schemas import AnalysisStart, AnalysisTask, Project
from app.storage.repositories import AnalysisRepository

logger = logging.getLogger(__name__)


class AnalysisConflictError(Exception):
    """该项目已有进行中的分析任务。"""


class AnalysisService:
    def __init__(
        self,
        repository: AnalysisRepository,
        runner: WorkerRunner,
        settings: Settings,
        llm: LLMClient | None = None,
    ) -> None:
        self._repository = repository
        self._runner = runner
        self._settings = settings
        # 画像用的模型客户端。为 None 或开关关闭时不生成画像，分析照常成功。
        self._llm = llm
        # asyncio.Task 弱引用会被事件循环 GC，必须持有强引用直到完成
        self._background: set[asyncio.Task[None]] = set()

    async def list_tasks(self, project_id: str) -> list[AnalysisTask]:
        return await self._repository.list_by_project(project_id)

    async def start(self, project: Project, payload: AnalysisStart | None = None) -> AnalysisTask:
        payload = payload or AnalysisStart()
        created = AnalysisTask(
            id=f"task_{uuid4().hex[:12]}",
            project_id=project.id,
            analysis_id=f"an_{uuid4().hex[:12]}",
            status="queued",
            created_at=datetime.now(UTC),
            requirement=payload.requirement,
            base_commit=payload.base_commit,
            target_commit=payload.target_commit,
        )
        task = await self._repository.add_task(created)
        if task is None:
            raise AnalysisConflictError(project.id)

        if not os.path.isdir(project.root_path):
            return await self._repository.update_task(
                task.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": datetime.now(UTC),
                        "error_code": "root_path_missing",
                        "error_message": f"项目根路径不存在或不是目录：{project.root_path}",
                    }
                )
            )

        background = asyncio.get_running_loop().create_task(self._execute(project, task))
        self._background.add(background)
        background.add_done_callback(self._background.discard)
        return task

    async def _execute(self, project: Project, task: AnalysisTask) -> None:
        # 必须沿用 update_task 的返回值：仓储返回的是持久化后的任务快照，
        # 若继续用入参 task（started_at 为 None）做后续全字段更新，
        # 会把刚写入的 started_at 覆盖回 NULL。
        task = await self._repository.update_task(
            task.model_copy(update={"status": "running", "started_at": datetime.now(UTC)})
        )
        request = WorkerRequest(
            project_id=project.id,
            root_path=project.root_path,
            analysis_id=task.analysis_id,
            framework=project.framework,
        )
        try:
            outcome = await self._runner.run(request, self._settings.worker_timeout_seconds)
        except Exception as error:
            # 编排自身的意外不能留下永远 running 的任务
            logger.exception("分析任务执行异常：%s", task.id)
            await self._repository.update_task(
                task.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": datetime.now(UTC),
                        "error_code": "analysis_dispatch_failed",
                        "error_message": str(error),
                    }
                )
            )
            return

        if outcome.ok and outcome.graph is not None:
            await self._repository.save_graph(project.id, outcome.graph)
            await self._repository.update_task(
                task.model_copy(
                    update={
                        "status": "succeeded",
                        "finished_at": datetime.now(UTC),
                        "node_count": len(outcome.graph.nodes),
                        "edge_count": len(outcome.graph.edges),
                        "warning_count": len(outcome.graph.warnings or []),
                    }
                )
            )
            self._schedule_profile(project, outcome.graph)
            return

        await self._repository.update_task(
            task.model_copy(
                update={
                    "status": "failed",
                    "finished_at": datetime.now(UTC),
                    "error_code": outcome.error_code or "invalid_ir",
                    "error_message": outcome.error_message,
                }
            )
        )

    def _schedule_profile(self, project: Project, graph: IRGraph) -> None:
        """分析成功后异步生成页面画像。

        放在后台任务而不是分析主流程里，因为画像是**可选的辅助线索**：
        多花一次模型调用，慢的是画像不是分析；失败也只是少一段描述。
        开关默认关，未配置模型时同样跳过——两者都不该让分析结果受影响。
        """
        if not self._settings.llm_profile_enabled or self._llm is None:
            return
        analysis_id = graph.analysisId
        if analysis_id is None:
            return
        background = asyncio.get_running_loop().create_task(
            self._generate_profile(project, graph, analysis_id)
        )
        self._background.add(background)
        background.add_done_callback(self._background.discard)

    async def _generate_profile(
        self, project: Project, graph: IRGraph, analysis_id: str
    ) -> None:
        assert self._llm is not None
        try:
            outcome = await profile_pages(
                graph,
                self._llm,
                analysis_id=analysis_id,
                model=self._settings.llm_model,
                max_calls=self._settings.llm_max_calls_per_request,
                max_pages=self._settings.llm_max_pages or None,
            )
        except Exception:  # noqa: BLE001 - 画像失败绝不能冒泡到分析任务
            logger.exception("页面画像生成失败：project=%s analysis=%s", project.id, analysis_id)
            return
        if outcome.degraded:
            # 降级也落库：告警本身是「为什么这个项目没有描述」的唯一记录
            logger.warning(
                "页面画像降级：project=%s analysis=%s %s",
                project.id,
                analysis_id,
                ";".join(outcome.payload.warnings),
            )
        try:
            await self._repository.save_page_profiles(project.id, outcome.payload)
        except Exception:  # noqa: BLE001
            logger.exception("页面画像落库失败：project=%s analysis=%s", project.id, analysis_id)
