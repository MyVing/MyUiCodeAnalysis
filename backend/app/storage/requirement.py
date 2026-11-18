"""需求定位的仓储：任务状态 + 定位产物（RequirementMap）。

与图产物分开存放是有意的：RequirementMap 是 AI 推断（置信度 ≤0.6），IRGraph 是
语法事实。两者同库不同集合，避免「把推断当事实」这类错误在存储层被掩盖。
"""

from typing import Protocol

from app.contracts import RequirementMap
from app.schemas import RequirementTask


class RequirementRepository(Protocol):
    async def add_task(self, task: RequirementTask) -> RequirementTask:
        """登记新任务。需求定位允许并发（不占 Worker 子进程），故没有冲突返回。"""

    async def get_task(self, task_id: str) -> RequirementTask | None: ...

    async def update_task(self, task: RequirementTask) -> RequirementTask: ...

    async def list_by_project(self, project_id: str) -> list[RequirementTask]: ...

    async def save_map(self, requirement_map: RequirementMap) -> None: ...

    async def link_task(self, task_id: str, requirement_id: str) -> None:
        """把定位产物绑定到任务：前端只知道 taskId，不该被迫记住两个 id。"""

    async def get_map(self, requirement_id: str) -> RequirementMap | None: ...

    async def get_map_by_task(self, task_id: str) -> RequirementMap | None: ...

    async def purge_project(self, project_id: str) -> None:
        """清空该项目的定位任务与产物（MySQL 靠外键级联，内存仓储显式清）。"""
