"""API 层数据契约，统一使用 camelCase 输出。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class HealthStatus(ApiModel):
    status: Literal["ok"] = "ok"
    name: str
    version: str
    storage: Literal["memory", "mysql"]
    server_time: datetime


class ProjectCreate(ApiModel):
    name: str = Field(min_length=1, max_length=80)
    root_path: str = Field(min_length=1, max_length=512)
    framework: Literal["react", "next"] = "react"

    @field_validator("name", "root_path")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped


class ProjectGitCreate(ApiModel):
    """按 Git 地址接入：平台负责克隆到受管目录，后续更新以远端为准。

    私有仓库支持两种认证写法：`username` + `password`（密码也可以是平台签发的
    访问令牌），或只给 `token`（等价于 `oauth2:<token>`）。凭据只在本次克隆命令
    中使用，不落库、不回显；公开仓库全部留空即可。
    """

    git_url: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, max_length=80)
    branch: str | None = Field(default=None, max_length=128)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=512)
    token: str | None = Field(default=None, max_length=512)
    framework: Literal["react", "next"] | None = None

    @field_validator("git_url")
    @classmethod
    def strip_git_url(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Git 地址不能为空")
        return stripped

    @field_validator("name", "branch", "username", "token")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("password")
    @classmethod
    def empty_password_to_none(cls, value: str | None) -> str | None:
        # 密码不做 strip：首尾空格可能是凭据的一部分，只把空串归一成 None
        return value or None


class Project(ApiModel):
    id: str
    name: str
    root_path: str
    framework: Literal["react", "next"]
    created_at: datetime
    storage: Literal["memory", "mysql"] = "memory"
    # local = 用户登记的本地目录；git = 平台克隆的受管仓库
    source: Literal["local", "git"] = "local"
    remote_url: str | None = None
    git_branch: str | None = None
    commit_sha: str | None = None
    last_synced_at: datetime | None = None


class PluginDependency(ApiModel):
    plugin_id: str = Field(min_length=1)
    version_range: str | None = None


class PluginManifest(ApiModel):
    """插件清单：形状由 shared/schemas/plugin.schema.json 定义。

    注意 API 层额外携带 `enabled`（按项目保存的启停状态），该字段不属于
    清单本身，而是项目维度的配置，故不出现在 plugin schema 中。
    """

    plugin_id: str = Field(min_length=1)
    name: str
    type: Literal["ui-framework", "runtime", "analyzer"]
    version: str
    entrypoint: str | None = None
    dependencies: list[PluginDependency] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    schema_version: str
    enabled: bool = True


class PluginList(ApiModel):
    items: list[PluginManifest] = Field(default_factory=list)
    isolation: Literal["subprocess"] = "subprocess"
    note: str


TaskStatus = Literal["queued", "running", "succeeded", "failed"]


class AnalysisStart(ApiModel):
    """分析任务的可选输入；需求与 Commit 字段本期仅接收存储，为需求驱动 / Diff 分析模式预留。"""

    requirement: str | None = Field(default=None, max_length=10000)
    base_commit: str | None = Field(default=None, max_length=128)
    target_commit: str | None = Field(default=None, max_length=128)


class AnalysisTask(ApiModel):
    id: str
    project_id: str
    analysis_id: str
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    node_count: int | None = None
    edge_count: int | None = None
    warning_count: int | None = None
    requirement: str | None = None
    base_commit: str | None = None
    target_commit: str | None = None


class AnalysisTaskList(ApiModel):
    items: list[AnalysisTask] = Field(default_factory=list)


class RequirementLocateStart(ApiModel):
    """一次需求定位的输入。

    `dryRun` 显式区分「只跑确定性召回」与「调用模型判定」：前者不花钱、可重复，
    用于验证召回质量与演示；后者需要先配置模型。
    """

    requirement: str = Field(min_length=1, max_length=10000)
    analysis_id: str | None = Field(default=None, max_length=128)
    dry_run: bool = False

    @field_validator("requirement")
    @classmethod
    def strip_requirement(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("需求描述不能为空")
        return stripped


class RequirementTask(ApiModel):
    """需求定位任务的状态。字段形状与 AnalysisTask 对齐，前端可复用同一套轮询逻辑。"""

    id: str
    project_id: str
    requirement_id: str
    status: TaskStatus
    dry_run: bool
    requirement: str
    analysis_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    candidate_count: int | None = None
    match_count: int | None = None
    warning_count: int | None = None


class RequirementTaskList(ApiModel):
    items: list[RequirementTask] = Field(default_factory=list)


class RuntimeStart(ApiModel):
    """一次运行时采集的输入。url 与 command 至少给一个：

    - `command`（如 "npm run dev"）：Worker 在项目根目录托管启动 dev server，
      采集完成后杀掉进程树；
    - `url`：直接访问已在运行的页面，不托管 dev server。
    """

    command: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2048)
    analysis_id: str | None = Field(default=None, max_length=128)
    settle_ms: int | None = Field(default=None, ge=100, le=120_000)

    @field_validator("command", "url")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class RuntimeTask(ApiModel):
    """运行时采集任务的状态。字段形状与 AnalysisTask / RequirementTask 对齐，
    前端可复用同一套轮询逻辑。"""

    id: str
    project_id: str
    trace_id: str
    status: TaskStatus
    command: str | None = None
    url: str | None = None
    analysis_id: str | None = None
    settle_ms: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    component_count: int | None = None
    network_count: int | None = None
    console_count: int | None = None
    interaction_count: int | None = None


class RuntimeTaskList(ApiModel):
    items: list[RuntimeTask] = Field(default_factory=list)


class ExploreStart(ApiModel):
    """一次 AI 自动探索的输入。url 与 command 至少给一个（与 RuntimeStart 同一约定）。

    - `dryRun=True`：只枚举元素并生成探索计划（含场景与用例清单），不在浏览器里
      执行交互——不花钱即可审阅计划质量；
    - `dryRun=False`：生成计划后立即确定性执行，产出逐步结果与截图。
    """

    goal: str = Field(min_length=1, max_length=2000)
    command: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2048)
    analysis_id: str | None = Field(default=None, max_length=128)
    dry_run: bool = False
    max_steps: int | None = Field(default=None, ge=1, le=50)

    @field_validator("goal")
    @classmethod
    def strip_goal(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("探索目标不能为空")
        return stripped

    @field_validator("command", "url")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ExploreTask(ApiModel):
    """探索任务的状态。字段形状与 RuntimeTask 对齐，前端可复用同一套轮询逻辑。

    计数聚合来自探索产物：elementCount 是 discovery 的元素数，stepCount /
    successCount / failedCount 来自计划执行结果，screenshotCount 是截图数。
    """

    id: str
    project_id: str
    goal: str
    status: TaskStatus
    command: str | None = None
    url: str | None = None
    analysis_id: str | None = None
    dry_run: bool
    max_steps: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    element_count: int | None = None
    step_count: int | None = None
    success_count: int | None = None
    failed_count: int | None = None
    screenshot_count: int | None = None


class ExploreTaskList(ApiModel):
    items: list[ExploreTask] = Field(default_factory=list)


class SourceExcerpt(ApiModel):
    """源码片段：前端点开「文件:行」时读这一段。"""

    file: str
    start_line: int
    end_line: int
    content: str
    truncated: bool = False
