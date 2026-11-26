"""Trace one requirement-chat request through routing, evidence, and LLM calls.

This starts an in-process app; it does not attach to an already-running server.
It uses the database and model settings loaded from the project's `.env` file.
Run from the backend directory:
    python scripts/trace_requirement_chat.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Allow direct execution from scripts/ while importing the backend application.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from fastapi.testclient import TestClient

from app.api.deps import get_llm_client, get_specdoc_llm_client
from app.core.config import Settings
from app.main import create_app
from app.requirement import chat_doc
from app.requirement.chat_doc import locate_route_feature as _locate_route_feature
from app.requirement.llm import OpenAICompatibleLLM, build_llm_client
from app.specdoc import evidence as evidence_module
from app.specdoc import infer as infer_module
from app.specdoc.evidence_query import EvidenceQuery
from app.specdoc.llm import OpenAICompatibleToolLLM, build_specdoc_llm_client
from app.specdoc.retrieval import EvidenceSearch

DEFAULT_PROJECT_ID = "prj_678282afec22"
DEFAULT_ROUTE = "/app/discussions"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _page_counts(pages: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for page in pages:
        for key in ("elements", "events", "chains", "forms", "apiFacts", "uiText"):
            counts[key] += len(page.get(key, []))
    return {"pages": len(pages), **dict(counts)}


async def _run(project_id: str, route_path: str, message: str) -> int:
    # The IDE may launch this file with the repository-level venv, whose CWD is
    # not backend/. Load backend/.env explicitly rather than depending on CWD.
    settings = Settings(_env_file=Path(__file__).resolve().parents[1] / ".env")
    settings = settings.model_copy(update={"llm_enabled": True})
    llm_client = build_llm_client(settings)
    specdoc_llm_client = build_specdoc_llm_client(settings)
    if llm_client is None or specdoc_llm_client is None:
        print(
            "错误：后端 .env 中 APP_LLM_API_KEY 为空；"
            "请设置有效密钥后再运行。"
        )
        return 2

    traces: dict[str, Any] = {
        "route": None,
        "evidence_pack": [],
        "llm_requests": [],
        "tool_executions": [],
    }
    prompt_json_checks: list[str] = []

    def traced_locate_route_feature(graph, requested_route):
        result = _locate_route_feature(graph, requested_route)
        traces["route"] = {
            "requested": requested_route,
            "matched": result.candidate.model_dump(mode="json") if result else None,
            "quote_corpus": result.quote_corpus if result else [],
        }
        return result

    original_build_pack = evidence_module.build_evidence_pack

    def traced_build_pack(materials, events_by_page=None):
        pack = original_build_pack(materials, events_by_page)
        raw_pages = pack.pages
        raw_json = pack.to_json()
        prompt_json, _ = pack.prompt_view()
        prompt_json_checks.append(prompt_json)
        traces["evidence_pack"].append({
            "raw_counts": _page_counts(raw_pages),
            "raw_characters": len(raw_json),
            "prompt_counts": _page_counts(pack.prompt_pages),
            "prompt_characters": len(prompt_json),
            "warnings": list(pack.warnings),
            "prompt_warnings": list(pack.prompt_warnings),
        })
        return pack

    # Capture the model's actual outgoing payload and any returned tool calls.
    original_request = OpenAICompatibleToolLLM._request
    original_requirement_messages = OpenAICompatibleLLM.chat_messages

    async def traced_requirement_messages(self, system, messages, *, temperature=0):
        traces["llm_requests"].append({
            "kind": "requirement_chat",
            "system_characters": len(system),
            "message_characters": sum(
                len(item.get("content", ""))
                for item in messages if isinstance(item.get("content"), str)
            ),
            "temperature": temperature,
        })
        return await original_requirement_messages(
            self, system, messages, temperature=temperature
        )

    async def traced_request(self, payload):
        messages = payload.get("messages", [])
        message_contents = [
            item.get("content", "")
            for item in messages
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        ]
        prompt_json_in_request = any(
            prompt_json in content
            for content in message_contents
            for prompt_json in prompt_json_checks
        )
        record = {
            "kind": "specdoc",
            "model": payload.get("model"),
            "message_characters": sum(map(len, message_contents)),
            "message_count": len(messages),
            "tools": [
                tool.get("function", {}).get("name")
                for tool in payload.get("tools", [])
                if isinstance(tool, dict)
            ],
            "prompt_json_in_request": prompt_json_in_request,
        }
        traces["llm_requests"].append(record)
        response = await original_request(self, payload)
        record["response_has_tool_calls"] = bool(response.get("tool_calls"))
        record["response_characters"] = len(response.get("content") or "")
        return response

    def _tool_summary(backend: str, name: str, arguments: dict[str, Any], result: dict[str, Any]):
        facts = result.get("facts", [])
        return {
            "backend": backend,
            "name": name,
            "arguments": arguments,
            "status": result.get("status"),
            "fact_count": len(facts) if isinstance(facts, list) else 0,
            "text_characters": sum(
                len(fact.get("text", ""))
                for fact in facts
                if isinstance(fact, dict) and isinstance(fact.get("text"), str)
            ),
        }

    async def traced_evidence_query(self, name, arguments):
        result = await original_query_execute(self, name, arguments)
        traces["tool_executions"].append(
            _tool_summary("EvidenceQuery", name, arguments, result)
        )
        return result

    async def traced_evidence_search(self, name, arguments):
        result = await original_search_execute(self, name, arguments)
        traces["tool_executions"].append(
            _tool_summary("EvidenceSearch", name, arguments, result)
        )
        return result

    original_query_execute = EvidenceQuery.execute
    original_search_execute = EvidenceSearch.execute
    chat_doc.locate_route_feature = traced_locate_route_feature
    evidence_module.build_evidence_pack = traced_build_pack
    infer_module.build_evidence_pack = traced_build_pack
    OpenAICompatibleLLM.chat_messages = traced_requirement_messages
    OpenAICompatibleToolLLM._request = traced_request
    EvidenceQuery.execute = traced_evidence_query
    EvidenceSearch.execute = traced_evidence_search

    app = create_app(settings)
    app.dependency_overrides[get_llm_client] = lambda: llm_client
    app.dependency_overrides[get_specdoc_llm_client] = lambda: specdoc_llm_client
    try:
        with TestClient(app) as client:
            response = client.post(
                f"{settings.api_prefix}/projects/{project_id}/requirement-chat",
                json={"messages": [{"role": "user", "content": message}],
                      "routePath": route_path},
            )
            print(f"HTTP {response.status_code}")
            try:
                print("响应：\n" + _json(response.json()))
            except ValueError:
                print(response.text)
            print("\n路由定位：\n" + _json(traces["route"] or "未经过精确路由定位"))
            print(
                "\n证据包统计（原始投影与 prompt_view）：\n"
                + _json(traces["evidence_pack"])
            )
            print("\n模型请求统计：\n" + _json(traces["llm_requests"]))
            if traces["tool_executions"]:
                print("\n实际执行的证据工具调用：\n" + _json(traces["tool_executions"]))
            else:
                print("\n实际证据工具调用：无（本次未执行 search_evidence executor）。")
            if response.status_code >= 400:
                return 1
            return 0
    finally:
        chat_doc.locate_route_feature = _locate_route_feature
        evidence_module.build_evidence_pack = original_build_pack
        infer_module.build_evidence_pack = original_build_pack
        OpenAICompatibleLLM.chat_messages = original_requirement_messages
        OpenAICompatibleToolLLM._request = original_request
        EvidenceQuery.execute = original_query_execute
        EvidenceSearch.execute = original_search_execute


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--route", default=DEFAULT_ROUTE)
    parser.add_argument("--message", default="前端交互视图")
    args = parser.parse_args()
    return asyncio.run(_run(args.project_id, args.route, args.message))


if __name__ == "__main__":
    raise SystemExit(main())
