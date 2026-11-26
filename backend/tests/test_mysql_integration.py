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
    "chat_sessions",
    "langgraph_checkpoint_writes",
    "langgraph_checkpoints",
    "runtime_traces",
    "runtime_tasks",
    "requirement_maps",
    "requirement_tasks",
    "graphs",
    "graph_edges",
    "graph_nodes",
    "graph_revision_chunks",
    "graph_revisions",
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


# --- LangGraph checkpointer（P3：需求定位中间状态审计落盘） ------------------


async def test_checkpoint_saver_roundtrip(engine) -> None:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import CheckpointMetadata

    from app.storage.checkpoint import MySqlCheckpointSaver

    saver = MySqlCheckpointSaver(engine)
    thread: RunnableConfig = {"configurable": {"thread_id": "rtask_test000001"}}

    metadata: CheckpointMetadata = {"source": "loop", "step": 1, "writes": {}, "parents": {}}
    first_checkpoint = {
        "v": 1,
        "id": "cp-0001",
        "ts": "2026-11-17T00:00:00+00:00",
        "channel_values": {"requirement": "用户列表增加状态筛选"},
        "channel_versions": {},
        "versions_seen": {},
    }
    saved_config = await saver.aput(
        thread, first_checkpoint, metadata, {}  # type: ignore[arg-type]
    )
    assert saved_config["configurable"]["checkpoint_id"] == "cp-0001"

    second_checkpoint = {
        "v": 1,
        "id": "cp-0002",
        "ts": "2026-11-17T00:00:01+00:00",
        "channel_values": {"features": ["f1"]},
        "channel_versions": {},
        "versions_seen": {},
    }
    await saver.aput(
        _child_config(thread, "cp-0001"),
        second_checkpoint,  # type: ignore[arg-type]
        metadata,
        {},
    )
    await saver.aput_writes(
        _child_config(thread, "cp-0002"),
        [("judge", {"calls_used": 2})],
        task_id="node-judge",
    )

    latest = await saver.aget_tuple(thread)
    assert latest is not None
    assert latest.config["configurable"]["checkpoint_id"] == "cp-0002"
    checkpoint = latest.checkpoint
    assert checkpoint["channel_values"]["features"] == ["f1"]
    assert latest.parent_config is not None
    assert latest.parent_config["configurable"]["checkpoint_id"] == "cp-0001"
    assert latest.pending_writes == [("node-judge", "judge", {"calls_used": 2})]

    listed = [item.config["configurable"]["checkpoint_id"] async for item in saver.alist(thread)]
    assert listed == ["cp-0002", "cp-0001"]

    await saver.adelete_thread("rtask_test000001")
    assert await saver.aget_tuple(thread) is None


def _child_config(thread, checkpoint_id: str):
    from copy import deepcopy

    config = deepcopy(thread)
    config["configurable"]["checkpoint_id"] = checkpoint_id
    return config


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


async def test_graph_nodes_legacy_schema_is_migrated(engine) -> None:
    # 模拟历史结构：node_id 缺失，且旧实现遗留三列和相关索引。
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE graph_nodes DROP INDEX uq_graph_node_biz"))
        await conn.execute(text("ALTER TABLE graph_nodes DROP COLUMN node_id"))
        await conn.execute(
            text("ALTER TABLE graph_nodes ADD COLUMN node_key CHAR(12) NOT NULL")
        )
        await conn.execute(
            text("ALTER TABLE graph_nodes ADD COLUMN stable_id TEXT NOT NULL")
        )
        await conn.execute(
            text("ALTER TABLE graph_nodes ADD COLUMN stable_hash CHAR(40) NOT NULL")
        )
        await conn.execute(
            text("ALTER TABLE graph_nodes ADD UNIQUE KEY uq_graph_node_key "
                 "(project_id, analysis_id, graph_kind, node_key)")
        )
        await conn.execute(
            text("ALTER TABLE graph_nodes ADD KEY idx_graph_node_stable "
                 "(project_id, analysis_id, graph_kind, stable_hash)")
        )
        await conn.execute(
            text(
                """
                INSERT INTO graph_nodes
                  (project_id, analysis_id, graph_kind, node_key, stable_id,
                   stable_hash, kind, name, attrs_json, created_at)
                VALUES
                  ('prj_legacy', 'an_legacy', 'static', 'legacy-key',
                   'legacy-stable-id', '0123456789012345678901234567890123456789',
                   'component', 'Legacy', JSON_OBJECT(), NOW(6))
                """
            )
        )

    # 启动迁移应补齐当前 schema，移除遗留字段，且可重复执行。
    await init_schema(engine)
    await init_schema(engine)

    async with engine.begin() as conn:
        columns = (
            await conn.execute(
                text(
                    """
                    SELECT COLUMN_NAME FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graph_nodes'
                    """
                )
            )
        ).all()
        indexes = (
            await conn.execute(
                text(
                    """
                    SELECT INDEX_NAME FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graph_nodes'
                    """
                )
            )
        ).all()
    column_names = {str(row[0]) for row in columns}
    index_names = {str(row[0]) for row in indexes}
    assert "node_id" in column_names
    assert not {"node_key", "stable_id", "stable_hash"} & column_names
    assert not {"uq_graph_node_key", "idx_graph_node_stable"} & index_names

    async with engine.begin() as conn:
        legacy_count = (
            await conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM graph_nodes
                    WHERE project_id = 'prj_legacy' AND analysis_id = 'an_legacy'
                      AND name = 'Legacy'
                    """
                )
            )
        ).scalar_one()
    assert legacy_count == 1

    projects = MySqlProjectRepository(engine)
    analyses = MySqlAnalysisRepository(engine)
    await projects.add(_project())
    await analyses.add_task(_task())
    graph = _mini_graph("an_test000001")
    await analyses.save_graph("prj_test000001", graph)

    async with engine.begin() as conn:
        saved_ids = (
            await conn.execute(
                text(
                    """
                    SELECT node_id FROM graph_nodes
                    WHERE project_id = :project_id AND analysis_id = :analysis_id
                    """
                ),
                {"project_id": "prj_test000001", "analysis_id": "an_test000001"},
            )
        ).scalars().all()
    assert {str(node_id) for node_id in saved_ids} == {node.id for node in graph.nodes}


async def test_graph_edges_legacy_schema_is_migrated(engine) -> None:
    # 模拟历史结构及数据，迁移需保留行但清理旧字段和索引。
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE graph_edges DROP INDEX uq_graph_edge_biz"))
        await conn.execute(text("ALTER TABLE graph_edges DROP INDEX idx_graph_edge_from"))
        await conn.execute(text("ALTER TABLE graph_edges DROP INDEX idx_graph_edge_to"))
        await conn.execute(text("ALTER TABLE graph_edges DROP INDEX idx_graph_edge_kind"))
        await conn.execute(text("ALTER TABLE graph_edges DROP COLUMN edge_id"))
        await conn.execute(text("ALTER TABLE graph_edges DROP COLUMN from_node"))
        await conn.execute(text("ALTER TABLE graph_edges DROP COLUMN to_node"))
        await conn.execute(
            text("ALTER TABLE graph_edges ADD COLUMN edge_key CHAR(12) NOT NULL")
        )
        await conn.execute(
            text("ALTER TABLE graph_edges ADD COLUMN from_key CHAR(12) NOT NULL")
        )
        await conn.execute(
            text("ALTER TABLE graph_edges ADD COLUMN to_key CHAR(12) NOT NULL")
        )
        await conn.execute(
            text("ALTER TABLE graph_edges ADD COLUMN stable_id TEXT NOT NULL")
        )
        await conn.execute(
            text("ALTER TABLE graph_edges ADD UNIQUE KEY uq_graph_edge_key "
                 "(project_id, analysis_id, graph_kind, edge_key)")
        )
        await conn.execute(
            text("ALTER TABLE graph_edges ADD KEY idx_graph_edge_from "
                 "(project_id, analysis_id, graph_kind, from_key)")
        )
        await conn.execute(
            text("ALTER TABLE graph_edges ADD KEY idx_graph_edge_to "
                 "(project_id, analysis_id, graph_kind, to_key)")
        )
        await conn.execute(
            text(
                """
                INSERT INTO graph_edges
                  (project_id, analysis_id, graph_kind, edge_key, from_key,
                   to_key, stable_id, kind, created_at)
                VALUES
                  ('prj_legacy', 'an_legacy', 'static', 'legacy-edge',
                   'legacy-from', 'legacy-to', 'legacy-stable-edge',
                   'navigate', NOW(6))
                """
            )
        )

    await init_schema(engine)
    await init_schema(engine)

    async with engine.begin() as conn:
        columns = (
            await conn.execute(
                text(
                    """
                    SELECT COLUMN_NAME FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graph_edges'
                    """
                )
            )
        ).all()
        indexes = (
            await conn.execute(
                text(
                    """
                    SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graph_edges'
                    ORDER BY INDEX_NAME, SEQ_IN_INDEX
                    """
                )
            )
        ).all()
        legacy_count = (
            await conn.execute(
                text("SELECT COUNT(*) FROM graph_edges WHERE project_id = 'prj_legacy'")
            )
        ).scalar_one()
    column_names = {str(row[0]) for row in columns}
    index_columns = {}
    for index_name, _, column_name in indexes:
        index_columns.setdefault(str(index_name), []).append(str(column_name))
    assert {"edge_id", "from_node", "to_node"} <= column_names
    assert not {"edge_key", "from_key", "to_key", "stable_id"} & column_names
    assert "uq_graph_edge_key" not in index_columns
    assert index_columns["uq_graph_edge_biz"] == [
        "project_id", "analysis_id", "graph_kind", "edge_id"
    ]
    assert index_columns["idx_graph_edge_from"] == [
        "project_id", "analysis_id", "graph_kind", "from_node"
    ]
    assert index_columns["idx_graph_edge_to"] == [
        "project_id", "analysis_id", "graph_kind", "to_node"
    ]
    assert legacy_count == 1

    projects = MySqlProjectRepository(engine)
    analyses = MySqlAnalysisRepository(engine)
    await projects.add(_project())
    await analyses.add_task(_task())
    graph = _mini_graph("an_test000001")
    await analyses.save_graph("prj_test000001", graph)
    async with engine.begin() as conn:
        saved_edge_ids = (
            await conn.execute(
                text(
                    """
                    SELECT edge_id FROM graph_edges
                    WHERE project_id = :project_id AND analysis_id = :analysis_id
                    """
                ),
                {"project_id": "prj_test000001", "analysis_id": "an_test000001"},
            )
        ).scalars().all()
    assert {str(edge_id) for edge_id in saved_edge_ids} == {edge.id for edge in graph.edges}


def _mini_graph(analysis_id: str):
    from tests.fakes import _mini_graph as make_graph

    return make_graph(analysis_id)


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


async def test_chat_session_upsert_and_eviction(engine) -> None:
    """会话整包落库 + 每项目 10 条上限淘汰（upsert 事务内完成）。"""
    from app.storage.mysql import MySqlChatSessionRepository

    await MySqlProjectRepository(engine).add(_project())
    repo = MySqlChatSessionRepository(engine)

    def _session(index: int) -> object:
        from app.schemas import ChatMessage, ChatSession

        return ChatSession(
            id=f"chat_test{index:06d}",
            project_id="prj_test000001",
            title=f"需求 {index}",
            messages=[
                ChatMessage(role="user", content=f"需求 {index}"),
                ChatMessage(role="assistant", content="收到", doc_page="转账"),
            ],
            draft=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    for index in range(1, 12):
        await repo.upsert(_session(index))

    listed = await repo.list_by_project("prj_test000001")
    assert len(listed) == 10
    # 最旧的被淘汰，最新的排最前
    assert listed[0].id == "chat_test000011"
    assert await repo.get("chat_test000001") is None
    assert listed[0].messages[1].doc_page == "转账"
    assert await repo.delete("chat_test000011") is True
    assert await repo.delete("chat_test000011") is False
