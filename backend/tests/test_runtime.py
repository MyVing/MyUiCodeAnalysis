"""运行时采集 API 与编排：任务流转、前置校验、合并图自动落库。"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import settings_with


@pytest.fixture
def runtime_project(client: TestClient, tmp_path: Path) -> dict:
    """带真实根目录的项目：托管启动（command）前置校验要求根路径存在。"""
    response = client.post(
        "/api/v1/projects",
        json={"name": "运行时项目", "rootPath": str(tmp_path), "framework": "react"},
    )
    assert response.status_code == 201
    return response.json()


def wait_for_runtime_task(client: TestClient, task_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runtime-tasks/{task_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"采集任务 {task_id} 未在 {timeout}s 内完成")


def _start_collect(client: TestClient, project_id: str, **payload: object) -> object:
    return client.post(f"/api/v1/projects/{project_id}/runtime-traces", json=payload)


def test_start_requires_launch_source(client: TestClient, project: dict) -> None:
    response = _start_collect(client, project["id"])  # type: ignore[arg-type]
    assert response.status_code == 409
    assert "url" in response.json()["detail"]


def test_start_with_missing_root_path_rejected(client: TestClient, project: dict) -> None:
    response = _start_collect(client, project["id"], command="npm run dev")
    assert response.status_code == 409
    assert response.json()["detail"].startswith("项目根路径不存在")


def test_start_with_unknown_analysis_rejected(client: TestClient, project: dict) -> None:
    response = _start_collect(
        client, project["id"], url="http://localhost:5199/", analysisId="an_missing"
    )
    assert response.status_code == 404


def test_collect_success_and_trace_roundtrip(
    client: TestClient, runtime_project: dict, fake_runtime_runner: object
) -> None:
    response = _start_collect(client, runtime_project["id"], command="npm run dev", settleMs=1500)
    assert response.status_code == 202
    task = response.json()
    assert task["status"] == "queued"
    assert task["traceId"].startswith("trace_")
    assert task["command"] == "npm run dev"

    final = wait_for_runtime_task(client, task["id"])
    assert final["status"] == "succeeded"
    # mini_trace 里 App 一个组件
    assert final["componentCount"] == 1

    trace_response = client.get(f"/api/v1/runtime-tasks/{task['id']}/trace")
    assert trace_response.status_code == 200
    trace = trace_response.json()
    assert trace["traceId"] == task["traceId"]
    assert trace["projectId"] == runtime_project["id"]
    assert trace["componentTree"][0]["name"] == "App"

    # 请求按协议下发：rootPath / settleMs 透传
    request = fake_runtime_runner.requests[0]  # type: ignore[attr-defined]
    assert request.command == "npm run dev"
    assert request.settle_ms == 1500
    assert request.trace_id == task["traceId"]


def test_collect_failure_is_visible(
    client: TestClient, runtime_project: dict, fake_runtime_runner: object
) -> None:
    fake_runtime_runner.mode = "failure"  # type: ignore[attr-defined]
    fake_runtime_runner.error_code = "browser_unavailable"  # type: ignore[attr-defined]

    response = _start_collect(client, runtime_project["id"], command="npm run dev")
    assert response.status_code == 202
    final = wait_for_runtime_task(client, response.json()["id"])
    assert final["status"] == "failed"
    assert final["errorCode"] == "browser_unavailable"
    assert final["errorMessage"] == "模拟的采集失败"


def test_list_runtime_tasks(client: TestClient, project: dict) -> None:
    response = client.get(f"/api/v1/projects/{project['id']}/runtime-traces")
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_unknown_runtime_task_404(client: TestClient) -> None:
    assert client.get("/api/v1/runtime-tasks/collect_missing").status_code == 404
    assert client.get("/api/v1/runtime-tasks/collect_missing/trace").status_code == 404


def test_trace_requires_succeeded_task(
    client: TestClient, runtime_project: dict, fake_runtime_runner: object
) -> None:
    # 失败的任务没有可读取的采集产物：409 而不是 404
    fake_runtime_runner.mode = "failure"  # type: ignore[attr-defined]
    response = _start_collect(client, runtime_project["id"], command="npm run dev")
    task_id = response.json()["id"]
    final = wait_for_runtime_task(client, task_id)
    assert final["status"] == "failed"

    trace_response = client.get(f"/api/v1/runtime-tasks/{task_id}/trace")
    assert trace_response.status_code == 409


def test_collect_merges_runtime_evidence_into_latest_graph(
    client: TestClient, runtime_project: dict, analysis_repository: object
) -> None:
    # 1. 先跑一次静态分析（FakeWorkerRunner 产出含 src/App.tsx#App 的迷你图）
    analysis = client.post(f"/api/v1/projects/{runtime_project['id']}/analyses", json={})
    assert analysis.status_code == 202
    analysis_id = analysis.json()["analysisId"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{analysis.json()['id']}").json()
        if task["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.01)
    assert task["status"] == "succeeded"

    # 2. 静态图阶段：graph 只有静态证据
    graph = client.get(f"/api/v1/projects/{runtime_project['id']}/graph").json()
    assert all(e["source"] == "static" for n in graph["nodes"] for e in n["evidence"])

    # 3. 采集 → 自动合并
    collect = _start_collect(client, runtime_project["id"], command="npm run dev")
    final = wait_for_runtime_task(client, collect.json()["id"])
    assert final["status"] == "succeeded"

    merged = client.get(f"/api/v1/projects/{runtime_project['id']}/graph").json()
    app_node = next(n for n in merged["nodes"] if n["id"] == "src/App.tsx#App")
    sources = {e["source"] for e in app_node["evidence"]}
    assert sources == {"static", "runtime"}
    runtime_evidence = next(e for e in app_node["evidence"] if e["source"] == "runtime")
    assert runtime_evidence["traceId"] == final["traceId"]
    assert runtime_evidence["file"] == "src/App.tsx"
    assert merged["analysisId"] == analysis_id


def test_collect_without_static_graph_still_succeeds(
    client: TestClient, runtime_project: dict
) -> None:
    # 无静态图：采集照常成功，仅跳过合并（graph 端点回退示例图）
    response = _start_collect(client, runtime_project["id"], command="npm run dev")
    final = wait_for_runtime_task(client, response.json()["id"])
    assert final["status"] == "succeeded"
    assert final["componentCount"] == 1


def test_settings_defaults_for_runtime_worker() -> None:
    from app.core.config import resolve_runtime_worker_entry

    settings = settings_with()
    assert settings.runtime_worker_timeout_seconds == 300.0
    assert settings.runtime_settle_ms == 3000
    assert str(resolve_runtime_worker_entry(settings)).replace("\\", "/").endswith(
        "workers/runtime-worker/dist/index.js"
    )
