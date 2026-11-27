from __future__ import annotations

import httpx

from scripts import trace_requirement_chat


def test_trace_sends_real_http_request_and_prints_request_id(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        headers = {"X-Request-ID": "trace-123"}
        text = ""

        @staticmethod
        def json() -> dict[str, object]:
            return {"reply": "回复", "draftRequirement": None}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, object]) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(trace_requirement_chat.httpx, "AsyncClient", FakeAsyncClient)
    result = trace_requirement_chat.asyncio.run(
        trace_requirement_chat._run(
            "project-1", "/app/discussions", "test message",
            base_url="http://localhost:8000/api/v1/",
            timeout=45,
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert captured["timeout"] == 45
    assert captured["url"] == (
        "http://localhost:8000/api/v1/projects/project-1/requirement-chat"
    )
    assert captured["json"] == {
        "messages": [{"role": "user", "content": "test message"}],
        "routePath": "/app/discussions",
    }
    assert "trace-123" in output
    assert '"reply": "回复"' in output


def test_trace_returns_nonzero_for_http_error(monkeypatch, capsys) -> None:
    class FakeResponse:
        status_code = 502
        headers = {}
        text = "upstream error"

        @staticmethod
        def json() -> dict[str, str]:
            return {"detail": "failed"}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(trace_requirement_chat.httpx, "AsyncClient", FakeAsyncClient)
    assert trace_requirement_chat.asyncio.run(
        trace_requirement_chat._run("p", "/", "hi")
    ) == 1
    assert "HTTP 502" in capsys.readouterr().out


def test_trace_reports_connection_error(monkeypatch, capsys) -> None:
    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> None:
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(trace_requirement_chat.httpx, "AsyncClient", FakeAsyncClient)
    assert trace_requirement_chat.asyncio.run(
        trace_requirement_chat._run("p", "/", "hi")
    ) == 2
    assert "ConnectError" in capsys.readouterr().err
