"""存储装配：按配置选择内存 / MySQL 实现，并在 MySQL 分支完成建表与重启恢复。"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.storage.chat import ChatSessionRepository
from app.storage.explore import ExploreRepository
from app.storage.memory import (
    create_analysis_repository,
    create_chat_session_repository,
    create_explore_repository,
    create_project_repository,
    create_requirement_repository,
    create_runtime_repository,
    create_specdoc_repository,
)
from app.storage.mysql import (
    MySqlAnalysisRepository,
    MySqlChatSessionRepository,
    MySqlExploreRepository,
    MySqlProjectRepository,
    MySqlRequirementRepository,
    MySqlRuntimeRepository,
    MySqlSpecDocRepository,
    create_engine_from_url,
    init_schema,
    run_boot_recovery,
)
from app.storage.repositories import AnalysisRepository, ProjectRepository
from app.storage.requirement import RequirementRepository
from app.storage.runtime import RuntimeRepository
from app.storage.specdoc import SpecDocRepository

logger = logging.getLogger(__name__)


@dataclass
class Storage:
    projects: ProjectRepository
    analyses: AnalysisRepository
    requirements: RequirementRepository
    runtimes: RuntimeRepository
    explores: "ExploreRepository"
    specdocs: SpecDocRepository
    chat_sessions: ChatSessionRepository
    engine: AsyncEngine | None = None  # memory 分支为 None


def create_storage(settings: Settings) -> Storage:
    """按 db_url 有无选择实现；不涉及 IO，可在任意上下文调用。"""
    if settings.db_url is None:
        return Storage(
            projects=create_project_repository(),
            analyses=create_analysis_repository(),
            requirements=create_requirement_repository(),
            runtimes=create_runtime_repository(),
            explores=create_explore_repository(),
            specdocs=create_specdoc_repository(),
            chat_sessions=create_chat_session_repository(),
        )
    engine = create_engine_from_url(settings)
    return Storage(
        projects=MySqlProjectRepository(engine),
        analyses=MySqlAnalysisRepository(engine),
        requirements=MySqlRequirementRepository(engine),
        runtimes=MySqlRuntimeRepository(engine),
        explores=MySqlExploreRepository(engine),
        specdocs=MySqlSpecDocRepository(engine),
        chat_sessions=MySqlChatSessionRepository(engine),
        engine=engine,
    )


async def init_storage(settings: Settings) -> Storage:
    """lifespan 入口：MySQL 分支建表 + 重启恢复；memory 分支直通（测试也走这里）。"""
    storage = create_storage(settings)
    if storage.engine is not None:
        await init_schema(storage.engine)
        await run_boot_recovery(storage.engine)
        logger.info("已连接 MySQL 持久化存储")
    return storage


async def dispose_storage(storage: Storage) -> None:
    if storage.engine is not None:
        await storage.engine.dispose()
