"""按需求采集：需求描述 → 定位页面 → 反查路由 → 逐页发起运行时采集。

把三个已有能力串成一条编排链（worker 层零改动）：
  1. P3 需求定位产出 RequirementMap.matches（pageNodeId 指向页面组件节点）；
  2. IR 图里 route →页面组件 的 renders 边反向回溯出路由路径；
  3. P2 运行时采集按 baseUrl + path 逐页发起（url 直连模式）。

确定性口径与静态分析一致：path 定不下来 / 含动态参数 / 没有路由边，一律
显式记 SkippedRoute，不猜 URL、不编造采集目标。
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from app.contracts import EdgeKind, IRGraph, NodeKind, RequirementMap
from app.core.config import Settings
from app.requirement.service import RequirementLocateService, RequirementPreconditionError
from app.runtime.service import RuntimePreconditionError, RuntimeService
from app.schemas import (
    Project,
    RequirementCollectStart,
    RequirementLocateStart,
    RequirementTask,
    RuntimeStart,
    RuntimeTask,
    SkippedRoute,
)
from app.storage.repositories import AnalysisRepository
from app.storage.requirement import RequirementRepository

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.3


class RequirementCollectPreconditionError(Exception):
    """前置条件不满足：缺输入 / 定位失败 / 图缺失 / 没有可采集路由。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RoutePlan:
    """成功解析出的采集目标。"""

    page_node_id: str
    route_path: str
    url: str


def resolve_route_urls(
    page_node_ids: list[str], graph: IRGraph, base_url: str
) -> tuple[list[RoutePlan], list[SkippedRoute]]:
    """把页面节点反查成采集 URL。纯函数，便于单测。

    反查规则：renders 边（route → 页面组件）→ route 节点的 attrs.path。
    与 navigate 分析器的建边口径一致：path 定不下来时本来就不建边，
    所以「没有 renders 边」就是「没有确定的路由路径」。
    """
    nodes = {node.id: node for node in graph.nodes}
    routes_by_target: dict[str, list[tuple[str, str]]] = {}
    for edge in graph.edges:
        if edge.kind is not EdgeKind.renders:
            continue
        route_node = nodes.get(edge.from_)
        if route_node is None or route_node.kind is not NodeKind.route:
            continue
        path = route_node.attrs.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        routes_by_target.setdefault(edge.to, []).append((edge.from_, path.strip()))

    plans: list[RoutePlan] = []
    skipped: list[SkippedRoute] = []
    seen_urls: set[str] = set()
    base = base_url.rstrip("/")

    for page_node_id in page_node_ids:
        candidates = routes_by_target.get(page_node_id, [])
        if not candidates:
            skipped.append(
                SkippedRoute(
                    pageNodeId=page_node_id,
                    reason="no_route_found",
                    message="IR 图中该页面组件没有 route →renders 边，无法确定访问路径",
                )
            )
            continue
        if len(candidates) > 1:
            skipped.append(
                SkippedRoute(
                    pageNodeId=page_node_id,
                    routePath=" / ".join(path for _, path in candidates),
                    reason="ambiguous_routes",
                    message=f"该页面被 {len(candidates)} 条路由渲染，无法确定唯一采集目标",
                )
            )
            continue
        path = candidates[0][1]
        if ":" in path:
            skipped.append(
                SkippedRoute(
                    pageNodeId=page_node_id,
                    routePath=path,
                    reason="dynamic_route_params",
                    message=f"路由 {path} 含动态参数，无法在无上下文的情况下确定具体 URL",
                )
            )
            continue
        url = f"{base}/{path.lstrip('/')}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        plans.append(RoutePlan(page_node_id=page_node_id, route_path=path, url=url))

    return plans, skipped


class RequirementCollectService:
    def __init__(
        self,
        requirements: RequirementRepository,
        analyses: AnalysisRepository,
        runtime_service: RuntimeService,
        requirement_service: RequirementLocateService,
        settings: Settings,
    ) -> None:
        self._requirements = requirements
        self._analyses = analyses
        self._runtime = runtime_service
        self._requirement = requirement_service
        self._settings = settings

    async def start(
        self, project: Project, payload: RequirementCollectStart
    ) -> tuple[list[RuntimeTask], list[SkippedRoute], str]:
        """发起按需求采集。返回（已创建的采集任务, 跳过的页面, 需求任务 id）。

        前置校验同步抛 RequirementCollectPreconditionError（API 层转 409）；
        采集任务本身与 RuntimeService.start 一致，在后台异步执行。
        """
        locate_task = await self._resolve_locate_task(project, payload)
        requirement_map = await self._require_map(locate_task)

        graph = await self._require_graph(project, requirement_map)
        page_node_ids: list[str] = []
        for match in requirement_map.matches:
            if match.pageNodeId not in page_node_ids:
                page_node_ids.append(match.pageNodeId)
        if not page_node_ids:
            raise RequirementCollectPreconditionError(
                "no_matched_pages",
                f"需求「{locate_task.requirement[:40]}…」没有定位到任何页面，无法采集。",
            )

        plans, skipped = resolve_route_urls(page_node_ids, graph, payload.base_url)
        if not plans:
            raise RequirementCollectPreconditionError(
                "no_collectable_routes",
                "所有匹配页面都无法解析出可访问的路由 URL："
                + "；".join(f"{item.page_node_id}: {item.reason}" for item in skipped),
            )

        tasks: list[RuntimeTask] = []
        for plan in plans:
            tasks.append(await self._start_runtime(project, requirement_map, plan, payload))
        return tasks, skipped, locate_task.id

    async def _resolve_locate_task(
        self, project: Project, payload: RequirementCollectStart
    ) -> RequirementTask:
        """requirement_task_id 直取；requirement 文本则跑一次定位并等待完成。"""
        if payload.requirement_task_id is not None:
            task = await self._requirements.get_task(payload.requirement_task_id)
            if task is None or task.project_id != project.id:
                raise RequirementCollectPreconditionError(
                    "requirement_task_not_found",
                    f"需求定位任务 {payload.requirement_task_id} 不存在或不属于该项目。",
                )
            if task.status != "succeeded":
                raise RequirementCollectPreconditionError(
                    "requirement_task_not_succeeded",
                    f"需求定位任务当前状态为 {task.status}，需先拿到成功的定位结果。",
                )
            return task

        if payload.requirement is None:
            raise RequirementCollectPreconditionError(
                "missing_requirement_source",
                "必须提供 requirement（需求描述）或 requirementTaskId（复用已有定位结果）之一。",
            )

        try:
            task = await self._requirement.start(
                project,
                RequirementLocateStart(
                    requirement=payload.requirement,
                    dry_run=payload.dry_run,
                ),
            )
        except RequirementPreconditionError as error:
            raise RequirementCollectPreconditionError(error.code, error.message) from None

        deadline = time.monotonic() + self._settings.requirement_collect_locate_timeout_seconds
        while time.monotonic() < deadline:
            current = await self._requirements.get_task(task.id)
            if current is None:
                break
            if current.status in ("succeeded", "failed"):
                if current.status == "failed":
                    raise RequirementCollectPreconditionError(
                        "requirement_locate_failed",
                        current.error_message or "需求定位任务失败。",
                    )
                return current
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        deadline = self._settings.requirement_collect_locate_timeout_seconds
        raise RequirementCollectPreconditionError(
            "requirement_locate_timeout",
            f"需求定位未在 {deadline:.0f}s 内完成。",
        )

    async def _require_map(self, locate_task: RequirementTask) -> RequirementMap:
        requirement_map = await self._requirements.get_map_by_task(locate_task.id)
        if requirement_map is None:
            raise RequirementCollectPreconditionError(
                "map_missing",
                f"需求定位任务 {locate_task.id} 没有关联的定位结果（map）。",
            )
        return requirement_map

    async def _require_graph(self, project: Project, requirement_map: RequirementMap) -> IRGraph:
        graph: IRGraph | None = None
        if requirement_map.analysisId is not None:
            graph = await self._analyses.get_graph(project.id, requirement_map.analysisId)
        if graph is None:
            graph = await self._analyses.get_latest_graph(project.id)
        if graph is None:
            raise RequirementCollectPreconditionError(
                "graph_missing",
                "项目没有可用的静态分析图，请先完成一次静态分析。",
            )
        return graph

    async def _start_runtime(
        self,
        project: Project,
        requirement_map: RequirementMap,
        plan: RoutePlan,
        payload: RequirementCollectStart,
    ) -> RuntimeTask:
        try:
            return await self._runtime.start(
                project,
                RuntimeStart(
                    url=plan.url,
                    analysis_id=requirement_map.analysisId,
                    settle_ms=payload.settle_ms,
                ),
            )
        except RuntimePreconditionError as error:
            raise RequirementCollectPreconditionError(error.code, error.message) from None
