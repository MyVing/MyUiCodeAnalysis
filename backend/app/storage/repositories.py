"""仓储接口：存储实现可替换，API 与前端不感知具体数据库。"""

from typing import Protocol

from app.contracts import IRGraph
from app.schemas import AnalysisTask, Project


class ProjectRepository(Protocol):
    def list(self) -> list[Project]: ...

    def get(self, project_id: str) -> Project | None: ...

    def add(self, project: Project) -> Project: ...


class AnalysisRepository(Protocol):
    def add_task(self, task: AnalysisTask) -> AnalysisTask | None:
        """登记新任务；该项目已有 queued/running 任务时返回 None（并发冲突）。"""

    def get_task(self, task_id: str) -> AnalysisTask | None: ...

    def update_task(self, task: AnalysisTask) -> AnalysisTask: ...

    def list_by_project(self, project_id: str) -> list[AnalysisTask]: ...

    def save_graph(self, project_id: str, graph: IRGraph) -> None: ...

    def get_graph(self, project_id: str, analysis_id: str) -> IRGraph | None: ...

    def get_latest_graph(self, project_id: str) -> IRGraph | None: ...
