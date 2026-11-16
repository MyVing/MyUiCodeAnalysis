"""应用配置。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    name: str = "React 代码智能分析平台"
    version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # ast-worker 子进程：node 命令、worker 入口、单次分析超时
    worker_node_command: str = "node"
    worker_entry_path: Path | None = None
    worker_timeout_seconds: float = 120.0

    # 需求定位的模型接入。
    # 默认关闭：关闭时接口仍可用，但只能 dry-run（只跑确定性召回，不调模型），
    # 这样召回质量可以独立验证，也不会因为没配 key 就整个功能不可用。
    llm_enabled: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0
    # 喂给判定节点的候选上限：候选越多越容易召回，但也越容易超上下文与引入噪声
    llm_max_candidates: int = 30
    # 单次需求定位在模型侧的调用次数上限，防止重试把一次请求拖成无限等待
    llm_max_calls_per_request: int = 4

    @property
    def llm_ready(self) -> bool:
        return self.llm_enabled and bool(self.llm_api_key)


def resolve_worker_entry(settings: Settings) -> Path:
    """解析 ast-worker 入口；未显式配置时锚定到仓库内的 dist 产物。"""
    if settings.worker_entry_path is not None:
        return Path(settings.worker_entry_path)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "workers" / "ast-worker" / "dist" / "index.js"


@lru_cache
def get_settings() -> Settings:
    return Settings()
