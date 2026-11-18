"""探索任务的仓储协议：任务状态 + 产物（ExploreArtifact）+ 产物目录。

与 runtime 仓储同构：产物按 task_id 落库；截图等二进制文件由 Worker 写入
artifacts_dir，后端只记录路径并提供下载端点。
"""

from typing import Any, Protocol


class ExploreRepository(Protocol):
    async def add_task(self, task: Any) -> Any:
        """登记新任务。探索允许并发（每次独立进程 + 独立产物目录），无冲突返回。"""

    async def get_task(self, task_id: str) -> Any | None: ...

    async def update_task(self, task: Any) -> Any: ...

    async def list_by_project(self, project_id: str) -> list[Any]: ...

    async def save_artifact(self, artifact: dict[str, Any], artifacts_dir: str) -> None:
        """整体落库产物（JSON），并记录产物目录供截图下载端点使用。"""

    async def get_artifact(self, task_id: str) -> dict[str, Any] | None: ...

    async def get_artifacts_dir(self, task_id: str) -> str | None: ...

    async def purge_project(self, project_id: str) -> None:
        """清空该项目的探索任务与产物记录（MySQL 靠外键级联，内存仓储显式清）。"""
