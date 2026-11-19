"""规格文档装配 API。

两类资源刻意分开（与需求定位 API 同构）：
  /projects/{id}/specdocs  项目维度：发起与列出
  /specdocs/{taskId}       任务维度：查询状态与产物（JSON / Markdown）
这样前端「发起 → 轮询 taskId → 取 doc」不需要回填项目 id。
"""

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import ProjectServiceDep, SpecDocServiceDep
from app.api.v1.projects import require_project
from app.contracts.specdoc import SpecDocMap
from app.schemas import SpecDocStart, SpecDocTask, SpecDocTaskList
from app.specdoc.render import render_markdown
from app.specdoc.service import SpecDocPreconditionError

router = APIRouter(tags=["specdocs"])

_PRECONDITION_STATUS: dict[str, int] = {
    "specdoc_map_not_found": status.HTTP_404_NOT_FOUND,
    "specdoc_map_stale": status.HTTP_409_CONFLICT,
}


@router.post(
    "/projects/{project_id}/specdocs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SpecDocTask,
    responses={
        404: {"description": "项目不存在，或指定的需求定位产物不存在"},
        409: {"description": "定位产物对应的分析图已不可用"},
    },
)
async def start_spec_doc(
    project_id: str,
    payload: SpecDocStart,
    service: ProjectServiceDep,
    specdoc_service: SpecDocServiceDep,
) -> SpecDocTask:
    project = await require_project(project_id, service)
    try:
        return await specdoc_service.start(project, payload)
    except SpecDocPreconditionError as error:
        raise HTTPException(
            status_code=_PRECONDITION_STATUS.get(error.code, status.HTTP_409_CONFLICT),
            detail=error.message,
        ) from None


@router.get("/projects/{project_id}/specdocs", response_model=SpecDocTaskList)
async def list_specdoc_tasks(
    project_id: str,
    service: ProjectServiceDep,
    specdoc_service: SpecDocServiceDep,
) -> SpecDocTaskList:
    await require_project(project_id, service)
    return SpecDocTaskList(items=await specdoc_service.list_tasks(project_id))


@router.get(
    "/specdocs/{task_id}",
    response_model=SpecDocTask,
    responses={404: {"description": "任务不存在"}},
)
async def get_specdoc_task(
    task_id: str, specdoc_service: SpecDocServiceDep
) -> SpecDocTask:
    task = await specdoc_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"规格文档任务 {task_id} 不存在"
        )
    return task


async def _require_doc(
    task_id: str, specdoc_service: SpecDocServiceDep
) -> SpecDocMap:
    task = await specdoc_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"规格文档任务 {task_id} 不存在"
        )
    if task.status != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态为 {task.status}，尚无可读取的规格产物。",
        )
    doc = await specdoc_service.get_doc_by_task(task_id)
    if doc is None:
        # 任务成功但产物已被有界淘汰：如实说明，而不是假装「还在准备」
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="规格产物已因保留上限被清理，请重新发起装配。",
        )
    return doc


@router.get(
    "/specdocs/{task_id}/doc",
    response_model=SpecDocMap,
    response_model_exclude_none=True,
    responses={
        404: {"description": "任务不存在，或产物尚未生成"},
        409: {"description": "任务未成功完成"},
    },
)
async def get_spec_doc(
    task_id: str, specdoc_service: SpecDocServiceDep
) -> SpecDocMap:
    return await _require_doc(task_id, specdoc_service)


@router.get(
    "/specdocs/{task_id}/doc.md",
    response_class=Response,
    responses={
        200: {"content": {"text/markdown": {}}, "description": "Markdown 规格文档"},
        404: {"description": "任务不存在，或产物尚未生成"},
        409: {"description": "任务未成功完成"},
    },
)
async def get_spec_doc_markdown(
    task_id: str, specdoc_service: SpecDocServiceDep
) -> Response:
    doc = await _require_doc(task_id, specdoc_service)
    return Response(
        content=render_markdown(doc),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{task_id}.md"'},
    )
