"""需求定位 API。

三类资源刻意分开：
  /projects/{id}/requirements      项目维度：发起与列出
  /requirements/{taskId}           任务维度：查询状态与产物
  /projects/{id}/chat-sessions     AI 需求助手会话：保存与回放
这样前端「发起 → 轮询 taskId → 取 map」不需要回填项目 id。
"""

import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status

from app.api.deps import (
    AnalysisRepositoryDep,
    ChatSessionRepositoryDep,
    ProjectServiceDep,
    RequirementChatServiceDep,
    RequirementLocateServiceDep,
)
from app.api.v1.projects import require_project
from app.contracts import RequirementMap
from app.core.tracing import elapsed_ms, log_event
from app.requirement.chat import RequirementChatPreconditionError
from app.requirement.llm import LLMCallError
from app.requirement.service import RequirementPreconditionError
from app.schemas import (
    ChatMessage,
    ChatSession,
    ChatSessionSummary,
    ChatSessionSummaryList,
    ChatSessionUpsert,
    RequirementChatResult,
    RequirementChatStart,
    RequirementLocateStart,
    RequirementTask,
    RequirementTaskList,
)

router = APIRouter(tags=["requirements"])
logger = logging.getLogger(__name__)

_PRECONDITION_STATUS: dict[str, int] = {
    "graph_missing": status.HTTP_409_CONFLICT,
    "graph_is_sample": status.HTTP_409_CONFLICT,
    "llm_unavailable": status.HTTP_409_CONFLICT,
    "analysis_not_found": status.HTTP_404_NOT_FOUND,
}

# 会话标题缺省取首条用户消息的前缀截断
_TITLE_PREFIX_LENGTH = 24


def _derive_title(messages: list[ChatMessage]) -> str:
    for message in messages:
        if message.role == "user":
            return message.content[:_TITLE_PREFIX_LENGTH].strip() or "新对话"
    return "新对话"


@router.post(
    "/projects/{project_id}/requirements",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RequirementTask,
    responses={
        404: {"description": "项目或指定的分析产物不存在"},
        409: {"description": "无图 / 图为示例数据 / 模型未配置"},
    },
)
async def start_requirement_locate(
    project_id: str,
    payload: RequirementLocateStart,
    service: ProjectServiceDep,
    locate_service: RequirementLocateServiceDep,
) -> RequirementTask:
    project = await require_project(project_id, service)
    try:
        return await locate_service.start(project, payload)
    except RequirementPreconditionError as error:
        raise HTTPException(
            status_code=_PRECONDITION_STATUS.get(error.code, status.HTTP_409_CONFLICT),
            detail=error.message,
        ) from None


@router.post(
    "/projects/{project_id}/requirement-chat",
    response_model=RequirementChatResult,
    responses={
        404: {"description": "项目不存在"},
        502: {"description": "模型调用失败"},
        503: {"description": "模型未配置"},
    },
)
async def requirement_chat(
    project_id: str,
    payload: RequirementChatStart,
    service: ProjectServiceDep,
    chat_service: RequirementChatServiceDep,
    analyses: AnalysisRepositoryDep,
) -> RequirementChatResult:
    """需求对话：澄清需求、查询交互文档或基于只读工具分析业务逻辑。

    项目已有解析产物时先做意图判定；图缺失则退化为纯澄清对话。
    """
    project = await require_project(project_id, service)
    graph = await analyses.get_latest_graph(project_id)
    messages = [{"role": item.role, "content": item.content} for item in payload.messages]
    started = time.perf_counter()
    checkpoint_meta: dict[str, object] = {}
    log_event(
        logger,
        logging.INFO,
        "request_start",
        project_id=project_id,
        route_path=payload.route_path,
        message_count=len(messages),
        message_characters=sum(len(message["content"]) for message in messages),
        graph_loaded=graph is not None,
    )
    try:
        reply, draft, doc_markdown, doc_page = await chat_service.chat(
            messages,
            project=project,
            graph=graph,
            route_path=payload.route_path,
            checkpoint_meta=checkpoint_meta,
        )
    except RequirementChatPreconditionError as error:
        log_event(
            logger,
            logging.WARNING,
            "request_failed",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=error.code,
            elapsed_ms=elapsed_ms(started),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error.message
        ) from None
    except LLMCallError as error:
        log_event(
            logger,
            logging.WARNING,
            "request_failed",
            status_code=status.HTTP_502_BAD_GATEWAY,
            code=error.code,
            retryable=error.retryable,
            elapsed_ms=elapsed_ms(started),
            job_id=checkpoint_meta.get("job_id"),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=error.message) from None
    log_event(
        logger,
        logging.INFO,
        "request_done",
        elapsed_ms=elapsed_ms(started),
        reply_characters=len(reply),
        has_draft=draft is not None,
        has_doc=doc_markdown is not None,
        doc_page=doc_page,
        job_id=checkpoint_meta.get("job_id"),
        resumed_sections=checkpoint_meta.get("resumed_sections"),
    )
    return RequirementChatResult(
        reply=reply,
        draft_requirement=draft,
        doc_markdown=doc_markdown,
        doc_page=doc_page,
    )


@router.get("/projects/{project_id}/requirements", response_model=RequirementTaskList)
async def list_requirement_tasks(
    project_id: str,
    service: ProjectServiceDep,
    locate_service: RequirementLocateServiceDep,
) -> RequirementTaskList:
    await require_project(project_id, service)
    return RequirementTaskList(items=await locate_service.list_tasks(project_id))


@router.get(
    "/requirements/{task_id}",
    response_model=RequirementTask,
    responses={404: {"description": "任务不存在"}},
)
async def get_requirement_task(
    task_id: str, locate_service: RequirementLocateServiceDep
) -> RequirementTask:
    task = await locate_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"需求定位任务 {task_id} 不存在"
        )
    return task


@router.get(
    "/requirements/{task_id}/map",
    response_model=RequirementMap,
    response_model_exclude_none=True,
    responses={
        404: {"description": "任务不存在，或产物尚未生成"},
        409: {"description": "任务未成功完成"},
    },
)
async def get_requirement_map(
    task_id: str, locate_service: RequirementLocateServiceDep
) -> RequirementMap:
    task = await locate_service.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"需求定位任务 {task_id} 不存在"
        )
    if task.status != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态为 {task.status}，尚无可读取的定位产物。",
        )

    requirement_map = await locate_service.get_map_by_task(task_id)
    if requirement_map is None:
        # 任务成功但产物已被有界淘汰：如实说明，而不是假装「还在准备」
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="定位产物已因保留上限被清理，请重新发起定位。",
        )
    return requirement_map


@router.post(
    "/projects/{project_id}/chat-sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=ChatSession,
    responses={404: {"description": "项目不存在"}},
)
async def upsert_chat_session(
    project_id: str,
    payload: ChatSessionUpsert,
    service: ProjectServiceDep,
    sessions: ChatSessionRepositoryDep,
) -> ChatSession:
    """保存（新建或整体替换）一个 AI 需求助手会话。

    前端在收到 assistant 回复后整段回传；id 省略表示新建。仓储写入时维持
    每项目最多 10 条，超出按 updatedAt 淘汰最旧——被淘汰会话的 id 再次出现
    时按新建处理（幂等）。requirement-chat 接口本身无状态，本接口只是旁路
    落库，不参与对话。
    """
    await require_project(project_id, service)

    existing: ChatSession | None = None
    if payload.id is not None:
        existing = await sessions.get(payload.id)
        # id 属于其他项目（或已被淘汰后前端仍持有）：按新建处理，不跨项目覆盖
        if existing is not None and existing.project_id != project_id:
            existing = None

    now = datetime.now(UTC)
    session = ChatSession(
        id=payload.id if existing is not None else f"chat_{uuid4().hex[:12]}",
        project_id=project_id,
        title=payload.title or _derive_title(payload.messages),
        messages=payload.messages,
        draft=payload.draft,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
    )
    return await sessions.upsert(session)


@router.get(
    "/projects/{project_id}/chat-sessions",
    response_model=ChatSessionSummaryList,
    responses={404: {"description": "项目不存在"}},
)
async def list_chat_sessions(
    project_id: str,
    service: ProjectServiceDep,
    sessions: ChatSessionRepositoryDep,
) -> ChatSessionSummaryList:
    """会话摘要列表（按 updatedAt 倒序）：侧栏渲染只需要标题与时间，
    消息正文（可能含整份规格文档）由详情接口按需取。"""
    await require_project(project_id, service)
    items = await sessions.list_by_project(project_id)
    return ChatSessionSummaryList(
        items=[
            ChatSessionSummary(
                id=item.id,
                project_id=item.project_id,
                title=item.title,
                message_count=len(item.messages),
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in items
        ]
    )


@router.get(
    "/chat-sessions/{session_id}",
    response_model=ChatSession,
    responses={404: {"description": "会话不存在"}},
)
async def get_chat_session(session_id: str, sessions: ChatSessionRepositoryDep) -> ChatSession:
    session = await sessions.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"会话 {session_id} 不存在",
        )
    return session


@router.delete(
    "/chat-sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "会话不存在"}},
)
async def delete_chat_session(session_id: str, sessions: ChatSessionRepositoryDep) -> None:
    deleted = await sessions.delete(session_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"会话 {session_id} 不存在",
        )
