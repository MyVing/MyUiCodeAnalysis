"""运行时采集 API，与 requirement 路由同构：

/projects/{id}/runtime-traces  项目维度：发起与列出
/runtime-tasks/{taskId}        任务维度：查询状态与采集产物
"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import (
    ProjectServiceDep,
    RequirementCollectServiceDep,
    RuntimeServiceDep,
)
from app.api.v1.projects import require_project
from app.contracts import RuntimeTrace
from app.runtime.requirement_collect import RequirementCollectPreconditionError
from app.runtime.service import RuntimePreconditionError
from app.schemas import (
    RequirementCollectResult,
    RequirementCollectStart,
    RuntimeStart,
    RuntimeTask,
    RuntimeTaskList,
)

router = APIRouter(tags=["runtime"])

_PRECONDITION_STATUS: dict[str, int] = {
    "missing_launch_source": status.HTTP_409_CONFLICT,
    "root_path_missing": status.HTTP_409_CONFLICT,
    "analysis_not_found": status.HTTP_404_NOT_FOUND,
}

_COLLECT_PRECONDITION_STATUS: dict[str, int] = {
    "requirement_task_not_found": status.HTTP_404_NOT_FOUND,
    "graph_missing": status.HTTP_404_NOT_FOUND,
    "requirement_task_not_succeeded": status.HTTP_409_CONFLICT,
    "requirement_locate_failed": status.HTTP_409_CONFLICT,
    "requirement_locate_timeout": status.HTTP_409_CONFLICT,
    "map_missing": status.HTTP_409_CONFLICT,
    "missing_requirement_source": status.HTTP_409_CONFLICT,
    "no_matched_pages": status.HTTP_409_CONFLICT,
    "no_collectable_routes": status.HTTP_409_CONFLICT,
}


@router.post(
    "/projects/{project_id}/runtime-traces",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RuntimeTask,
    responses={
        404: {"description": "项目或指定的分析产物不存在"},
        409: {"description": "缺启动来源（url / command）或根路径缺失"},
    },
)
async def start_runtime_collect(
    project_id: str,
    payload: RuntimeStart,
    service: ProjectServiceDep,
    runtime_service: RuntimeServiceDep,
) -> RuntimeTask:
    project = await require_project(project_id, service)
    try:
        return await runtime_service.start(project, payload)
    except RuntimePreconditionError as error:
        raise HTTPException(
            status_code=_PRECONDITION_STATUS.get(error.code, status.HTTP_409_CONFLICT),
            detail=error.message,
        ) from None


@router.get("/projects/{project_id}/runtime-traces", response_model=RuntimeTaskList)
async def list_runtime_tasks(
    project_id: str,
    service: ProjectServiceDep,
    runtime_service: RuntimeServiceDep,
) -> RuntimeTaskList:
    await require_project(project_id, service)
    return RuntimeTaskList(items=await runtime_service.list_tasks(project_id))


@router.post(
    "/projects/{project_id}/requirement-collects",
    status_code=status.HTTP_200_OK,
    response_model=RequirementCollectResult,
    responses={
        404: {"description": "项目 / 需求定位任务 / 静态图不存在"},
        409: {"description": "需求定位未成功、没有匹配页面，或路由解析不出可访问 URL"},
    },
)
async def start_requirement_collect(
    project_id: str,
    payload: RequirementCollectStart,
    service: ProjectServiceDep,
    collect_service: RequirementCollectServiceDep,
) -> RequirementCollectResult:
    """按需求采集：需求描述 → 定位页面 → 反查路由 → 逐页发起采集。

    同步返回已创建的采集任务与被跳过的页面；每个采集任务在后台异步执行，
    复用 /runtime-tasks/{taskId} 轮询。
    """
    project = await require_project(project_id, service)
    try:
        tasks, skipped, requirement_task_id = await collect_service.start(project, payload)
    except RequirementCollectPreconditionError as error:
        raise HTTPException(
            status_code=_COLLECT_PRECONDITION_STATUS.get(error.code, status.HTTP_409_CONFLICT),
            detail=error.message,
        ) from None
    return RequirementCollectResult(
        requirementTaskId=requirement_task_id,
        items=tasks,
        skipped=skipped,
    )


@router.get(
    "/runtime-tasks/{task_id}",
    response_model=RuntimeTask,
    responses={404: {"description": "任务不存在"}},
)
async def get_runtime_task(task_id: str, runtime_service: RuntimeServiceDep) -> RuntimeTask:
    task = await runtime_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"运行时采集任务 {task_id} 不存在"
        )
    return task


@router.get(
    "/runtime-tasks/{task_id}/trace",
    response_model=RuntimeTrace,
    response_model_exclude_none=True,
    responses={
        404: {"description": "任务不存在，或采集产物尚未生成"},
        409: {"description": "任务未成功完成"},
    },
)
async def get_runtime_trace(task_id: str, runtime_service: RuntimeServiceDep) -> RuntimeTrace:
    task = await runtime_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"运行时采集任务 {task_id} 不存在"
        )
    if task.status != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态为 {task.status}，尚无可读取的采集产物。",
        )

    trace = await runtime_service.get_trace_by_task(task_id)
    if trace is None:
        # 任务成功但产物已被有界淘汰：如实说明，而不是假装「还在准备」
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="采集产物已因保留上限被清理，请重新发起采集。",
        )
    return trace
