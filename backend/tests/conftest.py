"""测试夹具。"""

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_analysis_repository,
    get_chat_session_repository,
    get_explore_repository,
    get_explore_worker_runner,
    get_git_client,
    get_llm_client,
    get_project_repository,
    get_requirement_repository,
    get_runtime_repository,
    get_runtime_worker_runner,
    get_specdoc_llm_client,
    get_worker_runner,
)
from app.core.config import Settings, get_settings
from app.main import create_app
from app.storage.memory import (
    InMemoryAnalysisRepository,
    InMemoryChatSessionRepository,
    InMemoryExploreRepository,
    InMemoryProjectRepository,
    InMemoryRequirementRepository,
    InMemoryRuntimeRepository,
)
from tests.fakes import (
    FakeExploreWorkerRunner,
    FakeGitClient,
    FakeRuntimeWorkerRunner,
    FakeWorkerRunner,
)


@pytest.fixture
def analysis_repository() -> InMemoryAnalysisRepository:
    return InMemoryAnalysisRepository()


@pytest.fixture
def requirement_repository() -> InMemoryRequirementRepository:
    return InMemoryRequirementRepository()


@pytest.fixture
def runtime_repository() -> InMemoryRuntimeRepository:
    return InMemoryRuntimeRepository()


@pytest.fixture
def explore_repository() -> InMemoryExploreRepository:
    return InMemoryExploreRepository()


@pytest.fixture
def chat_session_repository() -> InMemoryChatSessionRepository:
    return InMemoryChatSessionRepository()


@pytest.fixture
def fake_runner() -> FakeWorkerRunner:
    return FakeWorkerRunner()


@pytest.fixture
def fake_runtime_runner() -> FakeRuntimeWorkerRunner:
    return FakeRuntimeWorkerRunner()


@pytest.fixture
def fake_explore_runner() -> FakeExploreWorkerRunner:
    return FakeExploreWorkerRunner()


@pytest.fixture
def fake_git() -> FakeGitClient:
    return FakeGitClient()


@pytest.fixture
def client(
    tmp_path,
    analysis_repository: InMemoryAnalysisRepository,
    requirement_repository: InMemoryRequirementRepository,
    runtime_repository: InMemoryRuntimeRepository,
    explore_repository: InMemoryExploreRepository,
    chat_session_repository: InMemoryChatSessionRepository,
    fake_runner: FakeWorkerRunner,
    fake_runtime_runner: FakeRuntimeWorkerRunner,
    fake_explore_runner: FakeExploreWorkerRunner,
    fake_git: FakeGitClient,
) -> Iterator[TestClient]:
    # 测试必须与开发者本地 .env 隔离：显式传不读 .env 的配置，
    # 并覆盖 get_settings，防止 DI 路径读到本地 APP_DB_URL（如 MySQL）
    settings = settings_with(
        repos_dir=tmp_path / "repos",
        explore_artifacts_dir=tmp_path / "explore",
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    repository = InMemoryProjectRepository()
    app.dependency_overrides[get_project_repository] = lambda: repository
    app.dependency_overrides[get_analysis_repository] = lambda: analysis_repository
    app.dependency_overrides[get_requirement_repository] = lambda: requirement_repository
    app.dependency_overrides[get_runtime_repository] = lambda: runtime_repository
    app.dependency_overrides[get_explore_repository] = lambda: explore_repository
    app.dependency_overrides[get_chat_session_repository] = lambda: chat_session_repository
    app.dependency_overrides[get_worker_runner] = lambda: fake_runner
    app.dependency_overrides[get_runtime_worker_runner] = lambda: fake_runtime_runner
    app.dependency_overrides[get_explore_worker_runner] = lambda: fake_explore_runner
    app.dependency_overrides[get_git_client] = lambda: fake_git
    # 默认禁用 LLM（get_llm_client 直接读本地 .env，会打到真实服务）；
    # 需要 LLM 的用例在测试内自行覆盖为 FakeLLMClient
    app.dependency_overrides[get_llm_client] = lambda: None
    app.dependency_overrides[get_specdoc_llm_client] = lambda: None
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
