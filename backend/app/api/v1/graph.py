"""图查询：优先返回真实分析产物，无分析时回退到明确标记的示例 IR。"""

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import AnalysisRepositoryDep, ProjectServiceDep
from app.api.v1.projects import require_project
from app.contracts import IRGraph
from app.ir.sample import build_sample_graph

router = APIRouter(prefix="/projects", tags=["graph"])


@router.get("/{project_id}/graph", response_model=IRGraph, response_model_exclude_none=True)
async def get_project_graph(
    project_id: str,
    service: ProjectServiceDep,
    repository: AnalysisRepositoryDep,
    analysis_id: str | None = Query(default=None, alias="analysisId"),
) -> IRGraph:
    project = await require_project(project_id, service)
    if analysis_id is not None:
        graph = await repository.get_graph(project_id, analysis_id)
        if graph is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="分析产物不存在",
            )
        return graph
    latest = await repository.get_latest_graph(project_id)
    if latest is not None:
        return latest
    return build_sample_graph(project)
