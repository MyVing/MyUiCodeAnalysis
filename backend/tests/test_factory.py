"""存储工厂的分支选择：不连真实数据库，只验证配置→实现的映射。"""

from app.core.config import Settings
from app.storage.factory import create_storage
from app.storage.memory import (
    InMemoryAnalysisRepository,
    InMemoryProjectRepository,
    InMemoryRequirementRepository,
    InMemoryRuntimeRepository,
)
from app.storage.mysql import (
    MySqlAnalysisRepository,
    MySqlProjectRepository,
    MySqlRequirementRepository,
    MySqlRuntimeRepository,
)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_settings_without_db_url_uses_memory_backend() -> None:
    settings = _settings()
    assert settings.storage_backend == "memory"
    storage = create_storage(settings)
    assert isinstance(storage.projects, InMemoryProjectRepository)
    assert isinstance(storage.analyses, InMemoryAnalysisRepository)
    assert isinstance(storage.requirements, InMemoryRequirementRepository)
    assert isinstance(storage.runtimes, InMemoryRuntimeRepository)
    assert storage.engine is None


def test_settings_with_db_url_uses_mysql_backend() -> None:
    settings = _settings(db_url="mysql+aiomysql://user:pass@127.0.0.1:3307/db?charset=utf8mb4")
    assert settings.storage_backend == "mysql"
    storage = create_storage(settings)
    assert isinstance(storage.projects, MySqlProjectRepository)
    assert isinstance(storage.analyses, MySqlAnalysisRepository)
    assert isinstance(storage.requirements, MySqlRequirementRepository)
    assert isinstance(storage.runtimes, MySqlRuntimeRepository)
    # 构建引擎不触发连接，无需真实实例
    assert storage.engine is not None
