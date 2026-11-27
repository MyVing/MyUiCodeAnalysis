"""对话文档 checkpoint：工具循环续跑、章节复用、ledger 恢复与预算。

覆盖 plan 中的验收点：
1. 第 2 轮模型请求失败后续跑，第 1 轮已完成的工具调用不重复执行；
2. 章节 status=parsed 后重跑，工具循环一次都不再进入；
3. 恢复后 ledger 里的事实仍然在（引文校验边界不丢）；
4. 工具预算用尽抛 tool_budget_exhausted，章节按旧逻辑留空；
5. 仓储幂等与 payload 版本不匹配的安全失败。
"""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.requirement.chat_doc import build_chat_doc
from app.schemas import Project
from app.specdoc.checkpoint import CHECKPOINT_VERSION, ChatDocRun, make_job
from app.specdoc.llm import OpenAICompatibleToolLLM, SpecDocToolError
from app.specdoc.retrieval import EvidenceFact, EvidenceLedger
from app.storage.chatdoc_checkpoint import ChatDocJob
from app.storage.memory import InMemoryChatDocCheckpointRepository
from tests.conftest import settings_with
from tests.specdoc_fixture import spec_graph

_PROJECT = Project(
    id="p_cp",
    name="转账演示",
    root_path="D:/work/demo",
    framework="react",
    created_at="2026-01-01T00:00:00Z",  # type: ignore[arg-type]
)


def _client() -> OpenAICompatibleToolLLM:
    client = OpenAICompatibleToolLLM(
        Settings(_env_file=None, llm_enabled=True, llm_api_key="test")
    )
    client._tool_retry_backoff = 0
    client._tool_retry_attempts = 1
    return client


def _job() -> ChatDocJob:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return ChatDocJob(
        job_id="cdoc_test",
        idempotency_key="k" * 64,
        project_id=_PROJECT.id,
        analysis_id="a1",
        input_hash="k" * 64,
        pipeline_version="chat-doc-v1",
        status="running",
        current_phase="locating",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )


class FakeSearch:
    """记录执行的工具调用，并把每条结果记入自己的 ledger。"""

    def __init__(self) -> None:
        self.ledger = EvidenceLedger()
        self.calls: list[dict] = []

    async def execute(self, name: str, arguments: dict) -> dict:
        self.calls.append(arguments)
        index = len(self.calls)
        fact = EvidenceFact(
            evidence_id=f"e{index}",
            page_id="p1",
            node_id="n1",
            category="uiText",
            text=f"证据-{index}",
        )
        self.ledger.record([fact])
        return {"status": "ok", "facts": [fact.as_dict()]}


def _tool_call(call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "search_evidence", "arguments": '{"category":"uiText"}'},
    }


_TOOLS = [{"type": "function", "function": {"name": "search_evidence"}}]


async def test_resume_skips_completed_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = InMemoryChatDocCheckpointRepository()
    job = await repository.get_or_create_job(_job())
    run = ChatDocRun(repository, job.job_id)
    client = _client()
    first_search = FakeSearch()

    script: list[object] = [
        {"tool_calls": [_tool_call("call-1")]},
        SpecDocToolError("模型请求超时", code="llm_timeout", retryable=True),
    ]

    async def request(payload):
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client, "_request", request)

    with pytest.raises(SpecDocToolError):
        await client.chat_with_tools(
            "system",
            "user",
            tools=_TOOLS,
            executor=first_search.execute,
            checkpoint=run,
            scope="steps",
        )
    assert len(first_search.calls) == 1

    # 续跑用全新的 executor：只有 checkpoint 里的事实能让它「记得」上一轮
    second_search = FakeSearch()
    script.append({"content": '{"steps": []}'})
    result = await client.chat_with_tools(
        "system",
        "user",
        tools=_TOOLS,
        executor=second_search.execute,
        checkpoint=run,
        scope="steps",
    )

    assert result == '{"steps": []}'
    assert second_search.calls == [], "已完成的工具调用不应重放"
    assert list(second_search.ledger.facts) == ["e1"], "ledger 应从 checkpoint 恢复"


async def test_tool_budget_exhausted_is_retryable_off(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    calls = 0

    async def request(payload):
        nonlocal calls
        calls += 1
        return {"tool_calls": [_tool_call(f"call-{calls}")]}

    monkeypatch.setattr(client, "_request", request)
    search = FakeSearch()

    with pytest.raises(SpecDocToolError) as error:
        await client.chat_with_tools(
            "system",
            "user",
            tools=_TOOLS,
            executor=search.execute,
            max_tool_calls=1,
        )
    assert error.value.code == "tool_budget_exhausted"
    assert len(search.calls) == 1


async def test_model_request_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    calls = 0

    async def request(payload):
        nonlocal calls
        calls += 1
        return {"tool_calls": [_tool_call(f"call-{calls}")]}

    monkeypatch.setattr(client, "_request", request)
    search = FakeSearch()

    with pytest.raises(SpecDocToolError) as error:
        await client.chat_with_tools(
            "system",
            "user",
            tools=_TOOLS,
            executor=search.execute,
            max_model_requests=2,
        )
    assert error.value.code == "tool_budget_exhausted"
    assert calls == 2


async def test_job_identity_is_stable_for_same_inputs() -> None:
    repository = InMemoryChatDocCheckpointRepository()
    first_id, first = make_job(
        repository,
        project_id=_PROJECT.id,
        analysis_id="a1",
        feature_text="转账",
        route_path="/transfer",
    )
    second_id, second = make_job(
        repository,
        project_id=_PROJECT.id,
        analysis_id="a1",
        feature_text="转账",
        route_path="/transfer",
    )

    assert first_id == second_id
    assert (await first).job_id == (await second).job_id


async def test_idempotency_conflict_raises() -> None:
    repository = InMemoryChatDocCheckpointRepository()
    job = await repository.get_or_create_job(_job())
    conflicting = ChatDocJob(**{**job.__dict__, "job_id": "other", "input_hash": "x" * 64})

    with pytest.raises(ValueError):
        await repository.get_or_create_job(conflicting)


async def test_version_mismatch_is_treated_as_empty() -> None:
    repository = InMemoryChatDocCheckpointRepository()
    job = await repository.get_or_create_job(_job())
    await repository.save_scope(
        job.job_id, "steps", {"schema": CHECKPOINT_VERSION + 1, "status": "parsed"}, phase="steps"
    )
    run = ChatDocRun(repository, job.job_id)

    assert await run.load_scope("steps") is None
    assert await run.load_parsed("steps") is None
    assert "steps" not in run.resumed_scopes


async def test_parsed_sections_are_reused_across_runs() -> None:
    """同一输入重跑：三章全部命中已解析结果，工具循环不再进入。"""

    class CountingToolLLM:
        def __init__(self) -> None:
            self.entries = 0

        async def chat_with_tools(self, system, user, **kwargs):
            self.entries += 1
            assert kwargs["checkpoint"] is not None
            if "业务操作步骤" in system:
                return json.dumps({"steps": []}, ensure_ascii=False)
            if "前端业务规则" in system:
                return json.dumps({"rules": []}, ensure_ascii=False)
            return json.dumps({"fieldMappings": []}, ensure_ascii=False)

    repository = InMemoryChatDocCheckpointRepository()
    tool_llm = CountingToolLLM()
    settings = settings_with()
    graph = spec_graph()

    first = await build_chat_doc(
        _PROJECT,
        "转账",
        graph,
        tool_llm,  # type: ignore[arg-type] - 无语料问答分支时不会用到
        settings,
        specdoc_llm=tool_llm,  # type: ignore[arg-type]
        route_path="/transfer",
        checkpoints=repository,
    )
    assert tool_llm.entries == 3
    assert first.doc_markdown is not None

    second = await build_chat_doc(
        _PROJECT,
        "转账",
        graph,
        tool_llm,  # type: ignore[arg-type]
        settings,
        specdoc_llm=tool_llm,  # type: ignore[arg-type]
        route_path="/transfer",
        checkpoints=repository,
    )

    assert tool_llm.entries == 3, "已解析章节不应再次调用模型"
    assert second.doc_markdown == first.doc_markdown
    assert second.job_id == first.job_id
    assert set(second.resumed_sections) == {"steps", "rules", "field_mappings"}
