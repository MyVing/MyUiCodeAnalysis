"""接口 → 页面反向检索端点（技术设计 6.11）：只读、零模型。

「这个接口在哪个页面被调用」不需要 AI——图里本来就有 `requests` 边。索引在
每张分析图上构建一次，按 analysisId 内存缓存；图更新（节点 / 边数变化）即
失效重建，不落库。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import AnalysisRepositoryDep, ProjectServiceDep, get_runtime_repository
from app.api.v1.projects import require_project
from app.contracts import IRGraph, RequirementWarning
from app.contracts.requirement import Severity
from app.ir.api_usage import ApiUsageIndex, apply_runtime_backing
from app.ir.sample import build_sample_graph
from app.schemas import ApiUsagePage, ApiUsageResult
from app.storage.runtime import RuntimeRepository

router = APIRouter(prefix="/projects", tags=["api-usage"])

# 索引缓存：key = analysisId + 节点/边数（同 id 重跑时图已变，计数不同自然失效）。
# 示例图每次请求重建（构建成本低，且与具体项目相关），不入缓存。
_INDEX_CACHE: OrderedDict[str, ApiUsageIndex] = OrderedDict()
_INDEX_CACHE_LIMIT = 32


def _index_for(graph: IRGraph) -> ApiUsageIndex:
    key = graph.analysisId
    if key is None or (graph.meta is not None and graph.meta.sample):
        return ApiUsageIndex(graph)
    cache_key = f"{key}:{len(graph.nodes)}/{len(graph.edges)}"
    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None:
        _INDEX_CACHE.move_to_end(cache_key)
        return cached
    index = ApiUsageIndex(graph)
    _INDEX_CACHE[cache_key] = index
    while len(_INDEX_CACHE) > _INDEX_CACHE_LIMIT:
        _INDEX_CACHE.popitem(last=False)
    return index


@router.get("/{project_id}/api-usage", response_model=ApiUsageResult)
async def get_api_usage(
    project_id: str,
    service: ProjectServiceDep,
    repository: AnalysisRepositoryDep,
    runtime_repository: Annotated[RuntimeRepository, Depends(get_runtime_repository)],
    url: str = Query(min_length=1, description="需求里写的接口 URL，可带 origin / query"),
    method: str | None = Query(
        default=None, description="HTTP 方法；缺省匹配任意方法（结果降档标注）"
    ),
    match: Literal["exact", "suffix"] = Query(default="exact"),
) -> ApiUsageResult:
    project = await require_project(project_id, service)
    graph = await repository.get_latest_graph(project_id)
    if graph is None:
        # 与 /graph 端点同一策略：无真实分析时回退示例图，但必须显式告知
        graph = build_sample_graph(project)
        sample = True
    else:
        sample = False

    index = _index_for(graph)
    lookup = index.lookup(method=method, url=url, match=match)
    warnings: list[RequirementWarning] = list(lookup.warnings)
    if sample:
        warnings.append(
            RequirementWarning(
                code="sample_graph",
                severity=Severity.info,
                message="当前项目没有真实分析产物，结果来自示例图，不代表真实源码。",
            )
        )

    trace = await runtime_repository.get_latest_trace(project_id)
    route_paths = {hit.page_node_id: index.route_path_of(hit.route_node_id) for hit in lookup.pages}
    pages = apply_runtime_backing(lookup.pages, route_paths, method=method, url=url, trace=trace)

    return ApiUsageResult(
        pages=[
            ApiUsagePage(
                page_node_id=hit.page_node_id,
                route_node_id=hit.route_node_id,
                path=list(hit.path),
                edge_path=list(hit.edge_path),
                confidence=hit.confidence,
                url_suffix_match=hit.url_suffix_match,
                runtime_backed=hit.runtime_backed,
                evidence=list(hit.evidence),
            )
            for hit in pages
        ],
        warnings=warnings,
    )
