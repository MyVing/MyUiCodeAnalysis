"""分析任务端到端编排测试（使用 FakeWorkerRunner，不启动真实子进程）。"""

import os

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from tests.conftest import wait_for_final_task
from tests.fakes import FakeWorkerRunner

FIXTURE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "tests", "fixtures", "enterprise-react-app")
)


@pytest.fixture
def analysis_project(client: TestClient) -> dict[str, object]:
    """rootPath 指向真实存在的 fixture 目录，让分析能真正进入 Worker 调度。"""
    response = client.post(
        "/api/v1/projects",
        json={"name": "企业示例应用", "rootPath": FIXTURE_ROOT, "framework": "react"},
    )
    assert response.status_code == 201
    return response.json()


def test_start_analysis_returns_queued_task(
    client: TestClient, analysis_project: dict[str, object]
) -> None:
    response = client.post(f"/api/v1/projects/{analysis_project['id']}/analyses")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] in ("queued", "running")
    assert body["projectId"] == analysis_project["id"]
    assert body["analysisId"].startswith("an_")
    assert body["id"].startswith("task_")


def test_successful_analysis_stores_real_graph(
    client: TestClient, analysis_project: dict[str, object]
) -> None:
    start = client.post(f"/api/v1/projects/{analysis_project['id']}/analyses")
    assert start.status_code == 202
    task = start.json()

    final = wait_for_final_task(client, task["id"])
    assert final["status"] == "succeeded"
    assert final["nodeCount"] == 1
    assert final["edgeCount"] == 1
    assert final["warningCount"] == 0
    assert final["finishedAt"] is not None

    graph = client.get(f"/api/v1/projects/{analysis_project['id']}/graph")
    assert graph.status_code == 200
    body = graph.json()
    assert body.get("meta", {}).get("sample") is None  # 真实产物不带 sample 标记
    assert body["analysisId"] == task["analysisId"]
    assert len(body["nodes"]) == 1


def test_failed_analysis_falls_back_to_sample_graph(
    client: TestClient, fake_runner: FakeWorkerRunner, analysis_project: dict[str, object]
) -> None:
    fake_runner.mode = "failure"

    start = client.post(f"/api/v1/projects/{analysis_project['id']}/analyses")
    assert start.status_code == 202
    task = start.json()

    final = wait_for_final_task(client, task["id"])
    assert final["status"] == "failed"
    assert final["errorCode"] == "analysis_failed"
    assert final["errorMessage"] == "模拟的分析失败"

    graph = client.get(f"/api/v1/projects/{analysis_project['id']}/graph")
    assert graph.status_code == 200
    assert graph.json()["meta"]["sample"] is True  # 失败后回退示例图


def test_invalid_ir_marks_task_failed(
    client: TestClient, fake_runner: FakeWorkerRunner, analysis_project: dict[str, object]
) -> None:
    fake_runner.mode = "failure"
    fake_runner.error_code = "invalid_ir"
    fake_runner.error_message = "产物不符合 IR 契约"

    start = client.post(f"/api/v1/projects/{analysis_project['id']}/analyses")
    task = start.json()

    final = wait_for_final_task(client, task["id"])
    assert final["status"] == "failed"
    assert final["errorCode"] == "invalid_ir"


def test_worker_timeout_marks_task_failed(
    client: TestClient, fake_runner: FakeWorkerRunner, analysis_project: dict[str, object]
) -> None:
    fake_runner.mode = "hang"
    client.app.dependency_overrides[get_settings] = lambda: Settings(worker_timeout_seconds=0.05)

    start = client.post(f"/api/v1/projects/{analysis_project['id']}/analyses")
    task = start.json()

    final = wait_for_final_task(client, task["id"])
    assert final["status"] == "failed"
    assert final["errorCode"] == "worker_timeout"


def test_concurrent_analysis_conflicts_with_409(
    client: TestClient, fake_runner: FakeWorkerRunner, analysis_project: dict[str, object]
) -> None:
    fake_runner.mode = "hang"

    first = client.post(f"/api/v1/projects/{analysis_project['id']}/analyses")
    assert first.status_code == 202
    second = client.post(f"/api/v1/projects/{analysis_project['id']}/analyses")
    assert second.status_code == 409

    wait_for_final_task(client, first.json()["id"])


def test_missing_root_path_fails_fast_without_worker(
    client: TestClient, project: dict[str, object]
) -> None:
    # conftest 的 project fixture rootPath（D:/work/orders）在本机不存在
    response = client.post(f"/api/v1/projects/{project['id']}/analyses")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "failed"
    assert body["errorCode"] == "root_path_missing"


def test_reserved_fields_are_echoed(
    client: TestClient, fake_runner: FakeWorkerRunner, analysis_project: dict[str, object]
) -> None:
    fake_runner.mode = "success"

    start = client.post(
        f"/api/v1/projects/{analysis_project['id']}/analyses",
        json={
            "requirement": "用户列表增加状态筛选",
            "baseCommit": "abc123",
            "targetCommit": "def456",
        },
    )
    assert start.status_code == 202
    body = start.json()
    assert body["requirement"] == "用户列表增加状态筛选"
    assert body["baseCommit"] == "abc123"
    assert body["targetCommit"] == "def456"

    final = wait_for_final_task(client, body["id"])
    assert final["status"] == "succeeded"
    assert final["requirement"] == "用户列表增加状态筛选"


def test_task_not_found_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/tasks/task_missing")
    assert response.status_code == 404


def test_graph_with_unknown_analysis_id_returns_404(
    client: TestClient, analysis_project: dict[str, object]
) -> None:
    response = client.get(f"/api/v1/projects/{analysis_project['id']}/graph?analysisId=an_missing")
    assert response.status_code == 404


def test_graph_without_analysis_returns_sample(
    client: TestClient, analysis_project: dict[str, object]
) -> None:
    response = client.get(f"/api/v1/projects/{analysis_project['id']}/graph")
    assert response.status_code == 200
    assert response.json()["meta"]["sample"] is True


def test_analysis_requires_existing_project(client: TestClient) -> None:
    response = client.post("/api/v1/projects/prj_missing/analyses")
    assert response.status_code == 404


def test_list_analyses_returns_tasks_desc(
    client: TestClient, fake_runner: FakeWorkerRunner, analysis_project: dict[str, object]
) -> None:
    fake_runner.mode = "failure"  # 立即完成，便于连续创建

    first = client.post(f"/api/v1/projects/{analysis_project['id']}/analyses")
    wait_for_final_task(client, first.json()["id"])
    second = client.post(f"/api/v1/projects/{analysis_project['id']}/analyses")
    wait_for_final_task(client, second.json()["id"])

    listing = client.get(f"/api/v1/projects/{analysis_project['id']}/analyses")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 2
    assert items[0]["createdAt"] >= items[1]["createdAt"]


