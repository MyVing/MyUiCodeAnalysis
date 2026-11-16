"""项目管理。"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import ProjectServiceDep
from app.schemas import Project, ProjectCreate

router = APIRouter(prefix="/projects", tags=["projects"])


def require_project(project_id: str, service: ProjectServiceDep) -> Project:
    project = service.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


@router.get("", response_model=list[Project])
def list_projects(service: ProjectServiceDep) -> list[Project]:
    return service.list_projects()


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, service: ProjectServiceDep) -> Project:
    return service.create_project(payload)


@router.get("/{project_id}", response_model=Project)
def get_project(project_id: str, service: ProjectServiceDep) -> Project:
    return require_project(project_id, service)
