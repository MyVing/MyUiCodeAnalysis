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

    # Git 接入：克隆产物落在受管目录，root_path 指向它，避免污染用户工作区。
    # 默认在用户主目录下，可用 APP_REPOS_DIR 覆盖。
    repos_dir: Path = Field(default_factory=lambda: Path.home() / ".uicodeanalysis" / "repos")
    git_command: str = "git"
    git_timeout_seconds: float = 180.0

    # runtime-worker 子进程：采集要托管 dev server + 浏览器，整体超时更宽松
    runtime_worker_entry_path: Path | None = None
    runtime_worker_timeout_seconds: float = 300.0
    # 页面加载后的静默观察窗口（毫秒）：等待被动网络请求落定
    runtime_settle_ms: int = 3000

    # AI 自动探索（P5）：复用 runtime-worker 的 discover / explore 模式。
    # 探索含逐步截图与计划执行，整体超时比被动采集更宽松。
    explore_worker_entry_path: Path | None = None
    explore_worker_timeout_seconds: float = 600.0
    # discover 阶段的可交互元素枚举上限
    explore_max_elements: int = 200
    # 截图等探索产物目录（每任务一个子目录）
    explore_artifacts_dir: Path = Field(
        default_factory=lambda: Path.home() / ".uicodeanalysis" / "explore"
    )

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
    # 图产物容量保护：超过 legacy 阈值的 JSON 自动写入 graph_revisions/chunks。
    # 阈值按 UTF-8 字节计，必须低于数据库 max_allowed_packet 并留出协议开销。
    graph_legacy_max_bytes: int = 4 * 1024 * 1024
    graph_chunk_bytes: int = 512 * 1024
    graph_max_bytes: int = 512 * 1024 * 1024
    graph_chunked_enabled: bool = True
    graph_chunked_read_enabled: bool = True
    graph_legacy_fallback_enabled: bool = True
    graph_writing_ttl_seconds: int = 3600

    # 需求定位的模型接入。
    # 默认关闭：关闭时接口仍可用，但只能 dry-run（只跑确定性召回，不调模型），
    # 这样召回质量可以独立验证，也不会因为没配 key 就整个功能不可用。
    llm_enabled: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0
    # 输出 token 上限：判定产物只是若干条 JSON 结论，但 8 个功能点满载时也可能
    # 上千 token。截断会让整份输出变成非法 JSON，所以留足余量而不是卡着成本设。
    llm_max_tokens: int = 4096
    # 用 response_format=json_object 约束输出。少数 OpenAI 兼容端点不支持该字段
    # （会返回 400），此时可关掉退化为纯 prompt 约束。
    llm_response_format: bool = True
    # 喂给判定节点的候选上限：候选越多越容易召回，但也越容易超上下文与引入噪声
    llm_max_candidates: int = 30
    # 单次需求定位在模型侧的调用次数上限，防止重试把一次请求拖成无限等待
    llm_max_calls_per_request: int = 4
    # 交给模型的页面数上限（页面 + 链路索引）。超过时按词法得分预筛并显式告警：
    # 一个中型 SPA 有几十上百个路由，全量进 prompt 会挤掉链路细节。
    # 设 0 表示不预筛。
    llm_max_pages: int = 60
    # 对话文档（/requirement-chat 查文档）的推断续跑参数。单章工具循环在
    # 传输层重试耗尽后，会带着 checkpoint 重新进入循环；这里的次数与预算
    # 都是「单章」口径，超出则该章留空 + warning，与旧行为一致。
    chatdoc_resume_attempts: int = 2
    chatdoc_max_model_requests: int | None = 12
    chatdoc_max_tool_calls: int | None = 40
    # chat_doc_jobs / chat_doc_checkpoints 的保留时长：过期的 job 启动时清理
    # （级联删 checkpoint），不引入定时器。
    chatdoc_checkpoint_ttl_minutes: int = 60

    # 分析成功后是否异步生成页面画像（每页一句业务描述，缓存给在线定位用）。
    # 默认关：打开意味着「每次分析多一次模型调用」，不该是隐式行为。
    # 即使打开，未配置 key 时也不会触发（llm_ready 为假）。
    llm_profile_enabled: bool = False

    # 按需求采集：requirement 文本入口需要先等需求定位任务跑完（轮询间隔 0.3s）
    requirement_collect_locate_timeout_seconds: float = 180.0

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


def resolve_explore_worker_entry(settings: Settings) -> Path:
    """解析探索 Worker 入口（与 runtime-worker 同一进程，模式不同而已）。"""
    if settings.explore_worker_entry_path is not None:
        return Path(settings.explore_worker_entry_path)
    return resolve_runtime_worker_entry(settings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
