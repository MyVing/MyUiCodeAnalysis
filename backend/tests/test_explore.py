"""AI 自动探索：任务流转、计划校验、dry-run、执行与产物下载。

模型一律用 FakeLLMClient——计划生成的正确性不能依赖真实模型。
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_llm_client
from app.requirement.llm import LLMCallError
from tests.fakes import FakeLLMClient


@pytest.fixture
def explore_project(client: TestClient, tmp_path) -> dict:
    response = client.post(
        "/api/v1/projects",
        json={"name": "探索项目", "rootPath": str(tmp_path), "framework": "react"},
    )
    assert response.status_code == 201
    return response.json()


def wait_for_explore_task(client: TestClient, task_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/explore-tasks/{task_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"探索任务 {task_id} 未在 {timeout}s 内完成")


def _llm_plan_json() -> str:
    """合规的模型计划：fill #0 → click #1 → assert。"""
    return (
        '{"summary": "在搜索框输入关键词后点击查询按钮，并断言结果出现。",'
        '"steps": ['
        '{"action": "fill", "targetIndex": 0, "value": "订单",'
        ' "reasoning": "先填搜索框"},'
        '{"action": "click", "targetIndex": 1, "reasoning": "触发查询"},'
        '{"action": "assert", "assertion": "查询结果"}'
        "],"
        '"scenario": {"title": "搜索订单", "description": "关键词搜索订单列表",'
        ' "preconditions": ["已登录"], "expectedOutcome": "出现匹配的订单列表"},'
        '"testCases": [{"title": "关键词搜索订单", "priority": "p1",'
        ' "steps": ["在搜索框输入订单", "点击查询"],'
        ' "expected": ["页面出现查询结果"], "relatedSteps": [0, 1, 2]}]}'
    )


def test_start_requires_launch_source(client: TestClient, project: dict) -> None:
    response = client.post(f"/api/v1/projects/{project['id']}/explores", json={"goal": "x"})
    assert response.status_code == 409
    assert "url" in response.json()["detail"]


def test_start_with_missing_root_path_rejected(client: TestClient, project: dict) -> None:
    response = client.post(
        f"/api/v1/projects/{project['id']}/explores",
        json={"goal": "验证登录", "command": "npm run dev"},
    )
    assert response.status_code == 409
    assert response.json()["detail"].startswith("项目根路径不存在")


def test_dry_run_generates_plan_and_script(
    client: TestClient, explore_project: dict, fake_explore_runner: object
) -> None:
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/explores",
        json={"goal": "验证登录流程", "command": "npm run dev", "dryRun": True},
    )
    assert response.status_code == 202
    task = response.json()
    assert task["status"] == "queued"
    assert task["dryRun"] is True

    final = wait_for_explore_task(client, task["id"])
    assert final["status"] == "succeeded"
    assert final["elementCount"] == 3

    artifact = client.get(f"/api/v1/explore-tasks/{task['id']}/artifact").json()
    assert len(artifact["discovery"]) == 3
    assert artifact["results"] == []
    # 兜底计划（未配模型）：有步骤与场景，dry-run 告警存在
    assert len(artifact["plan"]["steps"]) > 0
    warning_codes = {w["code"] for w in artifact["warnings"]}
    assert "dry_run" in warning_codes
    assert "llm_not_configured" in warning_codes

    script = artifact["script"]
    assert script["fileName"].endswith(".spec.ts")
    assert "import { test, expect }" in script["content"]


def test_llm_plan_is_validated_and_used(
    client: TestClient, explore_project: dict, fake_explore_runner: object
) -> None:
    llm = FakeLLMClient([_llm_plan_json()])
    client.app.dependency_overrides[get_llm_client] = lambda: llm
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/explores",
        json={"goal": "搜索订单", "url": "http://localhost:5199/", "dryRun": True},
    )
    final = wait_for_explore_task(client, response.json()["id"])
    assert final["status"] == "succeeded"

    artifact = client.get(f"/api/v1/explore-tasks/{final['id']}/artifact").json()
    plan = artifact["plan"]
    assert plan["summary"].startswith("在搜索框输入关键词")
    assert [s["action"] for s in plan["steps"]] == ["fill", "click", "assert"]
    assert plan["steps"][0]["targetIndex"] == 0
    assert artifact["scenario"]["title"] == "搜索订单"
    assert artifact["testCases"][0]["priority"] == "p1"
    # 模型计划未被丢弃
    warning_codes = {w["code"] for w in artifact["warnings"]}
    assert "plan_step_dropped" not in warning_codes


def test_llm_invalid_targets_are_dropped(
    client: TestClient, explore_project: dict, fake_explore_runner: object
) -> None:
    """模型幻觉出不存在的元素 #9：该步骤被丢弃，其余步骤重排后保留。"""
    bad_plan = (
        '{"summary": "x",'
        '"steps": ['
        '{"action": "click", "targetIndex": 9},'
        '{"action": "click", "targetIndex": 1}'
        "],"
        '"scenario": {"title": "t", "description": "d"},'
        '"testCases": []}'
    )
    client.app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([bad_plan])
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/explores",
        json={"goal": "g", "url": "http://localhost:5199/", "dryRun": True},
    )
    final = wait_for_explore_task(client, response.json()["id"])
    assert final["status"] == "succeeded"

    artifact = client.get(f"/api/v1/explore-tasks/{final['id']}/artifact").json()
    steps = artifact["plan"]["steps"]
    assert len(steps) == 1
    assert steps[0]["index"] == 0
    assert steps[0]["targetIndex"] == 1
    assert any(w["code"] == "plan_step_dropped" for w in artifact["warnings"])


def test_llm_failure_falls_back_to_deterministic_plan(
    client: TestClient, explore_project: dict, fake_explore_runner: object
) -> None:
    client.app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient(
        error=LLMCallError("HTTP 500")
    )
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/explores",
        json={"goal": "g", "url": "http://localhost:5199/", "dryRun": True},
    )
    final = wait_for_explore_task(client, response.json()["id"])
    assert final["status"] == "succeeded"

    artifact = client.get(f"/api/v1/explore-tasks/{final['id']}/artifact").json()
    assert any(w["code"] == "llm_unavailable_fallback" for w in artifact["warnings"])
    # 兜底计划：填文本框 → 点按钮 → 等待
    assert [s["action"] for s in artifact["plan"]["steps"]][:1] == ["fill"]


def test_execute_produces_results_and_counts(
    client: TestClient, explore_project: dict, fake_explore_runner: object
) -> None:
    client.app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([_llm_plan_json()])
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/explores",
        json={"goal": "搜索订单", "url": "http://localhost:5199/", "dryRun": False},
    )
    final = wait_for_explore_task(client, response.json()["id"])
    assert final["status"] == "succeeded"
    assert final["stepCount"] == 3
    assert final["successCount"] == 3
    assert final["failedCount"] == 0
    assert final["screenshotCount"] == 3

    artifact = client.get(f"/api/v1/explore-tasks/{final['id']}/artifact").json()
    assert len(artifact["results"]) == 3
    assert all(r["result"] == "success" for r in artifact["results"])

    # 执行请求按协议下发：selector 已解析、artifactsDir 已给出
    request = fake_explore_runner.explore_requests[0]  # type: ignore[attr-defined]
    assert request.artifacts_dir
    assert request.steps[0].selector == {
        "strategy": "role",
        "value": "textbox",
        "name": "搜索关键词",
    }


def test_discover_failure_is_visible(
    client: TestClient, explore_project: dict, fake_explore_runner: object
) -> None:
    fake_explore_runner.mode = "failure"  # type: ignore[attr-defined]
    fake_explore_runner.error_code = "browser_unavailable"  # type: ignore[attr-defined]
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/explores",
        json={"goal": "g", "command": "npm run dev"},
    )
    final = wait_for_explore_task(client, response.json()["id"])
    assert final["status"] == "failed"
    assert final["errorCode"] == "browser_unavailable"


def test_execute_failure_is_visible(
    client: TestClient, explore_project: dict, fake_explore_runner: object
) -> None:
    client.app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([_llm_plan_json()])
    fake_explore_runner.mode = "failure"  # type: ignore[attr-defined]
    fake_explore_runner.error_code = "page_load_failed"  # type: ignore[attr-defined]
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/explores",
        json={"goal": "g", "url": "http://localhost:5199/", "dryRun": False},
    )
    final = wait_for_explore_task(client, response.json()["id"])
    assert final["status"] == "failed"
    assert final["errorCode"] == "page_load_failed"


def test_script_download_and_screenshot_guard(
    client: TestClient, explore_project: dict, fake_explore_runner: object
) -> None:
    client.app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([_llm_plan_json()])
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/explores",
        json={"goal": "g", "url": "http://localhost:5199/", "dryRun": False},
    )
    final = wait_for_explore_task(client, response.json()["id"])

    script_response = client.get(f"/api/v1/explore-tasks/{final['id']}/script")
    assert script_response.status_code == 200
    assert script_response.headers["content-type"].startswith("text/x-typescript")
    assert "test(" in script_response.text

    # 截图文件（fake 不会真的写盘）：合法名字 404，非法名字同样 404
    assert (
        client.get(f"/api/v1/explore-tasks/{final['id']}/screenshots/step-000.png").status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/explore-tasks/{final['id']}/screenshots/..%2Fsecret.png").status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/explore-tasks/{final['id']}/screenshots/not-a-shot.png").status_code
        == 404
    )


def test_list_explore_tasks_and_unknown_task_404(client: TestClient, project: dict) -> None:
    response = client.get(f"/api/v1/projects/{project['id']}/explores")
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert client.get("/api/v1/explore-tasks/exp_missing").status_code == 404
    assert client.get("/api/v1/explore-tasks/exp_missing/artifact").status_code == 404


def test_artifact_requires_succeeded_task(
    client: TestClient, explore_project: dict, fake_explore_runner: object
) -> None:
    fake_explore_runner.mode = "failure"  # type: ignore[attr-defined]
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/explores",
        json={"goal": "g", "url": "http://localhost:5199/"},
    )
    final = wait_for_explore_task(client, response.json()["id"])
    assert final["status"] == "failed"
    artifact_response = client.get(f"/api/v1/explore-tasks/{final['id']}/artifact")
    assert artifact_response.status_code == 409


def test_settings_defaults_for_explore_worker() -> None:
    from app.core.config import resolve_explore_worker_entry
    from tests.conftest import settings_with

    settings = settings_with()
    assert settings.explore_worker_timeout_seconds == 600.0
    assert settings.explore_max_elements == 200
    assert str(resolve_explore_worker_entry(settings)).replace("\\", "/").endswith(
        "workers/runtime-worker/dist/index.js"
    )
