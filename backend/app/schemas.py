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


class Project(ApiModel):
    id: str
    name: str
    root_path: str
    framework: Literal["react", "next"]
    created_at: datetime
    storage: Literal["memory", "mysql"] = "memory"


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


class SourceExcerpt(ApiModel):
    """源码片段：前端点开「文件:行」时读这一段。"""

    file: str
    start_line: int
    end_line: int
    content: str
    truncated: bool = False
