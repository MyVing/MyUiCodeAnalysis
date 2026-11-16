"""分析任务：拉起 ast-worker 子进程执行真实静态分析。"""

from fastapi import APIRouter, HTTPException, status

from app.analysis.service import AnalysisConflictError
from app.api.deps import AnalysisServiceDep, ProjectServiceDep
from app.api.v1.projects import require_project
from app.schemas import AnalysisStart, AnalysisTask, AnalysisTaskList

router = APIRouter(tags=["analysis"])


@router.post(
    "/projects/{project_id}/analyses",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AnalysisTask,
    responses={409: {"description": "该项目已有进行中的分析任务"}},
)
async def start_analysis(
    project_id: str,
    service: ProjectServiceDep,
    analysis_service: AnalysisServiceDep,
    payload: AnalysisStart | None = None,
) -> AnalysisTask:
    project = await require_project(project_id, service)
    try:
        return await analysis_service.start(project, payload)
    except AnalysisConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该项目已有进行中的分析任务，请等待完成后再试。",
        ) from None


@router.get("/projects/{project_id}/analyses", response_model=AnalysisTaskList)
async def list_analyses(
    project_id: str,
    service: ProjectServiceDep,
    analysis_service: AnalysisServiceDep,
) -> AnalysisTaskList:
    await require_project(project_id, service)
    return AnalysisTaskList(items=await analysis_service.list_tasks(project_id))
