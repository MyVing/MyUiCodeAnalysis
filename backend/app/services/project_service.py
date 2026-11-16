"""项目领域服务。"""

from datetime import UTC, datetime
from uuid import uuid4

from app.schemas import Project, ProjectCreate
from app.storage.repositories import ProjectRepository


class ProjectService:
    def __init__(self, repository: ProjectRepository) -> None:
        self._repository = repository

    async def list_projects(self) -> list[Project]:
        return await self._repository.list()

    async def get_project(self, project_id: str) -> Project | None:
        return await self._repository.get(project_id)

    async def create_project(self, payload: ProjectCreate) -> Project:
        project = Project(
            id=f"prj_{uuid4().hex[:12]}",
            name=payload.name,
            root_path=payload.root_path,
            framework=payload.framework,
            created_at=datetime.now(UTC),
        )
        return await self._repository.add(project)
