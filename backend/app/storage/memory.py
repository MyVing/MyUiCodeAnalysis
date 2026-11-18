"""初始化阶段的内存仓储：重启即清空，不代表正式持久化能力。

与 MySQL 实现共用同一套异步 Protocol；锁用 asyncio.Lock——事件循环内
不会有真正的并行写，但保持语义与真实 DB 实现一致（每调用一次一条路径）。
"""

import asyncio
import json
from collections import deque
from typing import Any

from app.contracts import IRGraph, RequirementMap, RuntimeTrace
from app.requirement.page_profile import ProfilePayload
from app.schemas import AnalysisTask, ExploreTask, Project, RequirementTask, RuntimeTask
from app.storage.repositories import DuplicateRemoteUrlError

MAX_GRAPHS_PER_PROJECT = 5
MAX_MAPS_PER_PROJECT = 20
MAX_TRACES_PER_PROJECT = 20
MAX_ARTIFACTS_PER_PROJECT = 20


class InMemoryProjectRepository:
    def __init__(self) -> None:
        self._items: dict[str, Project] = {}
        self._lock = asyncio.Lock()

    async def list(self) -> list[Project]:
        async with self._lock:
            return sorted(self._items.values(), key=lambda item: item.created_at, reverse=True)

    async def get(self, project_id: str) -> Project | None:
        async with self._lock:
            return self._items.get(project_id)

    async def get_by_remote(self, remote_url: str) -> Project | None:
        async with self._lock:
            for item in self._items.values():
                if item.remote_url == remote_url:
                    return item
            return None

    async def add(self, project: Project) -> Project:
        async with self._lock:
            if project.remote_url and any(
                item.remote_url == project.remote_url for item in self._items.values()
            ):
                raise DuplicateRemoteUrlError(project.remote_url)
            self._items[project.id] = project
        return project

    async def update(self, project: Project) -> Project:
        async with self._lock:
            self._items[project.id] = project
        return project

    async def delete(self, project_id: str) -> bool:
        async with self._lock:
            return self._items.pop(project_id, None) is not None


class InMemoryAnalysisRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, AnalysisTask] = {}
        # 图产物按「kind:analysisId」复合键存放：P2 起同一分析可有 static 与 merged
        # 两个版本，淘汰顺序按写入时间统一排序，get_latest_graph 因此天然 merged 优先
        self._graphs: dict[str, dict[str, IRGraph]] = {}
        self._graph_order: dict[str, deque[str]] = {}
        # 页画像按 analysisId 存放：与图同一版本口径，图被淘汰后画像也不再有用
        self._profiles: dict[str, dict[str, ProfilePayload]] = {}
        self._profile_order: dict[str, deque[str]] = {}
        self._lock = asyncio.Lock()

    async def add_task(self, task: AnalysisTask) -> AnalysisTask | None:
        async with self._lock:
            conflict = any(
                item.project_id == task.project_id and item.status in ("queued", "running")
                for item in self._tasks.values()
            )
            if conflict:
                return None
            self._tasks[task.id] = task
            return task

    async def get_task(self, task_id: str) -> AnalysisTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def update_task(self, task: AnalysisTask) -> AnalysisTask:
        async with self._lock:
            self._tasks[task.id] = task
            return task

    async def list_by_project(self, project_id: str) -> list[AnalysisTask]:
        async with self._lock:
            items = [item for item in self._tasks.values() if item.project_id == project_id]
            return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def save_graph(self, project_id: str, graph: IRGraph) -> None:
        await self._save_graph(project_id, "static", graph)

    async def save_merged_graph(self, project_id: str, graph: IRGraph) -> None:
        """合并图按 analysisId 覆盖：同一分析多次采集后只保留最新合并结果。"""
        await self._save_graph(project_id, "merged", graph)

    async def _save_graph(self, project_id: str, kind: str, graph: IRGraph) -> None:
        analysis_id = graph.analysisId
        if analysis_id is None:
            raise ValueError("分析产物缺少 analysisId，无法入库")
        async with self._lock:
            key = f"{kind}:{analysis_id}"
            graphs = self._graphs.setdefault(project_id, {})
            order = self._graph_order.setdefault(project_id, deque())
            if key in order:
                order.remove(key)
            graphs[key] = graph
            order.append(key)
            while len(order) > MAX_GRAPHS_PER_PROJECT:
                oldest = order.popleft()
                graphs.pop(oldest, None)

    async def get_graph(self, project_id: str, analysis_id: str) -> IRGraph | None:
        async with self._lock:
            return self._graphs.get(project_id, {}).get(f"static:{analysis_id}")

    async def get_merged_graph(self, project_id: str, analysis_id: str) -> IRGraph | None:
        async with self._lock:
            return self._graphs.get(project_id, {}).get(f"merged:{analysis_id}")

    async def get_latest_graph(self, project_id: str) -> IRGraph | None:
        async with self._lock:
            order = self._graph_order.get(project_id)
            if not order:
                return None
            return self._graphs[project_id].get(order[-1])

    async def save_page_profiles(self, project_id: str, payload: ProfilePayload) -> None:
        async with self._lock:
            profiles = self._profiles.setdefault(project_id, {})
            order = self._profile_order.setdefault(project_id, deque())
            analysis_id = payload.analysis_id
            if analysis_id in order:
                order.remove(analysis_id)
            profiles[analysis_id] = payload
            order.append(analysis_id)
            # 与图产物的淘汰口径一致：画像按图版本存放，图被淘汰后画像也不再有用
            while len(order) > MAX_GRAPHS_PER_PROJECT:
                profiles.pop(order.popleft(), None)

    async def get_page_profiles(
        self, project_id: str, analysis_id: str
    ) -> ProfilePayload | None:
        async with self._lock:
            return self._profiles.get(project_id, {}).get(analysis_id)

    async def purge_project(self, project_id: str) -> None:
        async with self._lock:
            self._tasks = {
                task_id: item
                for task_id, item in self._tasks.items()
                if item.project_id != project_id
            }
            self._graphs.pop(project_id, None)
            self._graph_order.pop(project_id, None)
            self._profiles.pop(project_id, None)
            self._profile_order.pop(project_id, None)


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
        self._lock = asyncio.Lock()

    async def add_task(self, task: RequirementTask) -> RequirementTask:
        async with self._lock:
            self._tasks[task.id] = task
        return task

    async def get_task(self, task_id: str) -> RequirementTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def update_task(self, task: RequirementTask) -> RequirementTask:
        async with self._lock:
            self._tasks[task.id] = task
            return task

    async def list_by_project(self, project_id: str) -> list[RequirementTask]:
        async with self._lock:
            items = [item for item in self._tasks.values() if item.project_id == project_id]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def save_map(self, requirement_map: RequirementMap) -> None:
        async with self._lock:
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

    async def link_task(self, task_id: str, requirement_id: str) -> None:
        async with self._lock:
            self._map_by_task[task_id] = requirement_id

    async def get_map(self, requirement_id: str) -> RequirementMap | None:
        async with self._lock:
            return self._maps.get(requirement_id)

    async def get_map_by_task(self, task_id: str) -> RequirementMap | None:
        async with self._lock:
            requirement_id = self._map_by_task.get(task_id)
            if requirement_id is None:
                return None
            return self._maps.get(requirement_id)

    async def purge_project(self, project_id: str) -> None:
        async with self._lock:
            task_ids = [
                task_id
                for task_id, item in self._tasks.items()
                if item.project_id == project_id
            ]
            for task_id in task_ids:
                del self._tasks[task_id]
                self._map_by_task.pop(task_id, None)
            self._maps = {
                requirement_id: item
                for requirement_id, item in self._maps.items()
                if item.projectId != project_id
            }
            self._map_order.pop(project_id, None)


def create_requirement_repository() -> InMemoryRequirementRepository:
    return InMemoryRequirementRepository()


class InMemoryRuntimeRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, RuntimeTask] = {}
        self._traces: dict[str, RuntimeTrace] = {}
        self._trace_order: dict[str, deque[str]] = {}
        self._lock = asyncio.Lock()

    async def add_task(self, task: RuntimeTask) -> RuntimeTask:
        async with self._lock:
            self._tasks[task.id] = task
        return task

    async def get_task(self, task_id: str) -> RuntimeTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def update_task(self, task: RuntimeTask) -> RuntimeTask:
        async with self._lock:
            self._tasks[task.id] = task
            return task

    async def list_by_project(self, project_id: str) -> list[RuntimeTask]:
        async with self._lock:
            items = [item for item in self._tasks.values() if item.project_id == project_id]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def save_trace(self, trace: RuntimeTrace) -> None:
        async with self._lock:
            project_id = trace.projectId
            order = self._trace_order.setdefault(project_id, deque())
            if trace.traceId in order:
                order.remove(trace.traceId)
            self._traces[trace.traceId] = trace
            order.append(trace.traceId)
            while len(order) > MAX_TRACES_PER_PROJECT:
                oldest = order.popleft()
                self._traces.pop(oldest, None)

    async def get_trace(self, trace_id: str) -> RuntimeTrace | None:
        async with self._lock:
            return self._traces.get(trace_id)

    async def get_trace_by_task(self, task_id: str) -> RuntimeTrace | None:
        # 两跳：task → trace_id → trace；与 MySQL 实现的 JOIN 语义一致
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return self._traces.get(task.trace_id)

    async def get_latest_trace(self, project_id: str) -> RuntimeTrace | None:
        async with self._lock:
            order = self._trace_order.get(project_id)
            if not order:
                return None
            return self._traces.get(order[-1])

    async def purge_project(self, project_id: str) -> None:
        async with self._lock:
            self._tasks = {
                task_id: item
                for task_id, item in self._tasks.items()
                if item.project_id != project_id
            }
            self._traces = {
                trace_id: item
                for trace_id, item in self._traces.items()
                if item.projectId != project_id
            }
            self._trace_order.pop(project_id, None)


def create_runtime_repository() -> InMemoryRuntimeRepository:
    return InMemoryRuntimeRepository()


class InMemoryExploreRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, ExploreTask] = {}
        # (artifact JSON, artifacts_dir)；artifact 按 task_id 一比一，按项目有界淘汰
        self._artifacts: dict[str, tuple[dict[str, Any], str]] = {}
        self._artifact_order: dict[str, deque[str]] = {}
        self._lock = asyncio.Lock()

    async def add_task(self, task: ExploreTask) -> ExploreTask:
        async with self._lock:
            self._tasks[task.id] = task
        return task

    async def get_task(self, task_id: str) -> ExploreTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def update_task(self, task: ExploreTask) -> ExploreTask:
        async with self._lock:
            self._tasks[task.id] = task
            return task

    async def list_by_project(self, project_id: str) -> list[ExploreTask]:
        async with self._lock:
            items = [item for item in self._tasks.values() if item.project_id == project_id]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def save_artifact(self, artifact: dict[str, Any], artifacts_dir: str) -> None:
        async with self._lock:
            task_id = str(artifact["taskId"])
            project_id = str(artifact["projectId"])
            self._artifacts[task_id] = (artifact, artifacts_dir)
            order = self._artifact_order.setdefault(project_id, deque())
            if task_id in order:
                order.remove(task_id)
            order.append(task_id)
            while len(order) > MAX_ARTIFACTS_PER_PROJECT:
                oldest = order.popleft()
                self._artifacts.pop(oldest, None)

    async def get_artifact(self, task_id: str) -> dict[str, Any] | None:
        async with self._lock:
            stored = self._artifacts.get(task_id)
            # 深拷贝语义：返回后调用方可自由修改，不影响仓储内状态
            return json.loads(json.dumps(stored[0])) if stored else None

    async def get_artifacts_dir(self, task_id: str) -> str | None:
        async with self._lock:
            stored = self._artifacts.get(task_id)
            return stored[1] if stored else None

    async def purge_project(self, project_id: str) -> None:
        async with self._lock:
            self._tasks = {
                task_id: item
                for task_id, item in self._tasks.items()
                if item.project_id != project_id
            }
            for task_id in [
                task_id
                for task_id, (artifact, _) in self._artifacts.items()
                if str(artifact.get("projectId")) == project_id
            ]:
                del self._artifacts[task_id]
            self._artifact_order.pop(project_id, None)


def create_explore_repository() -> InMemoryExploreRepository:
    return InMemoryExploreRepository()
