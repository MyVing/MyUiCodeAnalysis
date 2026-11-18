"""依赖注入。

仓储从 app.state.storage 取（lifespan 装配，见 storage/factory.py），
测试通过 dependency_overrides 覆盖 get_*_repository 即可替换实现。
"""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from app.analysis.runner import SubprocessWorkerRunner, WorkerRunner
from app.analysis.service import AnalysisService
from app.core.config import (
    Settings,
    get_settings,
    resolve_explore_worker_entry,
    resolve_runtime_worker_entry,
    resolve_worker_entry,
)
from app.explore.runner import ExploreWorkerRunner
from app.explore.service import ExploreService
from app.git_client import GitClient, SubprocessGitClient
from app.plugin_manager.registry import PluginRegistry, create_plugin_registry
from app.requirement.llm import LLMClient, build_llm_client
from app.requirement.service import RequirementLocateService
from app.runtime.runner import RuntimeWorkerRunner, SubprocessRuntimeRunner
from app.runtime.service import RuntimeService
from app.services.project_service import ProjectService
from app.storage.checkpoint import MySqlCheckpointSaver
from app.storage.explore import ExploreRepository
from app.storage.factory import Storage
from app.storage.repositories import AnalysisRepository, ProjectRepository
from app.storage.requirement import RequirementRepository
from app.storage.runtime import RuntimeRepository

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


def get_runtime_repository(request: Request) -> RuntimeRepository:
    return get_storage(request).runtimes


def get_explore_repository(request: Request) -> ExploreRepository:
    return get_storage(request).explores


def get_git_client(settings: SettingsDep) -> GitClient:
    return SubprocessGitClient(settings.git_command, settings.git_timeout_seconds)


def get_project_service(
    repository: Annotated[ProjectRepository, Depends(get_project_repository)],
    git: Annotated[GitClient, Depends(get_git_client)],
    settings: SettingsDep,
) -> ProjectService:
    return ProjectService(repository, git, settings)


def get_worker_runner() -> WorkerRunner:
    settings = get_settings()
    return SubprocessWorkerRunner(
        node_command=settings.worker_node_command,
        entry_path=str(resolve_worker_entry(settings)),
    )


def get_llm_client() -> LLMClient | None:
    return build_llm_client(get_settings())


def get_analysis_service(
    repository: Annotated[AnalysisRepository, Depends(get_analysis_repository)],
    runner: Annotated[WorkerRunner, Depends(get_worker_runner)],
    llm: Annotated[LLMClient | None, Depends(get_llm_client)],
    settings: SettingsDep,
) -> AnalysisService:
    return AnalysisService(repository, runner, settings, llm)


@lru_cache
def get_plugin_registry() -> PluginRegistry:
    return create_plugin_registry()


def get_requirement_checkpointer(request: Request) -> BaseCheckpointSaver:
    """MySQL 后端复用业务连接池；内存后端退到 MemorySaver（不落盘）。"""
    engine = get_storage(request).engine
    if engine is not None:
        return MySqlCheckpointSaver(engine)
    return MemorySaver()


def get_requirement_locate_service(
    repository: Annotated[RequirementRepository, Depends(get_requirement_repository)],
    analyses: Annotated[AnalysisRepository, Depends(get_analysis_repository)],
    llm: Annotated[LLMClient | None, Depends(get_llm_client)],
    checkpointer: Annotated[BaseCheckpointSaver, Depends(get_requirement_checkpointer)],
    settings: SettingsDep,
) -> RequirementLocateService:
    return RequirementLocateService(repository, analyses, settings, llm, checkpointer)


def get_runtime_worker_runner() -> RuntimeWorkerRunner:
    settings = get_settings()
    return SubprocessRuntimeRunner(
        node_command=settings.worker_node_command,
        entry_path=str(resolve_runtime_worker_entry(settings)),
    )


def get_runtime_service(
    repository: Annotated[RuntimeRepository, Depends(get_runtime_repository)],
    analyses: Annotated[AnalysisRepository, Depends(get_analysis_repository)],
    runner: Annotated[RuntimeWorkerRunner, Depends(get_runtime_worker_runner)],
    settings: SettingsDep,
) -> RuntimeService:
    return RuntimeService(repository, analyses, runner, settings)


def get_explore_worker_runner() -> ExploreWorkerRunner:
    settings = get_settings()
    return ExploreWorkerRunner(
        node_command=settings.worker_node_command,
        entry_path=str(resolve_explore_worker_entry(settings)),
    )


def get_explore_service(
    repository: Annotated[ExploreRepository, Depends(get_explore_repository)],
    runner: Annotated[ExploreWorkerRunner, Depends(get_explore_worker_runner)],
    llm: Annotated[LLMClient | None, Depends(get_llm_client)],
    settings: SettingsDep,
) -> ExploreService:
    return ExploreService(repository, runner, settings, llm)


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
AnalysisRepositoryDep = Annotated[AnalysisRepository, Depends(get_analysis_repository)]
AnalysisServiceDep = Annotated[AnalysisService, Depends(get_analysis_service)]
PluginRegistryDep = Annotated[PluginRegistry, Depends(get_plugin_registry)]
RequirementLocateServiceDep = Annotated[
    RequirementLocateService, Depends(get_requirement_locate_service)
]
RuntimeServiceDep = Annotated[RuntimeService, Depends(get_runtime_service)]
ExploreServiceDep = Annotated[ExploreService, Depends(get_explore_service)]
