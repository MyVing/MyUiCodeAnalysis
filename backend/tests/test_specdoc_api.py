"""规格文档 API 测试：路由与前置校验（map 不存在 / 图已不可用 / 未完成）。

种子数据必须写在应用自己的事件循环上（内存仓储的 asyncio.Lock 绑定循环），
所以用 client.portal.call 调度，而不是在测试线程里 asyncio.run。
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from app.api.deps import (
    get_analysis_repository,
    get_requirement_repository,
    get_specdoc_llm_client,
)
from tests.fakes import FakeLLMClient
from tests.specdoc_fixture import spec_graph, spec_requirement_map

VALID_RULES = {
    "rules": [
        {
            "featureId": "f1",
            "category": "display",
            "target": "付款账户",
            "condition": "未选择付款账户",
            "result": "转账按钮置灰",
            "quote": "请选择付款账户",
        }
    ]
}


def _remap(requirement_map, project_id: str):
    """夹具里的 projectId 是占位值，重映射到测试项目（服务会校验归属）。"""
    return requirement_map.model_copy(update={"projectId": project_id})


def _seed(client: TestClient, project_id: str, graph, requirement_map) -> None:
    requirement_map = _remap(requirement_map, project_id)

    async def _run() -> None:
        analyses = client.app.dependency_overrides[get_analysis_repository]()
        requirements = client.app.dependency_overrides[get_requirement_repository]()
        await analyses.save_graph(project_id, graph)
        await requirements.save_map(requirement_map)

    client.portal.call(_run)


def _wait_for_final(client: TestClient, task_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/specdocs/{task_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内完成")


def test_start_returns_task_and_full_flow_succeeds(client: TestClient, project: dict) -> None:
    graph_ = spec_graph(analysis_id="an_api")
    requirement_map = spec_requirement_map(graph_, requirement_id="req_api")
    _seed(client, project["id"], graph_, requirement_map)
    client.app.dependency_overrides[get_specdoc_llm_client] = lambda: FakeLLMClient(
        [
            json.dumps({"steps": []}),
            json.dumps(VALID_RULES),
            json.dumps({"fieldMappings": []}),
        ]
    )

    response = client.post(
        f"/api/v1/projects/{project['id']}/specdocs",
        json={"requirementId": "req_api", "dryRun": False},
    )
    assert response.status_code == 202
    task = response.json()
    assert task["status"] == "queued"
    assert task["id"].startswith("stask_")

    final = _wait_for_final(client, task["id"])
    assert final["status"] == "succeeded"
    assert final["elementCount"] >= 3
    assert final["ruleCount"] == 1

    doc_response = client.get(f"/api/v1/specdocs/{task['id']}/doc")
    assert doc_response.status_code == 200
    doc = doc_response.json()
    assert doc["schemaVersion"] == "1.0"
    assert doc["requirementId"] == "req_api"
    assert doc["pages"][0]["pageNo"] == 1

    markdown_response = client.get(f"/api/v1/specdocs/{task['id']}/doc.md")
    assert markdown_response.status_code == 200
    assert markdown_response.headers["content-type"].startswith("text/markdown")
    body = markdown_response.text
    assert "### 2.1.3 业务操作步骤" in body
    assert "#### 2.1.4.1 页面要素清单" in body
    assert "#### 2.1.4.5 前后端接口映射" in body


def test_dry_run_flag_is_honored(client: TestClient, project: dict) -> None:
    graph_ = spec_graph(analysis_id="an_api2")
    requirement_map = spec_requirement_map(graph_, requirement_id="req_api2")
    _seed(client, project["id"], graph_, requirement_map)

    response = client.post(
        f"/api/v1/projects/{project['id']}/specdocs",
        json={"requirementId": "req_api2", "dryRun": True},
    )
    assert response.status_code == 202
    task = response.json()
    assert task["dryRun"] is True

    final = _wait_for_final(client, task["id"])
    assert final["status"] == "succeeded"

    markdown = client.get(f"/api/v1/specdocs/{task['id']}/doc.md")
    assert "dry-run" in markdown.text
    doc = client.get(f"/api/v1/specdocs/{task['id']}/doc").json()
    assert doc["steps"] == [] and doc["rules"] == []


def test_map_not_found_returns_404(client: TestClient, project: dict) -> None:
    response = client.post(
        f"/api/v1/projects/{project['id']}/specdocs",
        json={"requirementId": "req_missing"},
    )
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


def test_stale_map_returns_409(client: TestClient, project: dict) -> None:
    """定位产物引用的图不在仓储里（图已更新/清理）→ 409。"""
    requirement_map = spec_requirement_map(spec_graph(analysis_id="an_gone"))
    requirement_map = _remap(requirement_map, project["id"])

    async def _run() -> None:
        requirements = client.app.dependency_overrides[get_requirement_repository]()
        await requirements.save_map(requirement_map)

    client.portal.call(_run)

    response = client.post(
        f"/api/v1/projects/{project['id']}/specdocs",
        json={"requirementId": requirement_map.requirementId},
    )
    assert response.status_code == 409


def test_doc_requires_succeeded_task(client: TestClient) -> None:
    response = client.get("/api/v1/specdocs/stask_nonexistent/doc")
    assert response.status_code == 404


def test_specdoc_task_list(client: TestClient, project: dict) -> None:
    graph_ = spec_graph(analysis_id="an_api3")
    requirement_map = spec_requirement_map(graph_, requirement_id="req_api3")
    _seed(client, project["id"], graph_, requirement_map)
    client.post(
        f"/api/v1/projects/{project['id']}/specdocs",
        json={"requirementId": "req_api3", "dryRun": True},
    )
    response = client.get(f"/api/v1/projects/{project['id']}/specdocs")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["requirementId"] == "req_api3"
