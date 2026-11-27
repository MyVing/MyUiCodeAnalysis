"""Checkpoint storage for synchronous chat-generated spec documents."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class ChatDocJob:
    job_id: str
    idempotency_key: str
    project_id: str
    analysis_id: str | None
    input_hash: str
    pipeline_version: str
    status: str
    current_phase: str
    created_at: datetime
    expires_at: datetime


class ChatDocCheckpointRepository(Protocol):
    async def get_or_create_job(self, job: ChatDocJob) -> ChatDocJob: ...

    async def load_job(self, job_id: str) -> ChatDocJob | None: ...

    async def load_scope(self, job_id: str, scope: str) -> dict[str, Any] | None: ...

    async def save_scope(
        self, job_id: str, scope: str, payload: dict[str, Any], *, phase: str
    ) -> None: ...

    async def finish_job(self, job_id: str, status: str) -> None: ...

    async def purge_expired(self) -> int: ...
