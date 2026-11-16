"""仓储接口：存储实现可替换，API 与前端不感知具体数据库。

全部为异步签名：MySQL 实现天然 async，内存实现以 asyncio.Lock 保护，
避免同步 Protocol 被迫用线程池桥接。
"""

from typing import Protocol

from app.contracts import IRGraph
from app.schemas import AnalysisTask, Project


class ProjectRepository(Protocol):
    async def list(self) -> list[Project]: ...

    async def get(self, project_id: str) -> Project | None: ...

    async def add(self, project: Project) -> Project: ...


class AnalysisRepository(Protocol):
    async def add_task(self, task: AnalysisTask) -> AnalysisTask | None:
        """登记新任务；该项目已有 queued/running 任务时返回 None（并发冲突）。"""

    async def get_task(self, task_id: str) -> AnalysisTask | None: ...

    async def update_task(self, task: AnalysisTask) -> AnalysisTask: ...

    async def list_by_project(self, project_id: str) -> list[AnalysisTask]: ...

    async def save_graph(self, project_id: str, graph: IRGraph) -> None: ...

    async def get_graph(self, project_id: str, analysis_id: str) -> IRGraph | None: ...

    async def get_latest_graph(self, project_id: str) -> IRGraph | None: ...
