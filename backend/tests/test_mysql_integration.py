"""MySQL 持久化集成测试：需要真实实例，env 门控默认跳过。

运行方式（指向专用测试库，会清空其中数据）：
  APP_MYSQL_INTEGRATION=1
  APP_TEST_DB_URL=mysql+aiomysql://user:pass@127.0.0.1:3307/uicodeanalysis_test?charset=utf8mb4
  uv run pytest tests/test_mysql_integration.py
"""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.storage.mysql import (
    MySqlAnalysisRepository,
    MySqlProjectRepository,
    MySqlRequirementRepository,
    MySqlRuntimeRepository,
    init_schema,
    run_boot_recovery,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("APP_MYSQL_INTEGRATION"),
    reason="MySQL 集成测试需设置 APP_MYSQL_INTEGRATION=1 与 APP_TEST_DB_URL",
)

TABLES = (
    "runtime_traces",
    "runtime_tasks",
    "requirement_maps",
    "requirement_tasks",
    "graphs",
    "analysis_tasks",
    "projects",
)


@pytest.fixture
def db_url() -> str:
    url = os.environ.get("APP_TEST_DB_URL")
    assert url, "APP_MYSQL_INTEGRATION=1 时必须提供 APP_TEST_DB_URL"
    return url


@pytest.fixture
async def engine(db_url: str):
    eng = create_async_engine(db_url, pool_pre_ping=True)
    await init_schema(eng)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def clean_tables(engine):
    # DELETE 而非 TRUNCATE：被外键引用的父表不允许 TRUNCATE；数据量极小无性能顾虑
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))


def _project() -> object:
    from app.schemas import Project

    return Project(
        id="prj_test000001",
        name="测试项目",
        root_path="D:/nonexistent/path",
        framework="react",
        created_at=datetime.now(UTC),
    )


def _task(project_id: str = "prj_test000001", task_id: str = "task_test000001") -> object:
    from app.schemas import AnalysisTask

    return AnalysisTask(
        id=task_id,
        project_id=project_id,
        analysis_id="an_test000001",
        status="queued",
        created_at=datetime.now(UTC),
    )


async def test_project_roundtrip(engine) -> None:
    repo = MySqlProjectRepository(engine)
    project = _project()
    added = await repo.add(project)
    assert added.storage == "mysql"

    fetched = await repo.get(project.id)
    assert fetched is not None
    assert fetched.name == project.name
    assert fetched.created_at == project.created_at
    assert fetched.storage == "mysql"

    assert await repo.get("prj_missing") is None
    listed = await repo.list()
    assert [item.id for item in listed] == [project.id]


async def test_add_task_conflict_returns_none(engine) -> None:
    await MySqlProjectRepository(engine).add(_project())
    repo = MySqlAnalysisRepository(engine)
    first = await repo.add_task(_task())
    assert first is not None
    # queued 任务仍在：同项目第二个任务必须被唯一索引拒绝
    conflict = await repo.add_task(
        _task(task_id="task_test000002")
    )
    assert conflict is None


async def test_task_lifecycle_update_and_list(engine) -> None:
    await MySqlProjectRepository(engine).add(_project())
    repo = MySqlAnalysisRepository(engine)
    task = await repo.add_task(_task())
    assert task is not None

    running = task.model_copy(update={"status": "running", "started_at": datetime.now(UTC)})
    await repo.update_task(running)
    fetched = await repo.get_task(task.id)
    assert fetched is not None
    assert fetched.status == "running"

    done = running.model_copy(
        update={"status": "succeeded", "finished_at": datetime.now(UTC), "node_count": 7}
    )
    await repo.update_task(done)
    final = await repo.get_task(task.id)
    assert final is not None
    assert final.status == "succeeded"
    assert final.node_count == 7

    # 终态任务不再占用活动唯一索引：可以登记新任务
    assert await repo.add_task(_task(task_id="task_test000003")) is not None


async def test_graph_roundtrip_and_latest(engine) -> None:
    from tests.fakes import _mini_graph

    projects = MySqlProjectRepository(engine)
    analyses = MySqlAnalysisRepository(engine)
    await projects.add(_project())
    await analyses.add_task(_task())

    graph = _mini_graph("an_test000001")
    await analyses.save_graph("prj_test000001", graph)

    fetched = await analyses.get_graph("prj_test000001", "an_test000001")
    assert fetched is not None
    assert [node.id for node in fetched.nodes] == [node.id for node in graph.nodes]
    assert [edge.id for edge in fetched.edges] == [edge.id for edge in graph.edges]

    # 同 analysisId 重复写入是幂等的
    await analyses.save_graph("prj_test000001", graph)
    latest = await analyses.get_latest_graph("prj_test000001")
    assert latest is not None
    assert latest.analysisId == "an_test000001"
    assert await analyses.get_latest_graph("prj_missing") is None


async def test_requirement_task_and_map(engine) -> None:
    from app.contracts import Feature, RequirementMap
    from app.schemas import RequirementTask

    projects = MySqlProjectRepository(engine)
    requirements = MySqlRequirementRepository(engine)
    await projects.add(_project())

    task = RequirementTask(
        id="rtask_test00001",
        project_id="prj_test000001",
        # 契约要求非空 str：登记时先给占位 id，定位产物落库后由 link_task 覆盖
        requirement_id="req_pending00001",
        status="queued",
        dry_run=True,
        requirement="登录页面在哪",
        created_at=datetime.now(UTC),
    )
    await requirements.add_task(task)

    feature = Feature(id="f1", text="登录页面", keywords=())
    req_map = RequirementMap(
        schemaVersion="1.0",
        requirementId="req_test000001",
        projectId="prj_test000001",
        analysisId="an_test000001",
        requirement="登录页面在哪",
        createdAt=datetime.now(UTC),
        dryRun=True,
        features=[feature],
        matches=[],
        unmatched=[],
        candidates=[],
        dropped=[],
        warnings=[],
    )
    await requirements.save_map(req_map)
    await requirements.link_task(task.id, req_map.requirementId)

    by_id = await requirements.get_map("req_test000001")
    assert by_id is not None
    assert by_id.requirementId == "req_test000001"

    by_task = await requirements.get_map_by_task("rtask_test00001")
    assert by_task is not None
    assert by_task.requirementId == "req_test000001"
    assert await requirements.get_map_by_task("rtask_missing") is None


async def test_runtime_task_and_trace(engine) -> None:
    from app.schemas import RuntimeTask
    from tests.fakes import mini_trace

    projects = MySqlProjectRepository(engine)
    runtimes = MySqlRuntimeRepository(engine)
    await projects.add(_project())

    task = RuntimeTask(
        id="collect_test0001",
        project_id="prj_test000001",
        trace_id="trace_test0001",
        status="queued",
        command="npm run dev",
        analysis_id="an_test000001",
        settle_ms=1500,
        created_at=datetime.now(UTC),
    )
    await runtimes.add_task(task)

    running = task.model_copy(update={"status": "running", "started_at": datetime.now(UTC)})
    await runtimes.update_task(running)
    trace = mini_trace("trace_test0001", "prj_test000001", "an_test000001")
    await runtimes.save_trace(trace)
    done = running.model_copy(
        update={"status": "succeeded", "finished_at": datetime.now(UTC), "component_count": 1}
    )
    await runtimes.update_task(done)

    fetched = await runtimes.get_task("collect_test0001")
    assert fetched is not None
    assert fetched.status == "succeeded"
    assert fetched.settle_ms == 1500
    assert fetched.component_count == 1

    by_id = await runtimes.get_trace("trace_test0001")
    assert by_id is not None
    assert by_id.traceId == "trace_test0001"

    by_task = await runtimes.get_trace_by_task("collect_test0001")
    assert by_task is not None
    assert by_task.traceId == "trace_test0001"

    latest = await runtimes.get_latest_trace("prj_test000001")
    assert latest is not None
    assert latest.traceId == "trace_test0001"
    assert await runtimes.get_latest_trace("prj_missing") is None


async def test_boot_recovery_marks_running_tasks_failed(engine) -> None:
    await MySqlProjectRepository(engine).add(_project())
    repo = MySqlAnalysisRepository(engine)
    await repo.add_task(_task())
    running = _task().model_copy(
        update={"status": "running", "started_at": datetime.now(UTC)}
    )
    await repo.update_task(running)

    recovered = await run_boot_recovery(engine)
    assert recovered == 1

    task = await repo.get_task("task_test000001")
    assert task is not None
    assert task.status == "failed"
    assert task.error_code == "server_restarted"

    # 再跑一次是幂等的
    assert await run_boot_recovery(engine) == 0


async def test_data_survives_reconnect(engine, db_url: str) -> None:
    """持久化的本体验证：写入 → 换一个全新引擎（模拟重启）→ 数据仍在。"""
    projects = MySqlProjectRepository(engine)
    await projects.add(_project())

    fresh = create_async_engine(db_url, pool_pre_ping=True)
    try:
        fetched = await MySqlProjectRepository(fresh).get("prj_test000001")
        assert fetched is not None
        assert fetched.name == "测试项目"
    finally:
        await fresh.dispose()
