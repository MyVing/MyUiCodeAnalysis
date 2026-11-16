"""测试夹具。"""

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_analysis_repository,
    get_project_repository,
    get_requirement_repository,
    get_worker_runner,
)
from app.core.config import Settings
from app.main import create_app
from app.storage.memory import (
    InMemoryAnalysisRepository,
    InMemoryProjectRepository,
    InMemoryRequirementRepository,
)
from tests.fakes import FakeWorkerRunner


@pytest.fixture
def analysis_repository() -> InMemoryAnalysisRepository:
    return InMemoryAnalysisRepository()


@pytest.fixture
def requirement_repository() -> InMemoryRequirementRepository:
    return InMemoryRequirementRepository()


@pytest.fixture
def fake_runner() -> FakeWorkerRunner:
    return FakeWorkerRunner()


@pytest.fixture
def client(
    analysis_repository: InMemoryAnalysisRepository,
    requirement_repository: InMemoryRequirementRepository,
    fake_runner: FakeWorkerRunner,
) -> Iterator[TestClient]:
    app = create_app()
    repository = InMemoryProjectRepository()
    app.dependency_overrides[get_project_repository] = lambda: repository
    app.dependency_overrides[get_analysis_repository] = lambda: analysis_repository
    app.dependency_overrides[get_requirement_repository] = lambda: requirement_repository
    app.dependency_overrides[get_worker_runner] = lambda: fake_runner
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def project(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/v1/projects",
        json={"name": "订单平台", "rootPath": "D:/work/orders", "framework": "next"},
    )
    assert response.status_code == 201
    return response.json()


def settings_with(**overrides: object) -> Settings:
    """构造一份不读 .env 的配置：测试结果不能被开发者本地 .env 影响。"""
    base = Settings(_env_file=None, **overrides)
    return base


@pytest.fixture
def llm_ready_settings() -> Settings:
    return settings_with(
        llm_enabled=True,
        llm_api_key="test-key",
        llm_base_url="http://localhost:9/v1",
        llm_model="test-model",
    )


def wait_for_final_task(
    client: TestClient, task_id: str, timeout: float = 10.0
) -> dict[str, object]:
    """轮询直到任务进入终态；TestClient 的 portal 循环会推进后台 asyncio 任务。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, dict)
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内完成")
