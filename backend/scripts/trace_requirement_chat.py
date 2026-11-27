"""Send a real HTTP request to the running requirement-chat API.

The backend owns the model configuration and emits internal routing/evidence/LLM
traces according to APP_LOG_LEVEL. This script only sends a normal API request
and prints its response and X-Request-ID.

Run from the backend directory:
    python scripts/trace_requirement_chat.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

DEFAULT_PROJECT_ID = "prj_678282afec22"
DEFAULT_ROUTE = "/app/discussions"
DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


async def _run(
    project_id: str,
    route_path: str,
    message: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 180.0,
) -> int:
    url = f"{base_url.rstrip('/')}/projects/{project_id}/requirement-chat"
    payload = {
        "messages": [{"role": "user", "content": message}],
        "routePath": route_path,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.TimeoutException:
        print(f"请求超时：{url}（timeout={timeout}s）", file=sys.stderr)
        return 2
    except httpx.HTTPError as error:
        print(f"请求后端失败：{type(error).__name__}: {error}", file=sys.stderr)
        print("请确认后端已启动，并检查 --base-url。", file=sys.stderr)
        return 2

    print(f"HTTP {response.status_code}")
    request_id = response.headers.get("X-Request-ID")
    print(f"X-Request-ID: {request_id or '(响应中未提供)'}")
    try:
        print("响应：\n" + _json(response.json()))
    except ValueError:
        print(response.text)
    if response.status_code >= 400:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--route", default=DEFAULT_ROUTE)
    parser.add_argument("--message", default="前端交互视图")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"后端 API 前缀（默认：{DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="HTTP 请求超时秒数（默认：180）",
    )
    args = parser.parse_args()
    print(
        f"请求配置：base_url={args.base_url} project={args.project_id} "
        f"route={args.route} message_characters={len(args.message)}"
    )
    return asyncio.run(
        _run(
            args.project_id,
            args.route,
            args.message,
            base_url=args.base_url,
            timeout=args.timeout,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
