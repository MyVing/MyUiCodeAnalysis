"""插件配置：只读清单可用，变更在 M1 提供。"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import PluginRegistryDep, ProjectServiceDep
from app.api.v1.projects import require_project
from app.schemas import PluginList

router = APIRouter(prefix="/projects", tags=["plugins"])


@router.get("/{project_id}/plugins", response_model=PluginList)
def list_project_plugins(
    project_id: str,
    service: ProjectServiceDep,
    registry: PluginRegistryDep,
) -> PluginList:
    require_project(project_id, service)
    return registry.list_for_project(project_id)


@router.patch(
    "/{project_id}/plugins",
    responses={501: {"description": "插件变更尚未接入"}},
)
def update_project_plugins(project_id: str, service: ProjectServiceDep) -> None:
    require_project(project_id, service)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="插件启停将在 M1 以独立子进程形式接入。",
    )
