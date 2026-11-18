"""AI 自动探索 API，与 runtime 路由同构：

  /projects/{id}/explores          项目维度：发起与列出
  /explore-tasks/{taskId}          任务维度：查询状态
  /explore-tasks/{taskId}/artifact 任务维度：探索产物（轨迹/计划/场景/脚本）
  /explore-tasks/{taskId}/script   任务维度：下载生成的 Playwright 脚本
  /explore-tasks/{taskId}/screenshots/{fileName}  逐步截图
"""

import os
import re
import tempfile

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.api.deps import ExploreServiceDep, ProjectServiceDep
from app.api.v1.projects import require_project
from app.contracts import ExploreArtifact
from app.explore.service import ExplorePreconditionError
from app.schemas import ExploreStart, ExploreTask, ExploreTaskList

router = APIRouter(tags=["explore"])

_PRECONDITION_STATUS: dict[str, int] = {
    "missing_launch_source": status.HTTP_409_CONFLICT,
    "root_path_missing": status.HTTP_409_CONFLICT,
    "analysis_not_found": status.HTTP_404_NOT_FOUND,
}

_SCRIPT_MIME = "text/x-typescript; charset=utf-8"


@router.post(
    "/projects/{project_id}/explores",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ExploreTask,
    responses={
        404: {"description": "项目不存在"},
        409: {"description": "缺启动来源（url / command）或根路径缺失"},
    },
)
async def start_explore(
    project_id: str,
    payload: ExploreStart,
    service: ProjectServiceDep,
    explore_service: ExploreServiceDep,
) -> ExploreTask:
    project = await require_project(project_id, service)
    try:
        return await explore_service.start(project, payload)
    except ExplorePreconditionError as error:
        raise HTTPException(
            status_code=_PRECONDITION_STATUS.get(error.code, status.HTTP_409_CONFLICT),
            detail=error.message,
        ) from None


@router.get("/projects/{project_id}/explores", response_model=ExploreTaskList)
async def list_explore_tasks(
    project_id: str,
    service: ProjectServiceDep,
    explore_service: ExploreServiceDep,
) -> ExploreTaskList:
    await require_project(project_id, service)
    return ExploreTaskList(items=await explore_service.list_tasks(project_id))


@router.get(
    "/explore-tasks/{task_id}",
    response_model=ExploreTask,
    responses={404: {"description": "任务不存在"}},
)
async def get_explore_task(
    task_id: str, explore_service: ExploreServiceDep
) -> ExploreTask:
    task = await explore_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"探索任务 {task_id} 不存在"
        )
    return task


@router.get(
    "/explore-tasks/{task_id}/artifact",
    response_model=ExploreArtifact,
    response_model_exclude_none=True,
    responses={
        404: {"description": "任务不存在，或探索产物尚未生成"},
        409: {"description": "任务未成功完成"},
    },
)
async def get_explore_artifact(
    task_id: str, explore_service: ExploreServiceDep
) -> ExploreArtifact:
    task = await explore_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"探索任务 {task_id} 不存在"
        )
    if task.status != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态为 {task.status}，尚无可读取的探索产物。",
        )
    artifact = await explore_service.get_artifact(task_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="探索产物已因保留上限被清理，请重新发起探索。",
        )
    return artifact


@router.get(
    "/explore-tasks/{task_id}/script",
    response_class=FileResponse,
    responses={
        404: {"description": "任务不存在，或脚本尚未生成"},
        409: {"description": "任务未成功完成"},
    },
)
async def download_explore_script(
    task_id: str, explore_service: ExploreServiceDep
) -> FileResponse:
    artifact = await _require_succeeded_artifact(task_id, explore_service)
    if artifact.script is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="脚本尚未生成")
    # FileResponse 只支持磁盘路径：脚本内容在库里，先落临时文件再回传
    with tempfile.NamedTemporaryFile(
        "w", suffix=f"_{artifact.script.fileName}", encoding="utf-8", delete=False
    ) as handle:
        handle.write(artifact.script.content)
        temp_path = handle.name
    return FileResponse(
        path=temp_path, media_type=_SCRIPT_MIME, filename=artifact.script.fileName
    )


async def _require_succeeded_artifact(
    task_id: str, explore_service: ExploreServiceDep
) -> ExploreArtifact:
    task = await explore_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"探索任务 {task_id} 不存在"
        )
    if task.status != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态为 {task.status}，尚无可读取的探索产物。",
        )
    artifact = await explore_service.get_artifact(task_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="探索产物已因保留上限被清理，请重新发起探索。",
        )
    return artifact


@router.get(
    "/explore-tasks/{task_id}/screenshots/{file_name}",
    response_class=FileResponse,
    responses={
        404: {"description": "任务不存在，截图不存在，或任务未成功完成"},
    },
)
async def download_explore_screenshot(
    task_id: str,
    file_name: str,
    explore_service: ExploreServiceDep,
) -> FileResponse:
    task = await explore_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"探索任务 {task_id} 不存在"
        )
    if task.status != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态为 {task.status}，产物不可读取。",
        )
    artifacts_dir = await explore_service.get_artifacts_dir(task_id)
    if artifacts_dir is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="探索产物目录不存在"
        )
    # 文件名固定为 step-NNN.png（契约层已约束）；拼接后仍做一次白名单校验，
    # 防止路径穿越（artifacts_dir 来自库内记录，file_name 来自 URL）。
    if not _is_valid_screenshot_name(file_name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="截图文件名非法")
    path = os.path.join(artifacts_dir, file_name)
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="截图不存在或已被清理"
        )
    return FileResponse(path, media_type="image/png", filename=file_name)


def _is_valid_screenshot_name(file_name: str) -> bool:
    return re.fullmatch(r"step-\d{3}\.png", file_name) is not None
