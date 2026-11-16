"""需求定位 API。

两类资源刻意分开：
  /projects/{id}/requirements  项目维度：发起与列出
  /requirements/{taskId}       任务维度：查询状态与产物
这样前端「发起 → 轮询 taskId → 取 map」不需要回填项目 id。
"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import (
    ProjectServiceDep,
    RequirementLocateServiceDep,
)
from app.api.v1.projects import require_project
from app.contracts import RequirementMap
from app.requirement.service import RequirementPreconditionError
from app.schemas import RequirementLocateStart, RequirementTask, RequirementTaskList

router = APIRouter(tags=["requirements"])

_PRECONDITION_STATUS: dict[str, int] = {
    "graph_missing": status.HTTP_409_CONFLICT,
    "graph_is_sample": status.HTTP_409_CONFLICT,
    "llm_unavailable": status.HTTP_409_CONFLICT,
    "analysis_not_found": status.HTTP_404_NOT_FOUND,
}


@router.post(
    "/projects/{project_id}/requirements",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RequirementTask,
    responses={
        404: {"description": "项目或指定的分析产物不存在"},
        409: {"description": "无图 / 图为示例数据 / 模型未配置"},
    },
)
async def start_requirement_locate(
    project_id: str,
    payload: RequirementLocateStart,
    service: ProjectServiceDep,
    locate_service: RequirementLocateServiceDep,
) -> RequirementTask:
    project = await require_project(project_id, service)
    try:
        return await locate_service.start(project, payload)
    except RequirementPreconditionError as error:
        raise HTTPException(
            status_code=_PRECONDITION_STATUS.get(
                error.code, status.HTTP_409_CONFLICT
            ),
            detail=error.message,
        ) from None


@router.get("/projects/{project_id}/requirements", response_model=RequirementTaskList)
async def list_requirement_tasks(
    project_id: str,
    service: ProjectServiceDep,
    locate_service: RequirementLocateServiceDep,
) -> RequirementTaskList:
    await require_project(project_id, service)
    return RequirementTaskList(items=await locate_service.list_tasks(project_id))


@router.get(
    "/requirements/{task_id}",
    response_model=RequirementTask,
    responses={404: {"description": "任务不存在"}},
)
async def get_requirement_task(
    task_id: str, locate_service: RequirementLocateServiceDep
) -> RequirementTask:
    task = await locate_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"需求定位任务 {task_id} 不存在"
        )
    return task


@router.get(
    "/requirements/{task_id}/map",
    response_model=RequirementMap,
    response_model_exclude_none=True,
    responses={
        404: {"description": "任务不存在，或产物尚未生成"},
        409: {"description": "任务未成功完成"},
    },
)
async def get_requirement_map(
    task_id: str, locate_service: RequirementLocateServiceDep
) -> RequirementMap:
    task = await locate_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"需求定位任务 {task_id} 不存在"
        )
    if task.status != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态为 {task.status}，尚无可读取的定位产物。",
        )

    requirement_map = await locate_service.get_map_by_task(task_id)
    if requirement_map is None:
        # 任务成功但产物已被有界淘汰：如实说明，而不是假装「还在准备」
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="定位产物已因保留上限被清理，请重新发起定位。",
        )
    return requirement_map
