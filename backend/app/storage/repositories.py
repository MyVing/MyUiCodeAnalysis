"""仓储接口：存储实现可替换，API 与前端不感知具体数据库。

全部为异步签名：MySQL 实现天然 async，内存实现以 asyncio.Lock 保护，
避免同步 Protocol 被迫用线程池桥接。
"""

from typing import TYPE_CHECKING, Protocol

from app.contracts import IRGraph
from app.schemas import AnalysisTask, Project

if TYPE_CHECKING:
    # 画像产物由 app.requirement.page_profile 定义（存储层与生成方共用的数据形状）。
    # 这里只要类型，不在运行时导入：存储层不该为了一个注解把业务模块拉进导入链。
    from app.requirement.page_profile import ProfilePayload


class DuplicateRemoteUrlError(Exception):
    """同一 Git 地址只能接入一次；内存校验与 MySQL 唯一索引都以此异常表达。"""


class ProjectRepository(Protocol):
    async def list(self) -> list[Project]: ...

    async def get(self, project_id: str) -> Project | None: ...

    async def get_by_remote(self, remote_url: str) -> Project | None:
        """按归一化后的远程地址查重；本地目录项目永远返回 None。"""

    async def add(self, project: Project) -> Project:
        """新增项目；remote_url 已存在时抛 DuplicateRemoteUrlError。"""

    async def update(self, project: Project) -> Project: ...

    async def delete(self, project_id: str) -> bool:
        """删除项目记录；返回是否真的删了（不存在返回 False）。"""


class AnalysisRepository(Protocol):
    async def purge_project(self, project_id: str) -> None:
        """清空该项目的分析任务、图产物与页画像。

        MySQL 实现靠 projects 外键 ON DELETE DELETE CASCADE，此方法是内存
        仓储（无外键）的等价物；删除项目时两条存储路径行为一致。
        """
    async def add_task(self, task: AnalysisTask) -> AnalysisTask | None:
        """登记新任务；该项目已有 queued/running 任务时返回 None（并发冲突）。"""

    async def get_task(self, task_id: str) -> AnalysisTask | None: ...

    async def update_task(self, task: AnalysisTask) -> AnalysisTask: ...

    async def list_by_project(self, project_id: str) -> list[AnalysisTask]: ...

    async def save_graph(self, project_id: str, graph: IRGraph) -> None: ...

    async def save_merged_graph(self, project_id: str, graph: IRGraph) -> None:
        """保存静态图 + 运行时证据的合并版本；同一 analysisId 多次采集后覆盖。"""

    async def get_graph(self, project_id: str, analysis_id: str) -> IRGraph | None: ...

    async def get_merged_graph(self, project_id: str, analysis_id: str) -> IRGraph | None: ...

    async def get_latest_graph(self, project_id: str) -> IRGraph | None:
        """最新图产物：merged 存在且晚于最新 static 时优先返回 merged。"""

    async def save_page_profiles(self, project_id: str, payload: "ProfilePayload") -> None:
        """保存一份页画像；同一 analysisId 覆盖。"""

    async def get_page_profiles(
        self, project_id: str, analysis_id: str
    ) -> "ProfilePayload | None":
        """按图产物取画像。定位与图版本不一致时应当拿不到，而不是拿到旧描述。"""
