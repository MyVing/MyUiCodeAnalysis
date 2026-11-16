"""项目领域服务。"""

from datetime import UTC, datetime
from uuid import uuid4

from app.schemas import Project, ProjectCreate
from app.storage.repositories import ProjectRepository


class ProjectService:
    def __init__(self, repository: ProjectRepository) -> None:
        self._repository = repository

    def list_projects(self) -> list[Project]:
        return self._repository.list()

    def get_project(self, project_id: str) -> Project | None:
        return self._repository.get(project_id)

    def create_project(self, payload: ProjectCreate) -> Project:
        project = Project(
            id=f"prj_{uuid4().hex[:12]}",
            name=payload.name,
            root_path=payload.root_path,
            framework=payload.framework,
            created_at=datetime.now(UTC),
        )
        return self._repository.add(project)
