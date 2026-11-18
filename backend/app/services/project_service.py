"""项目领域服务。

两种接入方式共用同一张 projects 表：
- 本地目录：用户给出 root_path，平台只读；
- Git 仓库：平台克隆到受管目录，root_path 指向它，更新时强制对齐远端。

Git 项目的去重键是归一化后的远程地址，同一仓库只能接入一次；
更新语义是「以远端当前分支为准」，因为托管克隆里的本地改动没有保留价值。
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.git_client import (
    GitClient,
    GitError,
    detect_framework,
    normalize_remote_url,
    remove_repo_directory,
    repo_directory_name,
    repo_display_name,
)
from app.schemas import Project, ProjectCreate, ProjectGitCreate
from app.storage.repositories import DuplicateRemoteUrlError, ProjectRepository


class InvalidGitUrlError(Exception):
    """用户输入的 Git 地址无法识别。"""


class GitProjectExistsError(Exception):
    """同一 Git 地址已经接入过；携带既有项目供前端直接跳转。"""

    def __init__(self, project: Project) -> None:
        super().__init__(project.id)
        self.project = project


class GitOperationError(Exception):
    """克隆或更新失败，消息可直接展示。"""


class NotGitProjectError(Exception):
    """本地目录项目没有可更新的远端。"""


class ProjectService:
    def __init__(
        self,
        repository: ProjectRepository,
        git: GitClient,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._git = git
        self._settings = settings

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

    async def create_git_project(self, payload: ProjectGitCreate) -> Project:
        try:
            normalized = normalize_remote_url(payload.git_url)
        except ValueError as error:
            raise InvalidGitUrlError(str(error)) from error

        existing = await self._repository.get_by_remote(normalized)
        if existing is not None:
            raise GitProjectExistsError(existing)

        destination = self._settings.repos_dir / repo_directory_name(normalized)
        # 历史失败可能留下半成品目录；受管目录由平台独占，可安全清理
        remove_repo_directory(destination)
        try:
            snapshot = await self._git.clone(
                payload.git_url.strip(),
                destination,
                branch=payload.branch,
                username=payload.username,
                password=payload.password,
                token=payload.token,
            )
        except GitError as error:
            remove_repo_directory(destination)
            raise GitOperationError(f"Git 仓库克隆失败：{error}") from error
        except Exception as error:
            remove_repo_directory(destination)
            raise GitOperationError(f"Git 仓库克隆失败：{error}") from error

        project = Project(
            id=f"prj_{uuid4().hex[:12]}",
            name=payload.name or repo_display_name(normalized)[:80],
            root_path=str(destination),
            framework=payload.framework or detect_framework(destination),
            created_at=datetime.now(UTC),
            source="git",
            remote_url=normalized,
            git_branch=snapshot.branch,
            commit_sha=snapshot.commit,
            last_synced_at=datetime.now(UTC),
        )
        try:
            return await self._repository.add(project)
        except DuplicateRemoteUrlError as error:
            # 并发接入同一地址：目录归先到者，后来者清理自己的副本并返回既有项目
            remove_repo_directory(destination)
            existing = await self._repository.get_by_remote(normalized)
            if existing is None:
                raise GitOperationError("该 Git 地址正在被其他请求接入，请稍后重试") from error
            raise GitProjectExistsError(existing) from error

    async def sync_project(self, project: Project) -> Project:
        if project.source != "git" or not project.remote_url:
            raise NotGitProjectError("只有 Git 接入的项目支持从远端更新")
        root = Path(project.root_path)
        if not root.is_dir():
            raise GitOperationError(f"本地仓库目录不存在：{project.root_path}")
        try:
            snapshot = await self._git.sync(root)
        except GitError as error:
            raise GitOperationError(f"更新失败：{error}") from error

        updated = project.model_copy(
            update={
                "git_branch": snapshot.branch,
                "commit_sha": snapshot.commit,
                "last_synced_at": datetime.now(UTC),
            }
        )
        return await self._repository.update(updated)
