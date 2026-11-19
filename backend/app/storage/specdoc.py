"""规格文档装配的仓储：任务状态 + 产物（SpecDocMap）。

同一份 RequirementMap 可多次生成规格（每次一个 SpecDocTask），因此产物按
specDocId（= 任务 id）存取，与 requirement 仓储「map 按 requirementId 存」
的键位不同。
"""

from typing import Protocol

from app.contracts.specdoc import SpecDocMap
from app.schemas import SpecDocTask


class SpecDocRepository(Protocol):
    async def add_task(self, task: SpecDocTask) -> SpecDocTask: ...

    async def get_task(self, task_id: str) -> SpecDocTask | None: ...

    async def update_task(self, task: SpecDocTask) -> SpecDocTask: ...

    async def list_by_project(self, project_id: str) -> list[SpecDocTask]: ...

    async def save_doc(self, spec_doc: SpecDocMap) -> None: ...

    async def link_task(self, task_id: str, spec_doc_id: str) -> None:
        """把产物绑定到任务：前端只知道 taskId，不该被迫记住两个 id。"""

    async def get_doc(self, spec_doc_id: str) -> SpecDocMap | None: ...

    async def get_doc_by_task(self, task_id: str) -> SpecDocMap | None: ...

    async def purge_project(self, project_id: str) -> None:
        """清空该项目的装配任务与产物（MySQL 靠外键级联，内存仓储显式清）。"""
