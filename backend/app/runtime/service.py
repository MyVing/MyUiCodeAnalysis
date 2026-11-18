"""运行时采集服务：任务生命周期 + 采集编排 + 合并图落库，与 requirement 同构。

任务流转 queued → running → succeeded / failed；前置校验同步拒绝（无启动来源 /
根路径缺失 / 指定的分析产物不存在），用户立刻拿到可读错误而不是等一个必然失败的任务。
采集成功后若存在静态图，自动产出合并图（kind='merged'）并落库。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from uuid import uuid4

from app.contracts import RuntimeTrace
from app.core.config import Settings
from app.runtime.merge import flatten_component_tree, merge_static_with_runtime
from app.runtime.runner import RuntimeWorkerRequest, RuntimeWorkerRunner
from app.schemas import Project, RuntimeStart, RuntimeTask
from app.storage.repositories import AnalysisRepository
from app.storage.runtime import RuntimeRepository

logger = logging.getLogger(__name__)


class RuntimePreconditionError(Exception):
    """前置条件不满足：缺启动来源 / 根路径缺失 / 指定的分析产物不存在。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RuntimeService:
    def __init__(
        self,
        repository: RuntimeRepository,
        analyses: AnalysisRepository,
        runner: RuntimeWorkerRunner,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._analyses = analyses
        self._runner = runner
        self._settings = settings
        # asyncio.Task 弱引用会被事件循环 GC，必须持有强引用直到完成
        self._background: set[asyncio.Task[None]] = set()

    async def list_tasks(self, project_id: str) -> list[RuntimeTask]:
        return await self._repository.list_by_project(project_id)

    async def get_task(self, task_id: str) -> RuntimeTask | None:
        return await self._repository.get_task(task_id)

    async def get_trace_by_task(self, task_id: str) -> RuntimeTrace | None:
        return await self._repository.get_trace_by_task(task_id)

    async def start(self, project: Project, payload: RuntimeStart) -> RuntimeTask:
        # 前置校验必须在建任务之前：否则用户要先等一个必然失败的任务
        if payload.url is None and payload.command is None:
            raise RuntimePreconditionError(
                "missing_launch_source",
                "必须提供 url（访问已在运行的页面）或 command（托管启动 dev server）之一。",
            )
        if payload.command is not None and not os.path.isdir(project.root_path):
            raise RuntimePreconditionError(
                "root_path_missing",
                f"项目根路径不存在或不是目录：{project.root_path}",
            )
        if payload.analysis_id is not None:
            graph = await self._analyses.get_graph(project.id, payload.analysis_id)
            if graph is None:
                raise RuntimePreconditionError(
                    "analysis_not_found",
                    f"分析产物 {payload.analysis_id} 不存在，请先完成一次分析。",
                )

        task = RuntimeTask(
            id=f"collect_{uuid4().hex[:12]}",
            project_id=project.id,
            trace_id=f"trace_{uuid4().hex[:12]}",
            status="queued",
            command=payload.command,
            url=payload.url,
            analysis_id=payload.analysis_id,
            settle_ms=payload.settle_ms,
            created_at=datetime.now(UTC),
        )
        await self._repository.add_task(task)

        background = asyncio.get_running_loop().create_task(self._execute(project, task))
        self._background.add(background)
        background.add_done_callback(self._background.discard)
        return task

    async def _execute(self, project: Project, task: RuntimeTask) -> None:
        # 沿用仓储返回的快照：否则后续全字段更新会用 started_at=None 覆盖刚写入的值
        task = await self._repository.update_task(
            task.model_copy(update={"status": "running", "started_at": datetime.now(UTC)})
        )
        request = RuntimeWorkerRequest(
            project_id=project.id,
            root_path=project.root_path,
            trace_id=task.trace_id,
            analysis_id=task.analysis_id,
            url=task.url,
            command=task.command,
            settle_ms=(
                task.settle_ms
                if task.settle_ms is not None
                else self._settings.runtime_settle_ms
            ),
        )
        try:
            outcome = await self._runner.run(
                request, self._settings.runtime_worker_timeout_seconds
            )
        except Exception as error:  # noqa: BLE001 - 任何异常都要落成可见的失败任务
            logger.exception("运行时采集任务执行异常：%s", task.id)
            await self._repository.update_task(
                task.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": datetime.now(UTC),
                        "error_code": "runtime_dispatch_failed",
                        "error_message": str(error),
                    }
                )
            )
            return

        if not outcome.ok or outcome.trace is None:
            await self._repository.update_task(
                task.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": datetime.now(UTC),
                        "error_code": outcome.error_code or "collect_failed",
                        "error_message": outcome.error_message,
                    }
                )
            )
            return

        trace = outcome.trace
        await self._repository.save_trace(trace)
        await self._merge_into_graph(project, task.analysis_id, trace)
        await self._repository.update_task(
            task.model_copy(
                update={
                    "status": "succeeded",
                    "finished_at": datetime.now(UTC),
                    "component_count": len(flatten_component_tree(trace.componentTree or [])),
                    "network_count": len(trace.networkEvents or []),
                    "console_count": len(trace.consoleEvents or []),
                    "interaction_count": len(trace.interactions or []),
                }
            )
        )

    async def _merge_into_graph(
        self, project: Project, analysis_id: str | None, trace: RuntimeTrace
    ) -> None:
        """采集成功后把运行时证据并入静态图；无静态图或合并失败都不影响任务成功。"""
        try:
            if analysis_id is not None:
                graph = await self._analyses.get_graph(project.id, analysis_id)
            else:
                graph = await self._analyses.get_latest_graph(project.id)
            if graph is None:
                logger.info("项目 %s 无静态图，跳过合并", project.id)
                return
            if graph.meta is not None and graph.meta.sample is True:
                logger.info("项目 %s 最新图为示例数据，跳过合并", project.id)
                return
            merged = merge_static_with_runtime(graph, trace)
            await self._analyses.save_merged_graph(project.id, merged)
        except Exception:  # noqa: BLE001 - 合并是增值步骤，不能让采集任务因此失败
            logger.exception("合并运行时证据到静态图失败：项目 %s", project.id)
