"""项目管理：本地目录登记与 Git 仓库接入/更新。"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import ProjectServiceDep
from app.schemas import Project, ProjectCreate, ProjectGitCreate
from app.services.project_service import (
    GitOperationError,
    GitProjectExistsError,
    InvalidGitUrlError,
    NotGitProjectError,
)

router = APIRouter(prefix="/projects", tags=["projects"])


async def require_project(project_id: str, service: ProjectServiceDep) -> Project:
    project = await service.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


@router.get("", response_model=list[Project])
async def list_projects(service: ProjectServiceDep) -> list[Project]:
    return await service.list_projects()


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, service: ProjectServiceDep) -> Project:
    return await service.create_project(payload)


@router.post(
    "/git",
    response_model=Project,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "该 Git 地址已接入过（同一仓库只能接入一次）"},
        422: {"description": "Git 地址无法识别或克隆失败"},
    },
)
async def create_git_project(
    payload: ProjectGitCreate, service: ProjectServiceDep
) -> Project:
    try:
        return await service.create_git_project(payload)
    except InvalidGitUrlError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from None
    except GitProjectExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "git_project_exists",
                "message": f"该 Git 地址已接入项目「{error.project.name}」，同一仓库只能接入一次。",
                "projectId": error.project.id,
            },
        ) from None
    except GitOperationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from None


@router.post(
    "/{project_id}/sync",
    response_model=Project,
    responses={
        409: {"description": "本地目录项目不支持从远端更新"},
        422: {"description": "更新失败（仓库目录缺失、无跟踪分支等）"},
    },
)
async def sync_project(project_id: str, service: ProjectServiceDep) -> Project:
    project = await require_project(project_id, service)
    try:
        return await service.sync_project(project)
    except NotGitProjectError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    except GitOperationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from None


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: str, service: ProjectServiceDep) -> Project:
    return await require_project(project_id, service)
