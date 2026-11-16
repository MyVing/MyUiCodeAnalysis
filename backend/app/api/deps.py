"""依赖注入。"""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.analysis.runner import SubprocessWorkerRunner, WorkerRunner
from app.analysis.service import AnalysisService
from app.core.config import Settings, get_settings, resolve_worker_entry
from app.plugin_manager.registry import PluginRegistry, create_plugin_registry
from app.requirement.service import RequirementLocateService
from app.services.project_service import ProjectService
from app.storage.memory import (
    create_analysis_repository,
    create_project_repository,
    create_requirement_repository,
)
from app.storage.repositories import AnalysisRepository, ProjectRepository
from app.storage.requirement import RequirementRepository

SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache
def get_project_repository() -> ProjectRepository:
    return create_project_repository()


@lru_cache
def get_analysis_repository() -> AnalysisRepository:
    return create_analysis_repository()


def get_project_service(
    repository: Annotated[ProjectRepository, Depends(get_project_repository)],
) -> ProjectService:
    return ProjectService(repository)


def get_worker_runner() -> WorkerRunner:
    settings = get_settings()
    return SubprocessWorkerRunner(
        node_command=settings.worker_node_command,
        entry_path=str(resolve_worker_entry(settings)),
    )


def get_analysis_service(
    repository: Annotated[AnalysisRepository, Depends(get_analysis_repository)],
    runner: Annotated[WorkerRunner, Depends(get_worker_runner)],
    settings: SettingsDep,
) -> AnalysisService:
    return AnalysisService(repository, runner, settings)


@lru_cache
def get_plugin_registry() -> PluginRegistry:
    return create_plugin_registry()


@lru_cache
def get_requirement_repository() -> RequirementRepository:
    return create_requirement_repository()


def get_requirement_locate_service(
    repository: Annotated[RequirementRepository, Depends(get_requirement_repository)],
    analyses: Annotated[AnalysisRepository, Depends(get_analysis_repository)],
    settings: SettingsDep,
) -> RequirementLocateService:
    return RequirementLocateService(repository, analyses, settings)


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
AnalysisRepositoryDep = Annotated[AnalysisRepository, Depends(get_analysis_repository)]
AnalysisServiceDep = Annotated[AnalysisService, Depends(get_analysis_service)]
PluginRegistryDep = Annotated[PluginRegistry, Depends(get_plugin_registry)]
RequirementLocateServiceDep = Annotated[
    RequirementLocateService, Depends(get_requirement_locate_service)
]
