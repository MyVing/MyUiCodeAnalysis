"""初始化阶段的内存仓储：重启即清空，不代表正式持久化能力。"""

from collections import deque
from threading import Lock

from app.contracts import IRGraph, RequirementMap
from app.schemas import AnalysisTask, Project, RequirementTask

MAX_GRAPHS_PER_PROJECT = 5
MAX_MAPS_PER_PROJECT = 20


class InMemoryProjectRepository:
    def __init__(self) -> None:
        self._items: dict[str, Project] = {}
        self._lock = Lock()

    def list(self) -> list[Project]:
        with self._lock:
            return sorted(self._items.values(), key=lambda item: item.created_at, reverse=True)

    def get(self, project_id: str) -> Project | None:
        with self._lock:
            return self._items.get(project_id)

    def add(self, project: Project) -> Project:
        with self._lock:
            self._items[project.id] = project
        return project


class InMemoryAnalysisRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, AnalysisTask] = {}
        self._graphs: dict[str, dict[str, IRGraph]] = {}
        # 每项目的图产物保留顺序，用于 get_latest_graph 与有界淘汰
        self._graph_order: dict[str, deque[str]] = {}
        self._lock = Lock()

    def add_task(self, task: AnalysisTask) -> AnalysisTask | None:
        with self._lock:
            conflict = any(
                item.project_id == task.project_id and item.status in ("queued", "running")
                for item in self._tasks.values()
            )
            if conflict:
                return None
            self._tasks[task.id] = task
            return task

    def get_task(self, task_id: str) -> AnalysisTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update_task(self, task: AnalysisTask) -> AnalysisTask:
        with self._lock:
            self._tasks[task.id] = task
            return task

    def list_by_project(self, project_id: str) -> list[AnalysisTask]:
        with self._lock:
            items = [item for item in self._tasks.values() if item.project_id == project_id]
            return sorted(items, key=lambda item: item.created_at, reverse=True)

    def save_graph(self, project_id: str, graph: IRGraph) -> None:
        analysis_id = graph.analysisId
        if analysis_id is None:
            raise ValueError("分析产物缺少 analysisId，无法入库")
        with self._lock:
            graphs = self._graphs.setdefault(project_id, {})
            order = self._graph_order.setdefault(project_id, deque())
            if analysis_id in order:
                order.remove(analysis_id)
            graphs[analysis_id] = graph
            order.append(analysis_id)
            while len(order) > MAX_GRAPHS_PER_PROJECT:
                oldest = order.popleft()
                graphs.pop(oldest, None)

    def get_graph(self, project_id: str, analysis_id: str) -> IRGraph | None:
        with self._lock:
            return self._graphs.get(project_id, {}).get(analysis_id)

    def get_latest_graph(self, project_id: str) -> IRGraph | None:
        with self._lock:
            order = self._graph_order.get(project_id)
            if not order:
                return None
            return self._graphs[project_id].get(order[-1])


def create_project_repository() -> InMemoryProjectRepository:
    return InMemoryProjectRepository()


def create_analysis_repository() -> InMemoryAnalysisRepository:
    return InMemoryAnalysisRepository()


class InMemoryRequirementRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, RequirementTask] = {}
        self._maps: dict[str, RequirementMap] = {}
        # 任务 id → requirementId：定位产物按任务取用，避免前端拼 id
        self._map_by_task: dict[str, str] = {}
        self._map_order: dict[str, deque[str]] = {}
        self._lock = Lock()

    def add_task(self, task: RequirementTask) -> RequirementTask:
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get_task(self, task_id: str) -> RequirementTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update_task(self, task: RequirementTask) -> RequirementTask:
        with self._lock:
            self._tasks[task.id] = task
        return task

    def list_by_project(self, project_id: str) -> list[RequirementTask]:
        with self._lock:
            items = [item for item in self._tasks.values() if item.project_id == project_id]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def save_map(self, requirement_map: RequirementMap) -> None:
        with self._lock:
            project_id = requirement_map.projectId
            order = self._map_order.setdefault(project_id, deque())
            if requirement_map.requirementId in order:
                order.remove(requirement_map.requirementId)
            self._maps[requirement_map.requirementId] = requirement_map
            order.append(requirement_map.requirementId)
            while len(order) > MAX_MAPS_PER_PROJECT:
                oldest = order.popleft()
                self._maps.pop(oldest, None)
                for task_id, mapped in list(self._map_by_task.items()):
                    if mapped == oldest:
                        del self._map_by_task[task_id]

    def link_task(self, task_id: str, requirement_id: str) -> None:
        with self._lock:
            self._map_by_task[task_id] = requirement_id

    def get_map(self, requirement_id: str) -> RequirementMap | None:
        with self._lock:
            return self._maps.get(requirement_id)

    def get_map_by_task(self, task_id: str) -> RequirementMap | None:
        with self._lock:
            requirement_id = self._map_by_task.get(task_id)
            if requirement_id is None:
                return None
            return self._maps.get(requirement_id)


def create_requirement_repository() -> InMemoryRequirementRepository:
    return InMemoryRequirementRepository()
