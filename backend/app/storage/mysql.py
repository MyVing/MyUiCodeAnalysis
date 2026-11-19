"""MySQL 持久化实现：与内存实现共用同一套异步 Protocol。

设计要点：
- SQLAlchemy 仅用 Core 层（create_async_engine + text 查询），表少查询简单，
  不引入 ORM 模型的开销；
- 并发冲突用生成列 + 唯一索引表达：analysis_tasks.active_flag 在任务处于
  queued/running 时等于 project_id，进入终态后为 NULL。MySQL 唯一索引允许多个
  NULL，因此「每项目至多一个活动任务」变成单条 INSERT 撞唯一键 → 返回 None，
  与内存实现语义一致，且不存在先查后插的竞态窗口；
- 时间统一 DATETIME(6) 存 naive UTC：写入去 tzinfo，读出补 UTC；
- 图与需求映射整包存 JSON 列，不逐节点建表——分析产物的消费方永远是整图；
- 不做内存实现那种「每项目 5 张图」的有界淘汰：持久化的意义就是保留历史。
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.contracts import IRGraph, RequirementMap, RuntimeTrace, SpecDocMap
from app.core.config import Settings
from app.requirement.page_profile import ProfilePayload
from app.schemas import (
    AnalysisTask,
    ExploreTask,
    Project,
    RequirementTask,
    RuntimeTask,
    SpecDocTask,
)
from app.storage.checkpoint import CHECKPOINT_DDL_STATEMENTS
from app.storage.repositories import DuplicateRemoteUrlError

logger = logging.getLogger(__name__)

DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS projects (
      id VARCHAR(32) PRIMARY KEY,
      name VARCHAR(80) NOT NULL,
      root_path VARCHAR(512) NOT NULL,
      framework VARCHAR(8) NOT NULL,
      source VARCHAR(8) NOT NULL DEFAULT 'local',
      -- 归一化后的 Git 地址：同一仓库只能接入一次。MySQL 唯一索引允许多个 NULL，
      -- 因此本地目录项目（remote_url 为 NULL）不受影响。
      remote_url VARCHAR(512) NULL,
      git_branch VARCHAR(128) NULL,
      commit_sha VARCHAR(64) NULL,
      last_synced_at DATETIME(6) NULL,
      created_at DATETIME(6) NOT NULL,
      CONSTRAINT uq_projects_remote UNIQUE (remote_url),
      KEY idx_projects_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_tasks (
      id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      analysis_id VARCHAR(32) NOT NULL,
      status ENUM('queued','running','succeeded','failed') NOT NULL,
      -- 「每项目至多一个活动任务」的载体：应用层维护——登记任务时写 project_id，
      -- 任务进入终态时置 NULL（见 update_task 的 CASE 表达式）。
      -- 不用 STORED 生成列的原因：InnoDB 8.0 下生成列表达式引用 ENUM 时，
      -- 同表外键约束会以 1215 失败（实测 8.0.42），与唯一索引无关。
      active_flag VARCHAR(32) NULL,
      created_at DATETIME(6) NOT NULL,
      started_at DATETIME(6) NULL,
      finished_at DATETIME(6) NULL,
      error_code VARCHAR(64) NULL,
      error_message TEXT NULL,
      node_count INT NULL,
      edge_count INT NULL,
      warning_count INT NULL,
      requirement TEXT NULL,
      base_commit VARCHAR(128) NULL,
      target_commit VARCHAR(128) NULL,
      CONSTRAINT uq_active_task UNIQUE (active_flag),
      CONSTRAINT fk_tasks_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
      KEY idx_tasks_project (project_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS graphs (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      analysis_id VARCHAR(32) NOT NULL,
      kind VARCHAR(8) NOT NULL DEFAULT 'static',
      payload JSON NOT NULL,
      created_at DATETIME(6) NOT NULL,
      CONSTRAINT uq_graph_version UNIQUE (project_id, analysis_id, kind),
      KEY idx_graph_latest (project_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    -- 页面画像：分析后异步生成的页面业务描述，按图版本缓存。
    -- 与 graphs 分开存：画像是模型产物（可再来一次），图是事实（不可重算）。
    CREATE TABLE IF NOT EXISTS page_profiles (
      project_id VARCHAR(32) NOT NULL,
      analysis_id VARCHAR(32) NOT NULL,
      payload JSON NOT NULL,
      created_at DATETIME(6) NOT NULL,
      PRIMARY KEY (project_id, analysis_id),
      CONSTRAINT fk_profiles_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS requirement_tasks (
      id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      requirement_id VARCHAR(32) NULL,
      status ENUM('queued','running','succeeded','failed') NOT NULL,
      dry_run BOOLEAN NOT NULL,
      requirement TEXT NOT NULL,
      analysis_id VARCHAR(32) NULL,
      created_at DATETIME(6) NOT NULL,
      started_at DATETIME(6) NULL,
      finished_at DATETIME(6) NULL,
      error_code VARCHAR(64) NULL,
      error_message TEXT NULL,
      candidate_count INT NULL,
      match_count INT NULL,
      warning_count INT NULL,
      CONSTRAINT fk_rtasks_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
      KEY idx_rtasks_project (project_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS requirement_maps (
      requirement_id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      analysis_id VARCHAR(32) NULL,
      payload JSON NOT NULL,
      created_at DATETIME(6) NOT NULL,
      KEY idx_rmaps_project (project_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_tasks (
      id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      trace_id VARCHAR(32) NOT NULL,
      status ENUM('queued','running','succeeded','failed') NOT NULL,
      command VARCHAR(500) NULL,
      url VARCHAR(2048) NULL,
      analysis_id VARCHAR(32) NULL,
      settle_ms INT NULL,
      created_at DATETIME(6) NOT NULL,
      started_at DATETIME(6) NULL,
      finished_at DATETIME(6) NULL,
      error_code VARCHAR(64) NULL,
      error_message TEXT NULL,
      component_count INT NULL,
      network_count INT NULL,
      console_count INT NULL,
      interaction_count INT NULL,
      CONSTRAINT fk_runtimes_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
      KEY idx_runtime_tasks_project (project_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_traces (
      trace_id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      analysis_id VARCHAR(32) NULL,
      payload JSON NOT NULL,
      created_at DATETIME(6) NOT NULL,
      KEY idx_runtime_traces_project (project_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # LangGraph checkpointer 的两张表：需求定位中间状态的审计落盘（P3 起）
    *CHECKPOINT_DDL_STATEMENTS,
    # P5 AI 自动探索：任务状态 + 整包产物（JSON）+ 产物目录
    """
    CREATE TABLE IF NOT EXISTS explore_tasks (
      id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      goal TEXT NOT NULL,
      status ENUM('queued','running','succeeded','failed') NOT NULL,
      command VARCHAR(500) NULL,
      url VARCHAR(2048) NULL,
      analysis_id VARCHAR(32) NULL,
      dry_run BOOLEAN NOT NULL DEFAULT FALSE,
      max_steps INT NULL,
      created_at DATETIME(6) NOT NULL,
      started_at DATETIME(6) NULL,
      finished_at DATETIME(6) NULL,
      error_code VARCHAR(64) NULL,
      error_message TEXT NULL,
      element_count INT NULL,
      step_count INT NULL,
      success_count INT NULL,
      failed_count INT NULL,
      screenshot_count INT NULL,
      CONSTRAINT fk_explores_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
      KEY idx_explore_tasks_project (project_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS explore_artifacts (
      task_id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      payload JSON NOT NULL,
      artifacts_dir VARCHAR(512) NOT NULL,
      created_at DATETIME(6) NOT NULL,
      CONSTRAINT fk_explore_artifacts_task FOREIGN KEY (task_id)
        REFERENCES explore_tasks(id) ON DELETE CASCADE,
      KEY idx_explore_artifacts_project (project_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # 规格文档装配：任务状态 + 产物（JSON）。同一 RequirementMap 可多次生成
    # 规格，产物按 specDocId（= 任务 id）存取。
    """
    CREATE TABLE IF NOT EXISTS specdoc_tasks (
      id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      requirement_id VARCHAR(32) NOT NULL,
      status ENUM('queued','running','succeeded','failed') NOT NULL,
      dry_run BOOLEAN NOT NULL,
      created_at DATETIME(6) NOT NULL,
      started_at DATETIME(6) NULL,
      finished_at DATETIME(6) NULL,
      error_code VARCHAR(64) NULL,
      error_message TEXT NULL,
      element_count INT NULL,
      event_count INT NULL,
      rule_count INT NULL,
      warning_count INT NULL,
      CONSTRAINT fk_specdocs_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
      KEY idx_specdoc_tasks_project (project_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS spec_docs (
      spec_doc_id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      requirement_id VARCHAR(32) NOT NULL,
      analysis_id VARCHAR(32) NULL,
      payload JSON NOT NULL,
      created_at DATETIME(6) NOT NULL,
      CONSTRAINT fk_spec_docs_task FOREIGN KEY (spec_doc_id)
        REFERENCES specdoc_tasks(id) ON DELETE CASCADE,
      KEY idx_spec_docs_project (project_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)

_RESTARTED_MESSAGE = "服务重启时任务仍在进行中，无法恢复子进程状态，已标记为失败。"

# Git 接入引入的列。已有库的 projects 表由 CREATE TABLE IF NOT EXISTS 不会自动补列，
# 因此启动时按 information_schema 幂等补齐，不引入迁移工具。
_PROJECT_COLUMNS: dict[str, str] = {
    "source": "VARCHAR(8) NOT NULL DEFAULT 'local'",
    "remote_url": "VARCHAR(512) NULL",
    "git_branch": "VARCHAR(128) NULL",
    "commit_sha": "VARCHAR(64) NULL",
    "last_synced_at": "DATETIME(6) NULL",
}
_REMOTE_INDEX = "uq_projects_remote"


def _is_duplicate_key(error: IntegrityError) -> bool:
    # pymysql 的 ER_DUP_ENTRY = 1062；外键等其他完整性违规必须如实抛出，
    # 否则「项目不存在导致的 FK 失败」会被误判成并发冲突而静默丢任务。
    args = getattr(getattr(error, "orig", None), "args", None)
    if not args:
        return False
    return int(args[0]) == 1062


def _to_db(value: datetime) -> datetime:
    """Pydantic 的 UTC aware datetime → MySQL DATETIME 的 naive 值。"""
    return value.replace(tzinfo=None) if value.tzinfo else value


def _from_db(value: datetime | None) -> datetime | None:
    """MySQL DATETIME 的 naive 值 → 应用层的 UTC aware datetime。"""
    return value.replace(tzinfo=UTC) if value is not None else None


def create_engine_from_url(settings: Settings) -> AsyncEngine:
    """按 Settings 构建异步引擎；调用方保证 db_url 非空。"""
    assert settings.db_url is not None
    return create_async_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        echo=settings.db_echo,
    )


async def _ensure_project_columns(conn: Any) -> None:
    """幂等补齐 projects 的新列与远程地址唯一索引（旧库升级路径）。"""
    rows = (
        await conn.execute(
            text(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'projects'
                """
            )
        )
    ).all()
    existing = {str(row[0]) for row in rows}
    for name, definition in _PROJECT_COLUMNS.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE projects ADD COLUMN {name} {definition}"))
            logger.info("projects 表补充列 %s", name)

    index_rows = (
        await conn.execute(
            text(
                """
                SELECT INDEX_NAME FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'projects'
                  AND INDEX_NAME = :name
                """
            ),
            {"name": _REMOTE_INDEX},
        )
    ).all()
    if not index_rows:
        await conn.execute(
            text(f"ALTER TABLE projects ADD UNIQUE KEY {_REMOTE_INDEX} (remote_url)")
        )
        logger.info("projects 表补充唯一索引 %s", _REMOTE_INDEX)


async def init_schema(engine: AsyncEngine) -> None:
    """幂等建表；单写入方（单进程编排）下 CREATE TABLE IF NOT EXISTS 足够。"""
    async with engine.begin() as conn:
        for statement in DDL_STATEMENTS:
            await conn.execute(text(statement))
        await _ensure_project_columns(conn)


async def run_boot_recovery(engine: AsyncEngine) -> int:
    """重启恢复：queued/running 任务不可能还活着，落成可见的 failed。

    内存实现重启即清空，等价语义是「丢数据」；持久化后我们有责任把中断
    状态显式化，而不是留一个永远 running 的任务。
    """
    async with engine.begin() as conn:
        updated = 0
        for table in ("analysis_tasks", "requirement_tasks", "runtime_tasks"):
            result = await conn.execute(
                text(
                    f"""
                    UPDATE {table}
                    SET status = 'failed', error_code = 'server_restarted',
                        error_message = :message, finished_at = NOW(6)
                    WHERE status IN ('queued','running')
                    """
                ),
                {"message": _RESTARTED_MESSAGE},
            )
            updated += result.rowcount
    if updated:
        logger.warning("启动恢复：将 %d 个中断任务标记为 failed（server_restarted）", updated)
    return updated


class MySqlProjectRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _project(self, row: Row[Any]) -> Project:
        return Project(
            id=row.id,
            name=row.name,
            root_path=row.root_path,
            framework=row.framework,
            created_at=_from_db(row.created_at),
            storage="mysql",
            source=row.source or "local",
            remote_url=row.remote_url,
            git_branch=row.git_branch,
            commit_sha=row.commit_sha,
            last_synced_at=_from_db(row.last_synced_at),
        )

    async def list(self) -> list[Project]:
        query = text("SELECT * FROM projects ORDER BY created_at DESC")
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [self._project(row) for row in rows]

    async def get(self, project_id: str) -> Project | None:
        query = text("SELECT * FROM projects WHERE id = :id")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"id": project_id})).first()
        return self._project(row) if row else None

    async def get_by_remote(self, remote_url: str) -> Project | None:
        query = text("SELECT * FROM projects WHERE remote_url = :remote_url")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"remote_url": remote_url})).first()
        return self._project(row) if row else None

    async def add(self, project: Project) -> Project:
        query = text(
            """
            INSERT INTO projects (
              id, name, root_path, framework, source, remote_url,
              git_branch, commit_sha, last_synced_at, created_at
            )
            VALUES (
              :id, :name, :root_path, :framework, :source, :remote_url,
              :git_branch, :commit_sha, :last_synced_at, :created_at
            )
            """
        )
        params = {
            "id": project.id,
            "name": project.name,
            "root_path": project.root_path,
            "framework": project.framework,
            "source": project.source,
            "remote_url": project.remote_url,
            "git_branch": project.git_branch,
            "commit_sha": project.commit_sha,
            "last_synced_at": (
                _to_db(project.last_synced_at) if project.last_synced_at else None
            ),
            "created_at": _to_db(project.created_at),
        }
        try:
            async with self._engine.begin() as conn:
                await conn.execute(query, params)
        except IntegrityError as error:
            if _is_duplicate_key(error):
                raise DuplicateRemoteUrlError(project.remote_url or "") from error
            raise
        return project.model_copy(update={"storage": "mysql"})

    async def update(self, project: Project) -> Project:
        query = text(
            """
            UPDATE projects
            SET name = :name, root_path = :root_path, framework = :framework,
                git_branch = :git_branch, commit_sha = :commit_sha,
                last_synced_at = :last_synced_at
            WHERE id = :id
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": project.id,
                    "name": project.name,
                    "root_path": project.root_path,
                    "framework": project.framework,
                    "git_branch": project.git_branch,
                    "commit_sha": project.commit_sha,
                    "last_synced_at": (
                        _to_db(project.last_synced_at) if project.last_synced_at else None
                    ),
                },
            )
        return project.model_copy(update={"storage": "mysql"})

    async def delete(self, project_id: str) -> bool:
        query = text("DELETE FROM projects WHERE id = :id")
        async with self._engine.begin() as conn:
            result = await conn.execute(query, {"id": project_id})
        # 子表全部带 REFERENCES projects(id) ON DELETE CASCADE，
        # 分析 / 需求 / 运行时 / 探索产物随这一条语句一起消失
        return result.rowcount > 0


class MySqlAnalysisRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _task(self, row: Row[Any]) -> AnalysisTask:
        return AnalysisTask(
            id=row.id,
            project_id=row.project_id,
            analysis_id=row.analysis_id,
            status=row.status,
            created_at=_from_db(row.created_at),
            started_at=_from_db(row.started_at),
            finished_at=_from_db(row.finished_at),
            error_code=row.error_code,
            error_message=row.error_message,
            node_count=row.node_count,
            edge_count=row.edge_count,
            warning_count=row.warning_count,
            requirement=row.requirement,
            base_commit=row.base_commit,
            target_commit=row.target_commit,
        )

    async def add_task(self, task: AnalysisTask) -> AnalysisTask | None:
        query = text(
            """
            INSERT INTO analysis_tasks
              (id, project_id, analysis_id, status, active_flag, created_at,
               requirement, base_commit, target_commit)
            VALUES
              (:id, :project_id, :analysis_id, 'queued', :project_id, :created_at,
               :requirement, :base_commit, :target_commit)
            """
        )
        params = {
            "id": task.id,
            "project_id": task.project_id,
            "analysis_id": task.analysis_id,
            "created_at": _to_db(task.created_at),
            "requirement": task.requirement,
            "base_commit": task.base_commit,
            "target_commit": task.target_commit,
        }
        async with self._engine.begin() as conn:
            try:
                await conn.execute(query, params)
            except IntegrityError as error:
                # uq_active_task：该项目已有 queued/running 任务
                if _is_duplicate_key(error):
                    return None
                raise
        return task

    async def get_task(self, task_id: str) -> AnalysisTask | None:
        query = text("SELECT * FROM analysis_tasks WHERE id = :id")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"id": task_id})).first()
        return self._task(row) if row else None

    async def update_task(self, task: AnalysisTask) -> AnalysisTask:
        query = text(
            """
            UPDATE analysis_tasks
            SET status = :status,
                active_flag = CASE WHEN :status IN ('queued','running')
                                   THEN :project_id ELSE NULL END,
                started_at = :started_at,
                finished_at = :finished_at, error_code = :error_code,
                error_message = :error_message, node_count = :node_count,
                edge_count = :edge_count, warning_count = :warning_count,
                requirement = :requirement, base_commit = :base_commit,
                target_commit = :target_commit
            WHERE id = :id
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": task.id,
                    "status": task.status,
                    "project_id": task.project_id,
                    "started_at": _to_db(task.started_at) if task.started_at else None,
                    "finished_at": _to_db(task.finished_at) if task.finished_at else None,
                    "error_code": task.error_code,
                    "error_message": task.error_message,
                    "node_count": task.node_count,
                    "edge_count": task.edge_count,
                    "warning_count": task.warning_count,
                    "requirement": task.requirement,
                    "base_commit": task.base_commit,
                    "target_commit": task.target_commit,
                },
            )
        return task

    async def list_by_project(self, project_id: str) -> list[AnalysisTask]:
        query = text(
            "SELECT * FROM analysis_tasks WHERE project_id = :pid ORDER BY created_at DESC"
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query, {"pid": project_id})).all()
        return [self._task(row) for row in rows]

    async def save_graph(self, project_id: str, graph: IRGraph) -> None:
        analysis_id = graph.analysisId
        if analysis_id is None:
            raise ValueError("分析产物缺少 analysisId，无法入库")
        query = text(
            """
            INSERT INTO graphs (project_id, analysis_id, kind, payload, created_at)
            VALUES (:pid, :aid, 'static', :payload, :created_at)
            ON DUPLICATE KEY UPDATE payload = VALUES(payload), created_at = VALUES(created_at)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "pid": project_id,
                    "aid": analysis_id,
                    "payload": graph.model_dump_json(exclude_none=True),
                    "created_at": _to_db(datetime.now(UTC)),
                },
            )

    async def save_merged_graph(self, project_id: str, graph: IRGraph) -> None:
        """合并图沿用 graphs 表（kind='merged'），同一分析多次采集后覆盖为最新结果。"""
        analysis_id = graph.analysisId
        if analysis_id is None:
            raise ValueError("合并产物缺少 analysisId，无法入库")
        query = text(
            """
            INSERT INTO graphs (project_id, analysis_id, kind, payload, created_at)
            VALUES (:pid, :aid, 'merged', :payload, :created_at)
            ON DUPLICATE KEY UPDATE payload = VALUES(payload), created_at = VALUES(created_at)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "pid": project_id,
                    "aid": analysis_id,
                    "payload": graph.model_dump_json(exclude_none=True),
                    "created_at": _to_db(datetime.now(UTC)),
                },
            )

    async def get_graph(self, project_id: str, analysis_id: str) -> IRGraph | None:
        # 同一 analysisId 可能有 static 与 merged 两行：这里只取静态图
        query = text(
            """
            SELECT payload FROM graphs
            WHERE project_id = :pid AND analysis_id = :aid AND kind = 'static'
            """
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"pid": project_id, "aid": analysis_id})).first()
        return IRGraph.model_validate_json(row.payload) if row else None

    async def get_merged_graph(self, project_id: str, analysis_id: str) -> IRGraph | None:
        query = text(
            """
            SELECT payload FROM graphs
            WHERE project_id = :pid AND analysis_id = :aid AND kind = 'merged'
            """
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"pid": project_id, "aid": analysis_id})).first()
        return IRGraph.model_validate_json(row.payload) if row else None

    async def get_latest_graph(self, project_id: str) -> IRGraph | None:
        # merged 按写入时间排在对应 static 之后；若之后又跑了新分析，static 的
        # id 更大、自然重新成为最新——按 id 排序就是正确的时序语义
        query = text(
            """
            SELECT payload FROM graphs WHERE project_id = :pid
            ORDER BY id DESC LIMIT 1
            """
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"pid": project_id})).first()
        return IRGraph.model_validate_json(row.payload) if row else None

    async def save_page_profiles(self, project_id: str, payload: ProfilePayload) -> None:
        query = text(
            """
            INSERT INTO page_profiles (project_id, analysis_id, payload, created_at)
            VALUES (:pid, :aid, :payload, :created_at)
            ON DUPLICATE KEY UPDATE payload = VALUES(payload),
              created_at = VALUES(created_at)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "pid": project_id,
                    "aid": payload.analysis_id,
                    "payload": payload.model_dump_json(),
                    "created_at": _to_db(datetime.now(UTC)),
                },
            )

    async def get_page_profiles(
        self, project_id: str, analysis_id: str
    ) -> ProfilePayload | None:
        # 按 analysisId 精确取：图换了版本，旧画像就描述的是已经不存在的页面
        query = text(
            """
            SELECT payload FROM page_profiles
            WHERE project_id = :pid AND analysis_id = :aid
            """
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"pid": project_id, "aid": analysis_id})).first()
        return ProfilePayload.model_validate_json(row.payload) if row else None

    async def purge_project(self, project_id: str) -> None:
        # MySQL 路径靠 projects 外键级联删除，无需额外语句
        return None


class MySqlRequirementRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _task(self, row: Row[Any]) -> RequirementTask:
        return RequirementTask(
            id=row.id,
            project_id=row.project_id,
            requirement_id=row.requirement_id,
            status=row.status,
            dry_run=bool(row.dry_run),
            requirement=row.requirement,
            analysis_id=row.analysis_id,
            created_at=_from_db(row.created_at),
            started_at=_from_db(row.started_at),
            finished_at=_from_db(row.finished_at),
            error_code=row.error_code,
            error_message=row.error_message,
            candidate_count=row.candidate_count,
            match_count=row.match_count,
            warning_count=row.warning_count,
        )

    async def add_task(self, task: RequirementTask) -> RequirementTask:
        query = text(
            """
            INSERT INTO requirement_tasks
              (id, project_id, requirement_id, status, dry_run, requirement,
               analysis_id, created_at)
            VALUES
              (:id, :pid, :rid, 'queued', :dry_run, :requirement, :aid, :created_at)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": task.id,
                    "pid": task.project_id,
                    "rid": task.requirement_id,
                    "dry_run": task.dry_run,
                    "requirement": task.requirement,
                    "aid": task.analysis_id,
                    "created_at": _to_db(task.created_at),
                },
            )
        return task

    async def get_task(self, task_id: str) -> RequirementTask | None:
        query = text("SELECT * FROM requirement_tasks WHERE id = :id")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"id": task_id})).first()
        return self._task(row) if row else None

    async def update_task(self, task: RequirementTask) -> RequirementTask:
        query = text(
            """
            UPDATE requirement_tasks
            SET status = :status, requirement_id = :rid, analysis_id = :aid,
                started_at = :started_at, finished_at = :finished_at,
                error_code = :error_code, error_message = :error_message,
                candidate_count = :candidate_count, match_count = :match_count,
                warning_count = :warning_count
            WHERE id = :id
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": task.id,
                    "status": task.status,
                    "rid": task.requirement_id,
                    "aid": task.analysis_id,
                    "started_at": _to_db(task.started_at) if task.started_at else None,
                    "finished_at": _to_db(task.finished_at) if task.finished_at else None,
                    "error_code": task.error_code,
                    "error_message": task.error_message,
                    "candidate_count": task.candidate_count,
                    "match_count": task.match_count,
                    "warning_count": task.warning_count,
                },
            )
        return task

    async def list_by_project(self, project_id: str) -> list[RequirementTask]:
        query = text(
            "SELECT * FROM requirement_tasks WHERE project_id = :pid ORDER BY created_at DESC"
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query, {"pid": project_id})).all()
        return [self._task(row) for row in rows]

    async def save_map(self, requirement_map: RequirementMap) -> None:
        query = text(
            """
            INSERT INTO requirement_maps
              (requirement_id, project_id, analysis_id, payload, created_at)
            VALUES (:rid, :pid, :aid, :payload, :created_at)
            ON DUPLICATE KEY UPDATE payload = VALUES(payload),
              analysis_id = VALUES(analysis_id)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "rid": requirement_map.requirementId,
                    "pid": requirement_map.projectId,
                    "aid": requirement_map.analysisId,
                    "payload": requirement_map.model_dump_json(exclude_none=True),
                    "created_at": _to_db(datetime.now(UTC)),
                },
            )

    async def link_task(self, task_id: str, requirement_id: str) -> None:
        # 幂等确认：任务行本就携带 requirement_id 字段，这里做最终绑定
        query = text(
            "UPDATE requirement_tasks SET requirement_id = :rid WHERE id = :id"
        )
        async with self._engine.begin() as conn:
            await conn.execute(query, {"rid": requirement_id, "id": task_id})

    async def get_map(self, requirement_id: str) -> RequirementMap | None:
        query = text("SELECT payload FROM requirement_maps WHERE requirement_id = :rid")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"rid": requirement_id})).first()
        return RequirementMap.model_validate_json(row.payload) if row else None

    async def get_map_by_task(self, task_id: str) -> RequirementMap | None:
        # 两跳：task → requirement_id → map；内存实现的旁路字典在这里不需要
        query = text(
            """
            SELECT m.payload FROM requirement_tasks t
            JOIN requirement_maps m ON m.requirement_id = t.requirement_id
            WHERE t.id = :id
            """
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"id": task_id})).first()
        return RequirementMap.model_validate_json(row.payload) if row else None

    async def purge_project(self, project_id: str) -> None:
        # MySQL 路径靠 projects 外键级联删除，无需额外语句
        return None


class MySqlSpecDocRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _task(self, row: Row[Any]) -> SpecDocTask:
        return SpecDocTask(
            id=row.id,
            project_id=row.project_id,
            requirement_id=row.requirement_id,
            status=row.status,
            dry_run=bool(row.dry_run),
            created_at=_from_db(row.created_at),
            started_at=_from_db(row.started_at),
            finished_at=_from_db(row.finished_at),
            error_code=row.error_code,
            error_message=row.error_message,
            element_count=row.element_count,
            event_count=row.event_count,
            rule_count=row.rule_count,
            warning_count=row.warning_count,
        )

    async def add_task(self, task: SpecDocTask) -> SpecDocTask:
        query = text(
            """
            INSERT INTO specdoc_tasks
              (id, project_id, requirement_id, status, dry_run, created_at)
            VALUES (:id, :pid, :rid, 'queued', :dry_run, :created_at)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": task.id,
                    "pid": task.project_id,
                    "rid": task.requirement_id,
                    "dry_run": task.dry_run,
                    "created_at": _to_db(task.created_at),
                },
            )
        return task

    async def get_task(self, task_id: str) -> SpecDocTask | None:
        query = text("SELECT * FROM specdoc_tasks WHERE id = :id")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"id": task_id})).first()
        return self._task(row) if row else None

    async def update_task(self, task: SpecDocTask) -> SpecDocTask:
        query = text(
            """
            UPDATE specdoc_tasks
            SET status = :status, started_at = :started_at, finished_at = :finished_at,
                error_code = :error_code, error_message = :error_message,
                element_count = :element_count, event_count = :event_count,
                rule_count = :rule_count, warning_count = :warning_count
            WHERE id = :id
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": task.id,
                    "status": task.status,
                    "started_at": _to_db(task.started_at) if task.started_at else None,
                    "finished_at": _to_db(task.finished_at) if task.finished_at else None,
                    "error_code": task.error_code,
                    "error_message": task.error_message,
                    "element_count": task.element_count,
                    "event_count": task.event_count,
                    "rule_count": task.rule_count,
                    "warning_count": task.warning_count,
                },
            )
        return task

    async def list_by_project(self, project_id: str) -> list[SpecDocTask]:
        query = text(
            "SELECT * FROM specdoc_tasks WHERE project_id = :pid ORDER BY created_at DESC"
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query, {"pid": project_id})).all()
        return [self._task(row) for row in rows]

    async def save_doc(self, spec_doc: SpecDocMap) -> None:
        query = text(
            """
            INSERT INTO spec_docs
              (spec_doc_id, project_id, requirement_id, analysis_id, payload, created_at)
            VALUES (:sid, :pid, :rid, :aid, :payload, :created_at)
            ON DUPLICATE KEY UPDATE payload = VALUES(payload),
              analysis_id = VALUES(analysis_id)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "sid": spec_doc.specDocId,
                    "pid": spec_doc.projectId,
                    "rid": spec_doc.requirementId,
                    "aid": spec_doc.analysisId,
                    "payload": spec_doc.model_dump_json(exclude_none=True),
                    "created_at": _to_db(datetime.now(UTC)),
                },
            )

    async def link_task(self, task_id: str, spec_doc_id: str) -> None:
        # specDocId 即任务 id，绑定是幂等确认
        return None

    async def get_doc(self, spec_doc_id: str) -> SpecDocMap | None:
        query = text("SELECT payload FROM spec_docs WHERE spec_doc_id = :sid")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"sid": spec_doc_id})).first()
        return SpecDocMap.model_validate_json(row.payload) if row else None

    async def get_doc_by_task(self, task_id: str) -> SpecDocMap | None:
        # specDocId = 任务 id：单跳直达
        return await self.get_doc(task_id)

    async def purge_project(self, project_id: str) -> None:
        # MySQL 路径靠 projects 外键级联删除，无需额外语句
        return None


class MySqlRuntimeRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _task(self, row: Row[Any]) -> RuntimeTask:
        return RuntimeTask(
            id=row.id,
            project_id=row.project_id,
            trace_id=row.trace_id,
            status=row.status,
            command=row.command,
            url=row.url,
            analysis_id=row.analysis_id,
            settle_ms=row.settle_ms,
            created_at=_from_db(row.created_at),
            started_at=_from_db(row.started_at),
            finished_at=_from_db(row.finished_at),
            error_code=row.error_code,
            error_message=row.error_message,
            component_count=row.component_count,
            network_count=row.network_count,
            console_count=row.console_count,
            interaction_count=row.interaction_count,
        )

    async def add_task(self, task: RuntimeTask) -> RuntimeTask:
        query = text(
            """
            INSERT INTO runtime_tasks
              (id, project_id, trace_id, status, command, url, analysis_id, settle_ms, created_at)
            VALUES
              (:id, :pid, :tid, 'queued', :command, :url, :aid, :settle_ms, :created_at)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": task.id,
                    "pid": task.project_id,
                    "tid": task.trace_id,
                    "command": task.command,
                    "url": task.url,
                    "aid": task.analysis_id,
                    "settle_ms": task.settle_ms,
                    "created_at": _to_db(task.created_at),
                },
            )
        return task

    async def get_task(self, task_id: str) -> RuntimeTask | None:
        query = text("SELECT * FROM runtime_tasks WHERE id = :id")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"id": task_id})).first()
        return self._task(row) if row else None

    async def update_task(self, task: RuntimeTask) -> RuntimeTask:
        query = text(
            """
            UPDATE runtime_tasks
            SET status = :status, started_at = :started_at, finished_at = :finished_at,
                error_code = :error_code, error_message = :error_message,
                component_count = :component_count, network_count = :network_count,
                console_count = :console_count, interaction_count = :interaction_count
            WHERE id = :id
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": task.id,
                    "status": task.status,
                    "started_at": _to_db(task.started_at) if task.started_at else None,
                    "finished_at": _to_db(task.finished_at) if task.finished_at else None,
                    "error_code": task.error_code,
                    "error_message": task.error_message,
                    "component_count": task.component_count,
                    "network_count": task.network_count,
                    "console_count": task.console_count,
                    "interaction_count": task.interaction_count,
                },
            )
        return task

    async def list_by_project(self, project_id: str) -> list[RuntimeTask]:
        query = text(
            "SELECT * FROM runtime_tasks WHERE project_id = :pid ORDER BY created_at DESC"
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query, {"pid": project_id})).all()
        return [self._task(row) for row in rows]

    async def save_trace(self, trace: RuntimeTrace) -> None:
        query = text(
            """
            INSERT INTO runtime_traces (trace_id, project_id, analysis_id, payload, created_at)
            VALUES (:tid, :pid, :aid, :payload, :created_at)
            ON DUPLICATE KEY UPDATE payload = VALUES(payload),
              analysis_id = VALUES(analysis_id)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "tid": trace.traceId,
                    "pid": trace.projectId,
                    "aid": trace.analysisId,
                    "payload": trace.model_dump_json(exclude_none=True),
                    "created_at": _to_db(datetime.now(UTC)),
                },
            )

    async def get_trace(self, trace_id: str) -> RuntimeTrace | None:
        query = text("SELECT payload FROM runtime_traces WHERE trace_id = :tid")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"tid": trace_id})).first()
        return RuntimeTrace.model_validate_json(row.payload) if row else None

    async def get_trace_by_task(self, task_id: str) -> RuntimeTrace | None:
        # 两跳：task → trace_id → trace；task 行在建任务时就携带 trace_id
        query = text(
            """
            SELECT t.payload FROM runtime_tasks k
            JOIN runtime_traces t ON t.trace_id = k.trace_id
            WHERE k.id = :id
            """
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"id": task_id})).first()
        return RuntimeTrace.model_validate_json(row.payload) if row else None

    async def get_latest_trace(self, project_id: str) -> RuntimeTrace | None:
        query = text(
            """
            SELECT payload FROM runtime_traces WHERE project_id = :pid
            ORDER BY created_at DESC LIMIT 1
            """
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"pid": project_id})).first()
        return RuntimeTrace.model_validate_json(row.payload) if row else None

    async def purge_project(self, project_id: str) -> None:
        # MySQL 路径靠 projects 外键级联删除，无需额外语句
        return None


class MySqlExploreRepository:
    """探索任务 + 整包产物（JSON）+ 产物目录。形状与 runtime 仓储同构。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _task(self, row: Row[Any]) -> ExploreTask:
        return ExploreTask(
            id=row.id,
            project_id=row.project_id,
            goal=row.goal,
            status=row.status,
            command=row.command,
            url=row.url,
            analysis_id=row.analysis_id,
            dry_run=bool(row.dry_run),
            max_steps=row.max_steps,
            created_at=_from_db(row.created_at),
            started_at=_from_db(row.started_at),
            finished_at=_from_db(row.finished_at),
            error_code=row.error_code,
            error_message=row.error_message,
            element_count=row.element_count,
            step_count=row.step_count,
            success_count=row.success_count,
            failed_count=row.failed_count,
            screenshot_count=row.screenshot_count,
        )

    async def add_task(self, task: ExploreTask) -> ExploreTask:
        query = text(
            """
            INSERT INTO explore_tasks
              (id, project_id, goal, status, command, url, analysis_id, dry_run,
               max_steps, created_at)
            VALUES
              (:id, :pid, :goal, 'queued', :command, :url, :aid, :dry_run,
               :max_steps, :created_at)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": task.id,
                    "pid": task.project_id,
                    "goal": task.goal,
                    "command": task.command,
                    "url": task.url,
                    "aid": task.analysis_id,
                    "dry_run": task.dry_run,
                    "max_steps": task.max_steps,
                    "created_at": _to_db(task.created_at),
                },
            )
        return task

    async def get_task(self, task_id: str) -> ExploreTask | None:
        query = text("SELECT * FROM explore_tasks WHERE id = :id")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"id": task_id})).first()
        return self._task(row) if row else None

    async def update_task(self, task: ExploreTask) -> ExploreTask:
        query = text(
            """
            UPDATE explore_tasks
            SET status = :status, started_at = :started_at, finished_at = :finished_at,
                error_code = :error_code, error_message = :error_message,
                element_count = :element_count, step_count = :step_count,
                success_count = :success_count, failed_count = :failed_count,
                screenshot_count = :screenshot_count
            WHERE id = :id
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": task.id,
                    "status": task.status,
                    "started_at": _to_db(task.started_at) if task.started_at else None,
                    "finished_at": _to_db(task.finished_at) if task.finished_at else None,
                    "error_code": task.error_code,
                    "error_message": task.error_message,
                    "element_count": task.element_count,
                    "step_count": task.step_count,
                    "success_count": task.success_count,
                    "failed_count": task.failed_count,
                    "screenshot_count": task.screenshot_count,
                },
            )
        return task

    async def list_by_project(self, project_id: str) -> list[ExploreTask]:
        query = text(
            "SELECT * FROM explore_tasks WHERE project_id = :pid ORDER BY created_at DESC"
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query, {"pid": project_id})).all()
        return [self._task(row) for row in rows]

    async def save_artifact(self, artifact: dict[str, Any], artifacts_dir: str) -> None:
        query = text(
            """
            INSERT INTO explore_artifacts (task_id, project_id, payload, artifacts_dir, created_at)
            VALUES (:tid, :pid, :payload, :dir, :created_at)
            ON DUPLICATE KEY UPDATE payload = VALUES(payload),
              artifacts_dir = VALUES(artifacts_dir)
            """
        )
        payload = json.dumps(artifact, ensure_ascii=False)
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "tid": artifact["taskId"],
                    "pid": artifact["projectId"],
                    "payload": payload,
                    "dir": artifacts_dir,
                    "created_at": _to_db(datetime.now(UTC)),
                },
            )

    async def get_artifact(self, task_id: str) -> dict[str, Any] | None:
        query = text("SELECT payload FROM explore_artifacts WHERE task_id = :tid")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"tid": task_id})).first()
        if not row:
            return None
        return json.loads(row.payload)

    async def get_artifacts_dir(self, task_id: str) -> str | None:
        query = text("SELECT artifacts_dir FROM explore_artifacts WHERE task_id = :tid")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"tid": task_id})).first()
        return str(row.artifacts_dir) if row else None

    async def purge_project(self, project_id: str) -> None:
        # MySQL 路径靠 projects 外键级联删除，无需额外语句
        return None
