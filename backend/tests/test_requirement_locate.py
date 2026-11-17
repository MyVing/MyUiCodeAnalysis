"""需求定位编排层测试：任务状态机 + dry-run 路径 + 模型判定路径。

判定逻辑本身的规则在 test_requirement_judge.py；这里验证的是服务装配、
API 契约与降级行为。模型一律用 Fake——编排层的正确性与真实模型无关。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_analysis_repository,
    get_llm_client,
    get_project_repository,
    get_requirement_repository,
    get_runtime_repository,
    get_runtime_worker_runner,
    get_settings,
    get_worker_runner,
)
from app.core.config import Settings
from app.main import create_app
from app.requirement.llm import LLMCallError
from app.requirement.service import RequirementLocateService
from app.schemas import Project, RequirementLocateStart
from app.storage.memory import (
    InMemoryAnalysisRepository,
    InMemoryProjectRepository,
    InMemoryRequirementRepository,
    InMemoryRuntimeRepository,
)
from tests.fakes import FakeLLMClient, FakeRuntimeWorkerRunner, FakeWorkerRunner
from tests.graph_fixture import user_graph

_LIST_PAGE_ID = "src/pages/UserList.tsx#UserList"
_QUOTE = "搜索用户"


def _llm_response(**overrides: object) -> str:
    payload: dict[str, object] = {
        "matches": [
            {
                "featureId": "f1",
                "pageNodeId": _LIST_PAGE_ID,
                "quote": _QUOTE,
                "confidence": 0.9,
                "reason": "页面文案与需求一致",
            }
        ],
        "unmatched": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# --- 服务层（直接构造，绕开 HTTP） -----------------------------------------


def _project() -> Project:
    return Project(
        id="prj_test000001",
        name="订单平台",
        root_path="D:/work/orders",
        framework="next",
        created_at=datetime.now(UTC),
    )


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


async def _wait_task(service: RequirementLocateService, task_id: str, timeout: float = 10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        task = await service.get_task(task_id)
        assert task is not None
        if task.status in ("succeeded", "failed"):
            return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内完成")


async def test_dry_run_service_flow(
    requirement_repository: InMemoryRequirementRepository,
    analysis_repository: InMemoryAnalysisRepository,
) -> None:
    project = _project()
    await analysis_repository.save_graph(project.id, user_graph())
    service = RequirementLocateService(requirement_repository, analysis_repository, _settings())

    task = await service.start(
        project,
        RequirementLocateStart(requirement="用户列表增加状态筛选", dry_run=True),
    )
    task = await _wait_task(service, task.id)

    assert task.status == "succeeded"
    assert task.candidate_count and task.candidate_count > 0
    assert task.match_count == 0

    requirement_map = await service.get_map_by_task(task.id)
    assert requirement_map is not None
    assert requirement_map.dryRun is True
    assert requirement_map.matches == []
    assert requirement_map.candidates
    assert requirement_map.features[0].id == "f1"


async def test_llm_path_succeeds_with_fake_judge(
    requirement_repository: InMemoryRequirementRepository,
    analysis_repository: InMemoryAnalysisRepository,
) -> None:
    project = _project()
    await analysis_repository.save_graph(project.id, user_graph())
    llm = FakeLLMClient([_llm_response()])
    service = RequirementLocateService(
        requirement_repository,
        analysis_repository,
        _settings(llm_enabled=True, llm_api_key="k"),
        llm=llm,
    )

    task = await service.start(
        project,
        RequirementLocateStart(requirement="用户列表增加状态筛选", dry_run=False),
    )
    task = await _wait_task(service, task.id)

    assert task.status == "succeeded"
    assert task.match_count == 1
    assert llm.calls, "模型应被调用"

    requirement_map = await service.get_map_by_task(task.id)
    assert requirement_map is not None
    assert requirement_map.dryRun is False
    match = requirement_map.matches[0]
    assert match.pageNodeId == _LIST_PAGE_ID
    assert match.confidence == 0.6  # 封顶
    assert match.modelConfidence == 0.9
    assert match.quote == _QUOTE


async def test_llm_failure_degrades_to_recall_with_warning(
    requirement_repository: InMemoryRequirementRepository,
    analysis_repository: InMemoryAnalysisRepository,
) -> None:
    project = _project()
    await analysis_repository.save_graph(project.id, user_graph())
    llm = FakeLLMClient(error=LLMCallError("HTTP 503"))
    service = RequirementLocateService(
        requirement_repository,
        analysis_repository,
        _settings(llm_enabled=True, llm_api_key="k"),
        llm=llm,
    )

    task = await service.start(
        project,
        RequirementLocateStart(requirement="用户列表增加状态筛选", dry_run=False),
    )
    task = await _wait_task(service, task.id)

    # 模型失败不炸任务：降级为确定性召回 + 显式 error 告警
    assert task.status == "succeeded"
    requirement_map = await service.get_map_by_task(task.id)
    assert requirement_map is not None
    assert requirement_map.dryRun is False
    assert requirement_map.matches == []
    assert requirement_map.unmatched is not None
    codes = [warning.code for warning in requirement_map.warnings or []]
    assert "llm_unavailable" in codes


async def test_invalid_model_output_lands_in_dropped(
    requirement_repository: InMemoryRequirementRepository,
    analysis_repository: InMemoryAnalysisRepository,
) -> None:
    project = _project()
    await analysis_repository.save_graph(project.id, user_graph())
    # 一条合法 + 一条发明节点：dropped 必须可见而不是静默丢弃
    response = json.dumps(
        {
            "matches": [
                {
                    "featureId": "f1",
                    "pageNodeId": _LIST_PAGE_ID,
                    "quote": _QUOTE,
                    "confidence": 0.5,
                },
                {
                    "featureId": "f1",
                    "pageNodeId": "src/pages/Fake.tsx#Fake",
                    "quote": _QUOTE,
                    "confidence": 0.9,
                },
            ],
            "unmatched": [],
        }
    )
    service = RequirementLocateService(
        requirement_repository,
        analysis_repository,
        _settings(llm_enabled=True, llm_api_key="k"),
        llm=FakeLLMClient([response]),
    )

    task = await service.start(
        project,
        RequirementLocateStart(requirement="用户列表增加状态筛选", dry_run=False),
    )
    await _wait_task(service, task.id)

    requirement_map = await service.get_map_by_task(task.id)
    assert requirement_map is not None
    assert len(requirement_map.matches) == 1
    assert requirement_map.dropped is not None
    assert [dropped.nodeId for dropped in requirement_map.dropped] == ["src/pages/Fake.tsx#Fake"]


# --- API 层 ----------------------------------------------------------------


@pytest.fixture
def llm_fake() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def llm_client(
    llm_fake: FakeLLMClient,
    analysis_repository: InMemoryAnalysisRepository,
    requirement_repository: InMemoryRequirementRepository,
    runtime_repository: InMemoryRuntimeRepository,
    fake_runner: FakeWorkerRunner,
    fake_runtime_runner: FakeRuntimeWorkerRunner,
    llm_ready_settings: Settings,
) -> Iterator[TestClient]:
    project_repository = InMemoryProjectRepository()
    app = create_app(llm_ready_settings)
    app.dependency_overrides[get_settings] = lambda: llm_ready_settings
    app.dependency_overrides[get_llm_client] = lambda: llm_fake
    app.dependency_overrides[get_project_repository] = lambda: project_repository
    app.dependency_overrides[get_analysis_repository] = lambda: analysis_repository
    app.dependency_overrides[get_requirement_repository] = lambda: requirement_repository
    app.dependency_overrides[get_runtime_repository] = lambda: runtime_repository
    app.dependency_overrides[get_worker_runner] = lambda: fake_runner
    app.dependency_overrides[get_runtime_worker_runner] = lambda: fake_runtime_runner
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def llm_project(llm_client: TestClient) -> dict[str, object]:
    response = llm_client.post(
        "/api/v1/projects",
        json={"name": "订单平台", "rootPath": "D:/work/orders", "framework": "next"},
    )
    assert response.status_code == 201
    return response.json()


def _seed_graph(
    client: TestClient,
    analysis_repository: InMemoryAnalysisRepository,
    project_id: str,
) -> None:
    """把真实图夹具塞进内存仓储；必须在 TestClient 的 portal 循环上调用，
    否则 asyncio.Lock 会跨事件循环绑定。"""
    client.portal.call(analysis_repository.save_graph, project_id, user_graph())


def _wait_requirement_task(
    client: TestClient, task_id: str, timeout: float = 10.0
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/requirements/{task_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内完成")


def test_requirement_dry_run_api_flow(
    client: TestClient, project: dict[str, object], analysis_repository: InMemoryAnalysisRepository
) -> None:
    _seed_graph(client, analysis_repository, str(project["id"]))
    response = client.post(
        f"/api/v1/projects/{project['id']}/requirements",
        json={"requirement": "用户列表增加状态筛选", "dryRun": True},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["dryRun"] is True

    final = _wait_requirement_task(client, str(body["id"]))
    assert final["status"] == "succeeded"

    map_response = client.get(f"/api/v1/requirements/{body['id']}/map")
    assert map_response.status_code == 200
    requirement_map = map_response.json()
    assert requirement_map["dryRun"] is True
    assert requirement_map["matches"] == []
    assert requirement_map["candidates"]
    assert requirement_map["projectId"] == project["id"]


def test_requirement_without_llm_rejects_non_dry_run(
    client: TestClient, project: dict[str, object], analysis_repository: InMemoryAnalysisRepository
) -> None:
    _seed_graph(client, analysis_repository, str(project["id"]))
    response = client.post(
        f"/api/v1/projects/{project['id']}/requirements",
        json={"requirement": "用户列表增加状态筛选", "dryRun": False},
    )
    assert response.status_code == 409
    assert "APP_LLM_ENABLED" in response.json()["detail"]


def test_requirement_llm_api_flow(
    llm_client: TestClient,
    llm_project: dict[str, object],
    analysis_repository: InMemoryAnalysisRepository,
    llm_fake: FakeLLMClient,
) -> None:
    _seed_graph(llm_client, analysis_repository, str(llm_project["id"]))
    llm_fake.responses = [_llm_response()]

    start = llm_client.post(
        f"/api/v1/projects/{llm_project['id']}/requirements",
        json={"requirement": "用户列表增加状态筛选", "dryRun": False},
    )
    assert start.status_code == 202
    final = _wait_requirement_task(llm_client, str(start.json()["id"]))
    assert final["status"] == "succeeded"
    assert final["matchCount"] == 1

    requirement_map = llm_client.get(f"/api/v1/requirements/{start.json()['id']}/map").json()
    assert requirement_map["dryRun"] is False
    assert requirement_map["matches"][0]["pageNodeId"] == _LIST_PAGE_ID
    assert requirement_map["matches"][0]["confidence"] <= 0.6


def test_requirement_unknown_task_404(client: TestClient) -> None:
    assert client.get("/api/v1/requirements/rtask_missing").status_code == 404
    assert client.get("/api/v1/requirements/rtask_missing/map").status_code == 404


def test_requirement_list_returns_tasks_desc(
    client: TestClient, project: dict[str, object], analysis_repository: InMemoryAnalysisRepository
) -> None:
    _seed_graph(client, analysis_repository, str(project["id"]))
    for _ in range(2):
        response = client.post(
            f"/api/v1/projects/{project['id']}/requirements",
            json={"requirement": "用户列表增加状态筛选", "dryRun": True},
        )
        assert response.status_code == 202
        _wait_requirement_task(client, str(response.json()["id"]))

    listing = client.get(f"/api/v1/projects/{project['id']}/requirements")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 2
    assert items[0]["createdAt"] >= items[1]["createdAt"]


def test_requirement_sample_graph_rejected(client: TestClient, project: dict[str, object]) -> None:
    # 不播种真实图：项目无图 → 409 graph_missing
    response = client.post(
        f"/api/v1/projects/{project['id']}/requirements",
        json={"requirement": "用户列表增加状态筛选", "dryRun": True},
    )
    assert response.status_code == 409
