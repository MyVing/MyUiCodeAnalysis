"""依赖注入。

仓储从 app.state.storage 取（lifespan 装配，见 storage/factory.py），
测试通过 dependency_overrides 覆盖 get_*_repository 即可替换实现。
"""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from app.analysis.runner import SubprocessWorkerRunner, WorkerRunner
from app.analysis.service import AnalysisService
from app.core.config import Settings, get_settings, resolve_worker_entry
from app.plugin_manager.registry import PluginRegistry, create_plugin_registry
from app.requirement.service import RequirementLocateService
from app.services.project_service import ProjectService
from app.storage.factory import Storage
from app.storage.repositories import AnalysisRepository, ProjectRepository
from app.storage.requirement import RequirementRepository

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_storage(request: Request) -> Storage:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        # 直接以 ASGI 方式调用（未走 lifespan）时兜底，行为与旧内存实现一致
        from app.storage.factory import create_storage

        storage = create_storage(get_settings())
        request.app.state.storage = storage
    return storage


def get_project_repository(request: Request) -> ProjectRepository:
    return get_storage(request).projects


def get_analysis_repository(request: Request) -> AnalysisRepository:
    return get_storage(request).analyses


def get_requirement_repository(request: Request) -> RequirementRepository:
    return get_storage(request).requirements


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
