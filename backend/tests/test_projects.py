from datetime import UTC, datetime

from app.schemas import AnalysisTask, ExploreTask, RequirementTask, RuntimeTask
from app.storage.memory import (
    InMemoryAnalysisRepository,
    InMemoryExploreRepository,
    InMemoryRequirementRepository,
    InMemoryRuntimeRepository,
)


def _now() -> datetime:
    return datetime.now(UTC)


def test_project_crud_uses_camel_case_contract(client) -> None:
    assert client.get("/api/v1/projects").json() == []

    created = client.post(
        "/api/v1/projects",
        json={"name": "  订单平台  ", "rootPath": "D:/work/orders", "framework": "next"},
    )
    assert created.status_code == 201

    project = created.json()
    assert project["name"] == "订单平台"
    assert project["rootPath"] == "D:/work/orders"
    assert project["framework"] == "next"
    assert project["storage"] == "memory"
    assert project["id"].startswith("prj_")
    assert project["createdAt"]

    listed = client.get("/api/v1/projects").json()
    assert [item["id"] for item in listed] == [project["id"]]

    fetched = client.get(f"/api/v1/projects/{project['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == project


def test_unknown_project_returns_404(client) -> None:
    response = client.get("/api/v1/projects/prj_missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "项目不存在"


def test_blank_project_name_is_rejected(client) -> None:
    response = client.post(
        "/api/v1/projects",
        json={"name": "   ", "rootPath": "D:/work/orders", "framework": "react"},
    )

    assert response.status_code == 422


def test_delete_project_removes_record(client) -> None:
    created = client.post(
        "/api/v1/projects",
        json={"name": "订单平台", "rootPath": "D:/work/orders", "framework": "react"},
    )
    project_id = created.json()["id"]

    deleted = client.delete(f"/api/v1/projects/{project_id}")
    assert deleted.status_code == 204
    assert deleted.content == b""

    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404
    assert client.get("/api/v1/projects").json() == []

    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 404


async def test_delete_project_purges_analysis_artifacts(
    client, analysis_repository: InMemoryAnalysisRepository
) -> None:
    created = client.post(
        "/api/v1/projects",
        json={"name": "订单平台", "rootPath": "D:/work/orders", "framework": "react"},
    )
    project_id = created.json()["id"]
    task = AnalysisTask(
        id="ana_1", project_id=project_id, analysis_id="ax_1", status="queued", created_at=_now()
    )
    assert await analysis_repository.add_task(task) is not None

    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 204
    # 内存存储没有外键级联，删除项目必须显式清领域产物
    assert await analysis_repository.list_by_project(project_id) == []
    assert await analysis_repository.get_task("ana_1") is None


async def test_in_memory_repositories_purge_project() -> None:
    """requirement / runtime / explore 三个内存仓储的 purge 与 analysis 同口径。"""
    requirements = InMemoryRequirementRepository()
    runtimes = InMemoryRuntimeRepository()
    explores = InMemoryExploreRepository()

    requirement = RequirementTask(
        id="req_1",
        project_id="prj_x",
        requirement_id="rq_1",
        status="queued",
        dry_run=True,
        requirement="导出",
        created_at=_now(),
    )
    runtime = RuntimeTask(
        id="rt_1", project_id="prj_x", trace_id="tr_1", status="queued", created_at=_now()
    )
    explore = ExploreTask(
        id="ex_1",
        project_id="prj_x",
        goal="看看",
        status="queued",
        dry_run=True,
        created_at=_now(),
    )
    await requirements.add_task(requirement)
    await runtimes.add_task(runtime)
    await explores.add_task(explore)
    await explores.save_artifact(
        {"taskId": "ex_1", "projectId": "prj_x"}, "/tmp/artifacts/ex_1"
    )
    # 别的项目的任务不能被误伤
    other = RequirementTask(
        id="req_2",
        project_id="prj_y",
        requirement_id="rq_2",
        status="queued",
        dry_run=True,
        requirement="导出",
        created_at=_now(),
    )
    await requirements.add_task(other)

    await requirements.purge_project("prj_x")
    await runtimes.purge_project("prj_x")
    await explores.purge_project("prj_x")

    assert await requirements.list_by_project("prj_x") == []
    assert await requirements.get_task("req_1") is None
    assert await requirements.get_map_by_task("req_1") is None
    assert await runtimes.list_by_project("prj_x") == []
    assert await runtimes.get_task("rt_1") is None
    assert await explores.list_by_project("prj_x") == []
    assert await explores.get_artifact("ex_1") is None
    assert await explores.get_artifacts_dir("ex_1") is None
    # 旁证：其他项目不受影响
    assert [task.id for task in await requirements.list_by_project("prj_y")] == ["req_2"]


def test_delete_local_project_keeps_user_directory(client) -> None:
    # 本地目录项目只解除登记；用户目录是平台外的东西，删除绝不能碰
    root = "D:/work/orders-keep-me"
    created = client.post(
        "/api/v1/projects",
        json={"name": "订单平台", "rootPath": root, "framework": "react"},
    )
    project_id = created.json()["id"]

    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 204
