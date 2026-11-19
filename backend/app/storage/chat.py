"""AI 需求助手会话的仓储。

与需求定位任务分仓存放：定位任务是异步任务（状态机驱动、产物按任务取用），
会话是同步聊天的整段回放产物。每项目保留上限 MAX_CHAT_SESSIONS_PER_PROJECT
条，超出按 updatedAt 淘汰最旧——切回旧会话继续聊会刷新其 updatedAt（LRU 语义）。
"""

from typing import Protocol

from app.schemas import ChatSession

MAX_CHAT_SESSIONS_PER_PROJECT = 10


class ChatSessionRepository(Protocol):
    async def upsert(self, session: ChatSession) -> ChatSession:
        """新建或按 id 整体替换；写入时维持每项目上限，超出淘汰最旧。"""

    async def get(self, session_id: str) -> ChatSession | None: ...

    async def list_by_project(self, project_id: str) -> list[ChatSession]:
        """按 updatedAt 倒序（最近活跃在前）。"""

    async def delete(self, session_id: str) -> bool: ...
