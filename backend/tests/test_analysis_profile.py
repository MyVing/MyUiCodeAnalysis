"""分析成功后异步生成页面画像（阶段 C 的接线测试）。

三条边界：
1. 开关关闭 / 未配置模型 → 一次模型调用都不发（分析照常成功）；
2. 开关打开 → 画像落库，且按 analysisId 归属；
3. 画像生成失败 → 分析任务仍是 succeeded，只是没有画像。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_analysis_repository,
    get_llm_client,
    get_worker_runner,
)
from app.core.config import Settings, get_settings
from app.main import create_app
from tests.conftest import wait_for_final_task
from tests.fakes import FakeLLMClient, FakeWorkerRunner

FIXTURE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "tests", "fixtures", "enterprise-react-app")
)


def _make_client(settings: Settings, llm: FakeLLMClient | None = None) -> TestClient:
    from app.storage.memory import InMemoryAnalysisRepository

    app = create_app(settings)
    analyses = InMemoryAnalysisRepository()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_analysis_repository] = lambda: analyses
    if llm is not None:
        app.dependency_overrides[get_llm_client] = lambda: llm
    runner = FakeWorkerRunner()
    app.dependency_overrides[get_worker_runner] = lambda: runner
    client = TestClient(app)
    client._test_analyses = analyses  # type: ignore[attr-defined]
    client._test_runner = runner  # type: ignore[attr-defined]
    return client


@pytest.fixture
def profile_settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_enabled=True,
        llm_api_key="k",
        llm_profile_enabled=True,
    )


def _wait_stored_profiles(
    client: TestClient, project_id: str, analysis_id: str, timeout: float = 2.0
):
    """画像在分析任务的后台协程里生成：轮询仓储直到出现或超时。"""
    import time

    analyses = client._test_analyses  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stored = client.portal.call(analyses.get_page_profiles, project_id, analysis_id)
        if stored is not None:
            return stored
        time.sleep(0.01)
    return None


def test_profile_disabled_sends_no_model_call() -> None:
    """默认关：分析成功，但不会多花一次模型调用。"""
    llm = FakeLLMClient(["{}"])
    settings = Settings(_env_file=None, llm_enabled=True, llm_api_key="k")
    with _make_client(settings, llm) as client:
        project = client.post(
            "/api/v1/projects",
            json={"name": "p", "rootPath": FIXTURE_ROOT, "framework": "react"},
        ).json()
        start = client.post(f"/api/v1/projects/{project['id']}/analyses").json()
        assert wait_for_final_task(client, start["id"])["status"] == "succeeded"
        assert llm.calls == []


def test_profile_enabled_generates_and_stores(profile_settings: Settings) -> None:
    llm = FakeLLMClient(
        [
            '{"profiles": [{"nodeId": "src/App.tsx#App", '
            '"summary": "应用入口页面，挂载其余组件。"}]}'
        ]
    )
    with _make_client(profile_settings, llm) as client:
        project = client.post(
            "/api/v1/projects",
            json={"name": "p", "rootPath": FIXTURE_ROOT, "framework": "react"},
        ).json()
        start = client.post(f"/api/v1/projects/{project['id']}/analyses").json()
        final = wait_for_final_task(client, start["id"])
        assert final["status"] == "succeeded"
        assert llm.calls, "开关打开且模型可用时必须生成画像"

        stored = _wait_stored_profiles(client, project["id"], final["analysisId"])

        assert stored is not None, "画像应在分析成功后落库"
        assert stored.analysis_id == final["analysisId"]
        assert stored.profiles[0].page_id == "src/App.tsx#App"
        # 画像把 analysisId 写进自己的 payload：跨图版本取用时会拿不到（有意的）
        assert stored.summary_of("src/App.tsx#App")


def test_profile_failure_does_not_fail_analysis(profile_settings: Settings) -> None:
    from app.requirement.llm import LLMCallError

    llm = FakeLLMClient(error=LLMCallError("HTTP 500"))
    with _make_client(profile_settings, llm) as client:
        project = client.post(
            "/api/v1/projects",
            json={"name": "p", "rootPath": FIXTURE_ROOT, "framework": "react"},
        ).json()
        start = client.post(f"/api/v1/projects/{project['id']}/analyses").json()
        final = wait_for_final_task(client, start["id"])

    assert final["status"] == "succeeded"
    assert final["nodeCount"] == 1
