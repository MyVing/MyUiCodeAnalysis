"""应用配置。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    # runtime-worker 子进程：采集要托管 dev server + 浏览器，整体超时更宽松
    runtime_worker_entry_path: Path | None = None
    runtime_worker_timeout_seconds: float = 300.0
    # 页面加载后的静默观察窗口（毫秒）：等待被动网络请求落定
    runtime_settle_ms: int = 3000

    # 持久化。db_url 缺省（None）→ 内存存储，重启即清空；
    # 配置后走 MySQL，例如：
    #   APP_DB_URL=mysql+aiomysql://user:password@127.0.0.1:3306/uicodeanalysis?charset=utf8mb4
    # 驱动用 aiomysql（纯 Python）而非 asyncmy：Windows 上无编译依赖。
    db_url: str | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 5
    # MySQL 8 空闲 8 小时会断连；recycle + pre_ping 双保险防 "server has gone away"
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False

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

    @property
    def storage_backend(self) -> Literal["memory", "mysql"]:
        return "mysql" if self.db_url else "memory"


def resolve_worker_entry(settings: Settings) -> Path:
    """解析 ast-worker 入口；未显式配置时锚定到仓库内的 dist 产物。"""
    if settings.worker_entry_path is not None:
        return Path(settings.worker_entry_path)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "workers" / "ast-worker" / "dist" / "index.js"


def resolve_runtime_worker_entry(settings: Settings) -> Path:
    """解析 runtime-worker 入口；锚定规则与 ast-worker 一致。"""
    if settings.runtime_worker_entry_path is not None:
        return Path(settings.runtime_worker_entry_path)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "workers" / "runtime-worker" / "dist" / "index.js"


@lru_cache
def get_settings() -> Settings:
    return Settings()
