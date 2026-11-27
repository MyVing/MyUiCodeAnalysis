"""Stable identity and payload-version boundary for chat document checkpoints."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.storage.chatdoc_checkpoint import ChatDocCheckpointRepository, ChatDocJob

CHECKPOINT_VERSION = 1
PIPELINE_VERSION = "chat-doc-v1"
SCOPES = frozenset({"steps", "rules", "field_mappings"})


def make_job(
    repository: ChatDocCheckpointRepository,
    *,
    project_id: str,
    analysis_id: str | None,
    feature_text: str,
    route_path: str | None,
    ttl_minutes: int = 60,
) -> tuple[str, Any]:
    normalized = {
        "project_id": project_id,
        "analysis_id": analysis_id,
        "feature_text": feature_text,
        "route_path": route_path,
        "pipeline_version": PIPELINE_VERSION,
    }
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    job = ChatDocJob(
        job_id=f"cdoc_{digest[:32]}",
        idempotency_key=digest,
        project_id=project_id,
        analysis_id=analysis_id,
        input_hash=digest,
        pipeline_version=PIPELINE_VERSION,
        status="running",
        current_phase="locating",
        created_at=now,
        expires_at=now + timedelta(minutes=max(1, ttl_minutes)),
    )
    return job.job_id, repository.get_or_create_job(job)


class ChatDocRun:
    def __init__(self, repository: ChatDocCheckpointRepository, job_id: str) -> None:
        self.repository = repository
        self.job_id = job_id
        self.resumed_scopes: set[str] = set()

    async def load_scope(self, scope: str) -> dict[str, Any] | None:
        """读取章节状态；命中即记账为「本次确实恢复过」的章节。"""
        self._check_scope(scope)
        payload = await self._load(scope)
        if payload is not None:
            self.resumed_scopes.add(scope)
        return payload

    async def _load(self, scope: str) -> dict[str, Any] | None:
        payload = await self.repository.load_scope(self.job_id, scope)
        if payload is None:
            return None
        if payload.get("schema") != CHECKPOINT_VERSION:
            return None
        return payload

    async def save_scope(self, scope: str, payload: dict[str, Any]) -> None:
        self._check_scope(scope)
        await self.repository.save_scope(self.job_id, scope, payload, phase=scope)

    async def mark_parsed(self, scope: str, parsed: dict[str, Any]) -> None:
        self._check_scope(scope)
        # 用 _load 而不是 load_scope：本章节是刚算出来的，不该记成「恢复」
        saved = await self._load(scope) or {"schema": CHECKPOINT_VERSION}
        saved.update({"schema": CHECKPOINT_VERSION, "status": "parsed", "parsed": parsed})
        await self.save_scope(scope, saved)

    async def load_parsed(self, scope: str) -> dict[str, Any] | None:
        """已完成章节的最终 JSON；没有或版本不符时返回 None。"""
        self._check_scope(scope)
        payload = await self._load(scope)
        if payload is None or payload.get("status") != "parsed":
            return None
        self.resumed_scopes.add(scope)
        parsed = payload.get("parsed")
        return parsed if isinstance(parsed, dict) else None

    async def finish(self, status: str) -> None:
        await self.repository.finish_job(self.job_id, status)

    @staticmethod
    def _check_scope(scope: str) -> None:
        if scope not in SCOPES:
            raise ValueError(f"unsupported chat doc checkpoint scope: {scope}")
