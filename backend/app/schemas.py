"""API 层数据契约，统一使用 camelCase 输出。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.contracts import Evidence, RequirementWarning


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
    # 大型 monorepo 默认只分析主应用，避免多个 app、测试和 Storybook 被静默合并。
    source_roots: list[str] | None = Field(default=None, max_length=32)
    exclude_tests: bool = True
    exclude_storybook: bool = True
    scope_mode: Literal["all", "primary"] = "primary"


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


class ChatMessage(ApiModel):
    """对话历史中的一条消息。会话状态在前端持有，后端每次收全量历史；
    docMarkdown/docPage 是会话落库时 assistant 消息携带的规格文档产物，
    requirement-chat 接口本身只消费 role/content。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=10000)
    doc_markdown: str | None = Field(default=None, max_length=200000)
    doc_page: str | None = Field(default=None, max_length=200)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("消息内容不能为空")
        return stripped


class RequirementChatStart(ApiModel):
    """一次需求对话的输入：全量历史 + 可选的路由直达定位。

    上限 40 条：防止单请求被塞入超长会话烧掉模型预算——真聊到 40 条
    说明该提炼草稿了，前端应该在那时点「填入输入框」。
    """

    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    # 填写后按代码图中的 route 精确定位，绕过意图改写与词法召回。
    route_path: str | None = Field(default=None, max_length=512)

    @field_validator("route_path")
    @classmethod
    def strip_route_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class RequirementChatResult(ApiModel):
    """一次需求对话的输出。draftRequirement 非空表示模型认为信息已足够成文；
    docMarkdown 非空表示命中了项目页面并就地生成了规格文档（业务操作步骤 +
    前端交互视图），docPage 是命中的页面名。"""

    reply: str
    draft_requirement: str | None = None
    doc_markdown: str | None = None
    doc_page: str | None = None


class ChatSessionSummary(ApiModel):
    """会话摘要：历史侧栏列表用，不含消息正文——docMarkdown 可能很大，
    列表只需要标题与时间。"""

    id: str
    project_id: str
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class ChatSessionSummaryList(ApiModel):
    items: list[ChatSessionSummary] = Field(default_factory=list)


class ChatSession(ApiModel):
    """一个完整会话：全部消息 + 最新草稿。draft 是最近一条 assistant 回复
    产出的需求草稿，切回会话时前端据此恢复草稿横幅。"""

    id: str
    project_id: str
    title: str
    messages: list[ChatMessage]
    draft: str | None = None
    created_at: datetime
    updated_at: datetime


class ChatSessionUpsert(ApiModel):
    """保存会话的输入：前端在收到 assistant 回复后整段回传。

    id 省略表示新建；title 省略时服务端取首条用户消息截断生成。
    消息上限与 RequirementChatStart 同为 40 条，保证重放会话可直接继续对话。
    """

    id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=120)
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    draft: str | None = Field(default=None, max_length=10000)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class SpecDocStart(ApiModel):
    """一次规格文档装配的输入：指定来源需求定位产物。

    定位产物（RequirementMap）必须已存在且其 analysisId 与项目最新图一致，
    否则服务会以前置校验拒绝（specdoc_map_stale）。`dryRun=True` 只跑确定性
    装配（要素 / 事件 / 接口映射），模型推断章节（步骤 / 规则 / 字段映射）留空。
    """

    requirement_id: str = Field(min_length=1, max_length=128)
    dry_run: bool = False

    @field_validator("requirement_id")
    @classmethod
    def strip_requirement_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("requirementId 不能为空")
        return stripped


class SpecDocTask(ApiModel):
    """规格文档装配任务的状态。字段形状与 RequirementTask 对齐，前端复用同一套轮询逻辑。"""

    id: str
    project_id: str
    requirement_id: str
    status: TaskStatus
    dry_run: bool
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    element_count: int | None = None
    event_count: int | None = None
    rule_count: int | None = None
    warning_count: int | None = None


class SpecDocTaskList(ApiModel):
    items: list[SpecDocTask] = Field(default_factory=list)


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


class RequirementCollectStart(ApiModel):
    """按需求采集：需求描述 → 定位页面 → 反查路由 → 逐页发起运行时采集。

    requirement 与 requirement_task_id 二选一：前者先跑一次需求定位（需配置模型），
    后者复用已完成定位任务的 RequirementMap。注意 `dryRun=True` 的定位只跑
    确定性召回、`matches` 必为空，会导致 no_matched_pages——文本入口请保持
    dryRun=False。

    baseUrl 必填且服务需已在运行——采集走 url 直连模式；托管 dev server
    模式（command）Worker 只会访问根路径，无法定位到具体路由，不在本入口范围。
    """

    requirement: str | None = Field(default=None, max_length=10000)
    requirement_task_id: str | None = Field(default=None, max_length=128)
    base_url: str = Field(max_length=2048)
    dry_run: bool = False
    settle_ms: int | None = Field(default=None, ge=100, le=120_000)

    @field_validator("requirement")
    @classmethod
    def strip_requirement(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped.startswith(("http://", "https://")):
            raise ValueError("baseUrl 必须以 http:// 或 https:// 开头")
        return stripped


class SkippedRoute(ApiModel):
    """未能转成采集任务的目标页面，带机器可读原因。"""

    page_node_id: str
    route_path: str | None = None
    reason: str
    message: str


class RequirementCollectResult(ApiModel):
    """一次按需求采集的编排结果：已发起的任务 + 被跳过的页面（各自带原因）。"""

    requirement_task_id: str
    items: list[RuntimeTask] = Field(default_factory=list)
    skipped: list[SkippedRoute] = Field(default_factory=list)


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


class ApiUsagePage(ApiModel):
    """接口反查的一条命中：某页面（经某条调用链）请求了这个接口（6.11）。"""

    page_node_id: str
    route_node_id: str | None = None
    # 完整调用链 nodeIds / edgeIds：api → … → 页面（→ 路由），每一环都可回图核对
    path: list[str]
    edge_path: list[str]
    confidence: float
    # URL 只做了后缀匹配（method 未提供或走 match=suffix）：结论降档，如实标注
    url_suffix_match: bool = False
    # 运行时双证：trace 的请求 URL 与页面 URL 同时出现
    runtime_backed: bool = False
    evidence: list[Evidence] = Field(default_factory=list)


class ApiUsageResult(ApiModel):
    pages: list[ApiUsagePage] = Field(default_factory=list)
    warnings: list[RequirementWarning] = Field(default_factory=list)
