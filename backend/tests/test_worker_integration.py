"""真实 Worker 集成测试：需要 node 与已构建的 dist，默认跳过。

触发方式：先 `npm run build -w @uicodeanalysis/ast-worker`，再
`APP_WORKER_INTEGRATION=1 uv run pytest tests/test_worker_integration.py`
"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.conftest import wait_for_final_task

pytestmark = pytest.mark.skipif(
    not os.environ.get("APP_WORKER_INTEGRATION"),
    reason="真实 worker 集成测试需设置 APP_WORKER_INTEGRATION=1",
)


@pytest.fixture
def real_client(analysis_repository) -> Iterator[TestClient]:
    """只覆盖仓储，Worker 走真实 SubprocessWorkerRunner。"""
    from app.api.deps import get_analysis_repository, get_project_repository
    from app.main import create_app
    from app.storage.memory import InMemoryProjectRepository

    app = create_app()
    project_repository = InMemoryProjectRepository()
    app.dependency_overrides[get_project_repository] = lambda: project_repository
    app.dependency_overrides[get_analysis_repository] = lambda: analysis_repository
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def fixture_project(real_client: TestClient) -> dict[str, object]:
    fixture_root = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "tests", "fixtures", "enterprise-react-app"
        )
    )
    response = real_client.post(
        "/api/v1/projects",
        json={"name": "集成测试项目", "rootPath": fixture_root, "framework": "react"},
    )
    assert response.status_code == 201
    return response.json()


def test_real_worker_analyzes_fixture(
    real_client: TestClient, fixture_project: dict[str, object]
) -> None:
    """不注入 FakeWorkerRunner，走真实 SubprocessWorkerRunner。"""
    start = real_client.post(f"/api/v1/projects/{fixture_project['id']}/analyses")
    assert start.status_code == 202
    task = start.json()

    final = wait_for_final_task(real_client, task["id"], timeout=60.0)
    assert final["status"] == "succeeded", (
        f"分析失败：{final.get('errorCode')} {final.get('errorMessage')}"
    )
    assert (final["nodeCount"] or 0) > 0

    graph = real_client.get(f"/api/v1/projects/{fixture_project['id']}/graph")
    assert graph.status_code == 200
    body = graph.json()
    assert body["analysisId"] == task["analysisId"]
    assert body["meta"]["sample"] is False  # 真实产物显式标记 sample=false
