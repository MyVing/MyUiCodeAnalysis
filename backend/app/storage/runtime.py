"""运行时采集的仓储：任务状态 + 采集产物（RuntimeTrace）。

与 requirement 同构：trace_id 在建任务时就生成并写进任务行，产物按该 id 落库，
前端轮询 taskId 后按任务取 trace，不需要记住第二个 id。
"""

from typing import Protocol

from app.contracts import RuntimeTrace
from app.schemas import RuntimeTask


class RuntimeRepository(Protocol):
    async def add_task(self, task: RuntimeTask) -> RuntimeTask:
        """登记新任务。采集允许并发（每次独立进程 + 独立 trace），无冲突返回。"""

    async def get_task(self, task_id: str) -> RuntimeTask | None: ...

    async def update_task(self, task: RuntimeTask) -> RuntimeTask: ...

    async def list_by_project(self, project_id: str) -> list[RuntimeTask]: ...

    async def save_trace(self, trace: RuntimeTrace) -> None: ...

    async def get_trace(self, trace_id: str) -> RuntimeTrace | None: ...

    async def get_trace_by_task(self, task_id: str) -> RuntimeTrace | None: ...

    async def get_latest_trace(self, project_id: str) -> RuntimeTrace | None: ...

    async def purge_project(self, project_id: str) -> None:
        """清空该项目的采集任务与 trace（MySQL 靠外键级联，内存仓储显式清）。"""
