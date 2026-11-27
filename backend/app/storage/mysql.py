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

import hashlib
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
    ChatSession,
    ExploreTask,
    Project,
    RequirementTask,
    RuntimeTask,
    SpecDocTask,
)
from app.storage.chat import MAX_CHAT_SESSIONS_PER_PROJECT
from app.storage.chatdoc_checkpoint import ChatDocJob
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
      updated_at DATETIME(6) NOT NULL,
      CONSTRAINT uq_graph_version UNIQUE (project_id, analysis_id, kind),
      KEY idx_graph_latest (project_id, updated_at, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_revisions (
      revision_id BIGINT AUTO_INCREMENT PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      analysis_id VARCHAR(32) NOT NULL,
      kind VARCHAR(8) NOT NULL,
      state ENUM('writing','published','failed') NOT NULL,
      schema_version VARCHAR(16) NOT NULL,
      payload_bytes BIGINT NOT NULL,
      payload_sha256 CHAR(64) NOT NULL,
      node_count INT NOT NULL,
      edge_count INT NOT NULL,
      chunk_count INT NOT NULL,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      published_at DATETIME(6) NULL,
      error_code VARCHAR(64) NULL,
      error_message TEXT NULL,
      CONSTRAINT uq_graph_revision_version UNIQUE (project_id, analysis_id, kind),
      CONSTRAINT fk_graph_revisions_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
      KEY idx_graph_revisions_latest (project_id, state, published_at, revision_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_revision_chunks (
      revision_id BIGINT NOT NULL,
      chunk_no INT NOT NULL,
      data LONGTEXT NOT NULL,
      byte_length INT NOT NULL,
      chunk_sha256 CHAR(64) NOT NULL,
      created_at DATETIME(6) NOT NULL,
      PRIMARY KEY (revision_id, chunk_no),
      CONSTRAINT fk_graph_revision_chunks_revision FOREIGN KEY (revision_id)
        REFERENCES graph_revisions(revision_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    -- graph_nodes / graph_edges：为 AI 按需检索提供的行式索引层。
    -- 原始图仍然以 payload JSON 完整保存在 graphs（或分块的 graph_revisions），
    -- 新表是完整事实的冗余投影：每个 IRNode 一行、每个 IREdge 一行，
    -- 让 AI 的 EvidenceQuery 可以直接走 WHERE kind=? / FROM / TO 等普通列索引，
    -- 而不是先全量反序列化再在内存里 flatten 过滤。
    -- 写入路径上 _save_graph 会双写：先清旧行再插新行，与 payload JSON 保持同步。
    CREATE TABLE IF NOT EXISTS graph_nodes (
      id            BIGINT AUTO_INCREMENT PRIMARY KEY,
      project_id    VARCHAR(32) NOT NULL,
      analysis_id   VARCHAR(32) NOT NULL,
      graph_kind    VARCHAR(8) NOT NULL,
      node_id       VARCHAR(512) NOT NULL,
      kind          VARCHAR(32) NOT NULL,
      name          VARCHAR(256) NOT NULL,
      file          VARCHAR(512) NULL,
      line          INT NULL,
      loc_col       INT NULL,
      attrs_json    JSON NOT NULL,
      confidence    FLOAT NULL,
      source        VARCHAR(8) NULL,
      created_at    DATETIME(6) NOT NULL,
      UNIQUE KEY uq_graph_node_biz (project_id, analysis_id, graph_kind, node_id),
      KEY idx_graph_node_kind     (project_id, analysis_id, graph_kind, kind),
      KEY idx_graph_node_file     (project_id, analysis_id, graph_kind, file(255))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_edges (
      id            BIGINT AUTO_INCREMENT PRIMARY KEY,
      project_id    VARCHAR(32) NOT NULL,
      analysis_id   VARCHAR(32) NOT NULL,
      graph_kind    VARCHAR(8) NOT NULL,
      edge_id       VARCHAR(512) NOT NULL,
      from_node     VARCHAR(512) NOT NULL,
      to_node       VARCHAR(512) NOT NULL,
      kind          VARCHAR(32) NOT NULL,
      created_at    DATETIME(6) NOT NULL,
      UNIQUE KEY uq_graph_edge_biz (project_id, analysis_id, graph_kind, edge_id),
      KEY idx_graph_edge_from     (project_id, analysis_id, graph_kind, from_node),
      KEY idx_graph_edge_to       (project_id, analysis_id, graph_kind, to_node),
      KEY idx_graph_edge_kind     (project_id, analysis_id, graph_kind, kind)
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
    # AI 需求助手会话：整包 JSON 存放（消息含 docMarkdown 时较大），按项目
    # 保留最近 MAX_CHAT_SESSIONS_PER_PROJECT 条，淘汰由 upsert 事务内完成。
    """
    CREATE TABLE IF NOT EXISTS chat_sessions (
      id VARCHAR(32) PRIMARY KEY,
      project_id VARCHAR(32) NOT NULL,
      payload JSON NOT NULL,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      CONSTRAINT fk_chat_sessions_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
      KEY idx_chat_sessions_project (project_id, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # 对话文档的请求内断点：只覆盖 requirement-chat 的 route_locating 链路，
    # 与正式 specdoc 任务表分开——语义是「一次同步请求可恢复」，不是可轮询任务。
    """
    CREATE TABLE IF NOT EXISTS chat_doc_jobs (
      job_id VARCHAR(64) PRIMARY KEY,
      idempotency_key CHAR(64) NOT NULL,
      project_id VARCHAR(32) NOT NULL,
      analysis_id VARCHAR(32) NULL,
      input_hash CHAR(64) NOT NULL,
      pipeline_version VARCHAR(32) NOT NULL,
      status ENUM('running','succeeded','failed') NOT NULL,
      current_phase VARCHAR(32) NOT NULL,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      expires_at DATETIME(6) NOT NULL,
      CONSTRAINT uq_chat_doc_idem UNIQUE (idempotency_key),
      KEY idx_chat_doc_expires (expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_doc_checkpoints (
      job_id VARCHAR(64) NOT NULL,
      scope VARCHAR(32) NOT NULL,
      version INT NOT NULL,
      payload JSON NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      PRIMARY KEY (job_id, scope),
      CONSTRAINT fk_chat_doc_cp_job FOREIGN KEY (job_id)
        REFERENCES chat_doc_jobs(job_id) ON DELETE CASCADE
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
_GRAPH_INDEX = "idx_graph_latest_v2"


def _graph_payload(graph: IRGraph) -> tuple[str, bytes, str]:
    """序列化图并计算稳定摘要，所有大小均按 UTF-8 字节计。"""
    payload = graph.model_dump_json(exclude_none=True)
    payload_bytes = payload.encode("utf-8")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    return payload, payload_bytes, digest


async def _write_graph_rows(
    conn: Any,
    project_id: str,
    analysis_id: str,
    graph_kind: str,
    graph: IRGraph,
    now: datetime,
) -> None:
    """将图的节点/边投影写入 graph_nodes / graph_edges，为 AI 按需检索提供行式索引层。

    与 payload JSON 双写：先清旧行再插新行，保证同一 (project_id, analysis_id, kind)
    三元组下两张表的内容与 payload 严格一致。attrs 自由扩展字段整体存 JSON，
    常用查询列（kind / file / from_node / to_node）走普通列索引。
    """
    # 先清旧行（幂等）：常规写入和分块写入都会触发，保证 upsert 语义
    await conn.execute(
        text(
            """
            DELETE FROM graph_nodes
            WHERE project_id = :pid AND analysis_id = :aid AND graph_kind = :kind
            """
        ),
        {"pid": project_id, "aid": analysis_id, "kind": graph_kind},
    )
    await conn.execute(
        text(
            """
            DELETE FROM graph_edges
            WHERE project_id = :pid AND analysis_id = :aid AND graph_kind = :kind
            """
        ),
        {"pid": project_id, "aid": analysis_id, "kind": graph_kind},
    )

    # 批量插节点：attrs 整体存 JSON；evidence 只取第一条的 confidence / source
    node_rows = []
    for node in graph.nodes:
        first_evidence = node.evidence[0] if node.evidence else None
        node_rows.append(
            {
                "pid": project_id,
                "aid": analysis_id,
                "kind": graph_kind,
                "node_id": node.id,
                "node_kind": node.kind.value,
                "name": node.name,
                "file": node.file,
                "line": node.loc.line if node.loc is not None else None,
                "loc_col": node.loc.col if node.loc is not None else None,
                "attrs_json": json.dumps(
                    node.attrs, ensure_ascii=False, sort_keys=True
                ),
                "confidence": (
                    first_evidence.confidence
                    if first_evidence is not None
                    else None
                ),
                "source": (
                    first_evidence.source.value
                    if first_evidence is not None
                    else None
                ),
                "created_at": now,
            }
        )

    if node_rows:
        await conn.execute(
            text(
                """
                INSERT INTO graph_nodes
                  (project_id, analysis_id, graph_kind, node_id, kind, name,
                   file, line, loc_col, attrs_json, confidence, source, created_at)
                VALUES
                  (:pid, :aid, :kind, :node_id, :node_kind, :name,
                   :file, :line, :loc_col, :attrs_json,
                   :confidence, :source, :created_at)
                """
            ),
            node_rows,
        )

    # 批量插边
    edge_rows = []
    for edge in graph.edges:
        edge_rows.append(
            {
                "pid": project_id,
                "aid": analysis_id,
                "kind": graph_kind,
                "edge_id": edge.id,
                "from_node": edge.from_,
                "to_node": edge.to,
                "edge_kind": edge.kind.value,
                "created_at": now,
            }
        )

    if edge_rows:
        await conn.execute(
            text(
                """
                INSERT INTO graph_edges
                  (project_id, analysis_id, graph_kind, edge_id,
                   from_node, to_node, kind, created_at)
                VALUES
                  (:pid, :aid, :kind, :edge_id,
                   :from_node, :to_node, :edge_kind, :created_at)
                """
            ),
            edge_rows,
        )


class GraphStorageError(RuntimeError):
    """图产物已写入但无法通过完整性校验。"""


def _is_duplicate_key(error: IntegrityError) -> bool:
    # pymysql 的 ER_DUP_ENTRY = 1062；外键等其他完整性违规必须如实抛出。
    args = getattr(getattr(error, "orig", None), "args", None)
    if not args:
        return False
    return int(args[0]) == 1062


def _split_utf8(payload: bytes, chunk_size: int) -> list[bytes]:
    """按 UTF-8 字符边界切分，避免把多字节字符拆到两个 chunk。"""
    chunks: list[bytes] = []
    start = 0
    while start < len(payload):
        end = min(start + chunk_size, len(payload))
        while end > start:
            try:
                payload[start:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        if end == start:
            raise GraphStorageError("chunk 大小不足以容纳一个 UTF-8 字符")
        chunks.append(payload[start:end])
        start = end
    return chunks


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


async def _ensure_graph_schema(conn: Any) -> None:
    """补齐旧图表缺少的列和可用于 latest 查询的索引。"""
    node_columns = (
        await conn.execute(
            text(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graph_nodes'
                """
            )
        )
    ).all()
    existing_node_columns = {str(row[0]) for row in node_columns}
    if "node_id" not in existing_node_columns:
        # 历史节点无法可靠还原稳定 ID；保留旧行并允许该新增列为空。
        await conn.execute(
            text("ALTER TABLE graph_nodes ADD COLUMN node_id VARCHAR(512) NULL")
        )
        logger.info("graph_nodes 表补充列 node_id")

    legacy_columns = {"node_key", "stable_id", "stable_hash"}
    legacy_rows = (
        await conn.execute(
            text(
                """
                SELECT INDEX_NAME, COLUMN_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graph_nodes'
                """
            )
        )
    ).all()
    legacy_indexes = {
        str(row[0]) for row in legacy_rows if str(row[1]) in legacy_columns
    }
    for index_name in sorted(legacy_indexes):
        # These identifiers are read from information_schema for this known table.
        safe_index_name = index_name.replace("`", "``")
        await conn.execute(text(f"ALTER TABLE graph_nodes DROP INDEX `{safe_index_name}`"))
        logger.info("graph_nodes 表删除遗留索引 %s", index_name)

    for column_name in sorted(legacy_columns & existing_node_columns):
        await conn.execute(
            text(f"ALTER TABLE graph_nodes DROP COLUMN `{column_name}`")
        )
        logger.info("graph_nodes 表删除遗留列 %s", column_name)

    node_index_rows = (
        await conn.execute(
            text(
                """
                SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graph_nodes'
                  AND INDEX_NAME = 'uq_graph_node_biz'
                ORDER BY SEQ_IN_INDEX
                """
            )
        )
    ).all()
    node_index_columns = [str(row[2]) for row in node_index_rows]
    if node_index_columns != ["project_id", "analysis_id", "graph_kind", "node_id"] or (
        node_index_rows and any(int(row[3]) != 0 for row in node_index_rows)
    ):
        if node_index_rows:
            await conn.execute(text("ALTER TABLE graph_nodes DROP INDEX uq_graph_node_biz"))
        await conn.execute(
            text(
                """ALTER TABLE graph_nodes ADD UNIQUE KEY uq_graph_node_biz
                   (project_id, analysis_id, graph_kind, node_id)"""
            )
        )
        logger.info("graph_nodes 表补充唯一索引 uq_graph_node_biz")

    edge_columns = (
        await conn.execute(
            text(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graph_edges'
                """
            )
        )
    ).all()
    existing_edge_columns = {str(row[0]) for row in edge_columns}
    edge_legacy_columns = {"edge_key", "from_key", "to_key", "stable_id"}
    edge_index_rows = (
        await conn.execute(
            text(
                """
                SELECT INDEX_NAME, COLUMN_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graph_edges'
                """
            )
        )
    ).all()
    edge_legacy_indexes = {
        str(row[0]) for row in edge_index_rows if str(row[1]) in edge_legacy_columns
    }
    for index_name in sorted(edge_legacy_indexes):
        safe_index_name = index_name.replace("`", "``")
        await conn.execute(text(f"ALTER TABLE graph_edges DROP INDEX `{safe_index_name}`"))
        logger.info("graph_edges 表删除遗留索引 %s", index_name)

    for column_name, definition in (
        ("edge_id", "VARCHAR(512) NULL"),
        ("from_node", "VARCHAR(512) NULL"),
        ("to_node", "VARCHAR(512) NULL"),
    ):
        if column_name not in existing_edge_columns:
            await conn.execute(
                text(f"ALTER TABLE graph_edges ADD COLUMN `{column_name}` {definition}")
            )
            logger.info("graph_edges 表补充列 %s", column_name)

    for column_name in sorted(edge_legacy_columns & existing_edge_columns):
        await conn.execute(
            text(f"ALTER TABLE graph_edges DROP COLUMN `{column_name}`")
        )
        logger.info("graph_edges 表删除遗留列 %s", column_name)

    edge_index_rows = (
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
    edge_indexes = {
        index_name: [str(row[2]) for row in edge_index_rows if str(row[0]) == index_name]
        for index_name in {str(row[0]) for row in edge_index_rows}
    }
    edge_index_specs = (
        ("uq_graph_edge_biz", ["project_id", "analysis_id", "graph_kind", "edge_id"], True),
        ("idx_graph_edge_from", ["project_id", "analysis_id", "graph_kind", "from_node"], False),
        ("idx_graph_edge_to", ["project_id", "analysis_id", "graph_kind", "to_node"], False),
        ("idx_graph_edge_kind", ["project_id", "analysis_id", "graph_kind", "kind"], False),
    )
    for index_name, expected_columns, unique in edge_index_specs:
        actual_columns = edge_indexes.get(index_name, [])
        if actual_columns == expected_columns:
            continue
        if actual_columns:
            safe_index_name = index_name.replace("`", "``")
            await conn.execute(text(f"ALTER TABLE graph_edges DROP INDEX `{safe_index_name}`"))
        unique_sql = "UNIQUE " if unique else ""
        columns_sql = ", ".join(f"`{column}`" for column in expected_columns)
        await conn.execute(
            text(
                f"ALTER TABLE graph_edges ADD {unique_sql}KEY `{index_name}` ({columns_sql})"
            )
        )
        logger.info("graph_edges 表补充索引 %s", index_name)

    columns = (
        await conn.execute(
            text(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graphs'
                """
            )
        )
    ).all()
    existing_columns = {str(row[0]) for row in columns}
    if "updated_at" not in existing_columns:
        await conn.execute(text("ALTER TABLE graphs ADD COLUMN updated_at DATETIME(6) NULL"))
        await conn.execute(
            text("UPDATE graphs SET updated_at = created_at WHERE updated_at IS NULL")
        )
        logger.info("graphs 表补充列 updated_at")

    index_rows = (
        await conn.execute(
            text(
                """
                SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'graphs'
                  AND INDEX_NAME = :name
                ORDER BY SEQ_IN_INDEX
                """
            ),
            {"name": _GRAPH_INDEX},
        )
    ).all()
    index_columns = [str(row[2]) for row in index_rows]
    if index_columns != ["project_id", "updated_at", "id"]:
        if index_rows:
            # 索引名存在但列顺序错误时，不能继续 FORCE INDEX：否则仍可能
            # 触发 filesort，甚至把错误的历史索引当成修复已完成。
            await conn.execute(text(f"ALTER TABLE graphs DROP INDEX {_GRAPH_INDEX}"))
            logger.warning(
                "graphs 表索引 %s 列顺序异常：%s；已删除并重建",
                _GRAPH_INDEX,
                index_columns,
            )
        await conn.execute(
            text(f"ALTER TABLE graphs ADD KEY {_GRAPH_INDEX} (project_id, updated_at, id)")
        )
        logger.info("graphs 表补充索引 %s", _GRAPH_INDEX)


async def init_schema(engine: AsyncEngine) -> None:
    """幂等建表，并补齐历史库缺少的图索引与列。"""
    async with engine.begin() as conn:
        for statement in DDL_STATEMENTS:
            await conn.execute(text(statement))
        await _ensure_project_columns(conn)
        await _ensure_graph_schema(conn)


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
    def __init__(self, engine: AsyncEngine, settings: Settings | None = None) -> None:
        self._engine = engine
        self._settings = settings or Settings()

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
        await self._save_graph(project_id, graph, "static")

    async def save_merged_graph(self, project_id: str, graph: IRGraph) -> None:
        """合并图沿用 graphs 表（kind='merged'），同一分析多次采集后覆盖。"""
        await self._save_graph(project_id, graph, "merged")

    async def _save_graph(self, project_id: str, graph: IRGraph, kind: str) -> None:
        analysis_id = graph.analysisId
        if analysis_id is None:
            raise ValueError("分析产物缺少 analysisId，无法入库")
        payload, payload_bytes, digest = _graph_payload(graph)
        if len(payload_bytes) > self._settings.graph_max_bytes:
            raise GraphStorageError(
                f"图产物大小 {len(payload_bytes)} bytes 超过上限 "
                f"{self._settings.graph_max_bytes} bytes"
            )
        now = _to_db(datetime.now(UTC))
        if (
            self._settings.graph_chunked_enabled
            and len(payload_bytes) > self._settings.graph_legacy_max_bytes
        ):
            await self._save_chunked_graph(
                project_id, graph, kind, payload_bytes, digest, now
            )
            return
        query = text(
            f"""
            INSERT INTO graphs (project_id, analysis_id, kind, payload, created_at, updated_at)
            VALUES (:pid, :aid, '{kind}', :payload, :created_at, :updated_at)
            ON DUPLICATE KEY UPDATE payload = VALUES(payload),
              updated_at = VALUES(updated_at)
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "pid": project_id,
                    "aid": analysis_id,
                    "payload": payload,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            # 双写：同步行式索引层，供 AI EvidenceQuery 按需检索
            await _write_graph_rows(
                conn, project_id, analysis_id, kind, graph, now
            )
        logger.info(
            "图产物已保存：project=%s analysis=%s kind=%s bytes=%d sha256=%s",
            project_id,
            analysis_id,
            kind,
            len(payload_bytes),
            digest,
        )

    async def _save_chunked_graph(
        self,
        project_id: str,
        graph: IRGraph,
        kind: str,
        payload_bytes: bytes,
        digest: str,
        now: datetime,
    ) -> None:
        """以 writing -> published 状态写入分块图，避免读到半成品。"""
        chunk_size = max(1, self._settings.graph_chunk_bytes)
        chunks = _split_utf8(payload_bytes, chunk_size)
        analysis_id = graph.analysisId
        assert analysis_id is not None
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO graph_revisions (
                      project_id, analysis_id, kind, state, schema_version,
                      payload_bytes, payload_sha256, node_count, edge_count,
                      chunk_count, created_at, updated_at
                    ) VALUES (
                      :pid, :aid, :kind, 'writing', :version,
                      :payload_bytes, :payload_sha256, :node_count, :edge_count,
                      :chunk_count, :created_at, :updated_at
                    )
                    ON DUPLICATE KEY UPDATE
                      state = 'writing', schema_version = VALUES(schema_version),
                      payload_bytes = VALUES(payload_bytes),
                      payload_sha256 = VALUES(payload_sha256),
                      node_count = VALUES(node_count), edge_count = VALUES(edge_count),
                      chunk_count = VALUES(chunk_count), updated_at = VALUES(updated_at),
                      published_at = NULL, error_code = NULL, error_message = NULL
                    """
                ),
                {
                    "pid": project_id,
                    "aid": analysis_id,
                    "kind": kind,
                    "version": graph.version,
                    "payload_bytes": len(payload_bytes),
                    "payload_sha256": digest,
                    "node_count": len(graph.nodes),
                    "edge_count": len(graph.edges),
                    "chunk_count": len(chunks),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            revision = (
                await conn.execute(
                    text(
                        """
                        SELECT revision_id FROM graph_revisions
                        WHERE project_id = :pid AND analysis_id = :aid AND kind = :kind
                        """
                    ),
                    {"pid": project_id, "aid": analysis_id, "kind": kind},
                )
            ).first()
            if revision is None:
                raise GraphStorageError("分块图版本创建后无法读取 revision_id")
            revision_id = int(revision.revision_id)
            await conn.execute(
                text("DELETE FROM graph_revision_chunks WHERE revision_id = :revision_id"),
                {"revision_id": revision_id},
            )
            for chunk_no, chunk in enumerate(chunks):
                await conn.execute(
                    text(
                        """
                        INSERT INTO graph_revision_chunks
                          (revision_id, chunk_no, data, byte_length, chunk_sha256, created_at)
                        VALUES (
                          :revision_id, :chunk_no, :data, :byte_length,
                          :chunk_sha256, :created_at
                        )
                        """
                    ),
                    {
                        "revision_id": revision_id,
                        "chunk_no": chunk_no,
                        "data": chunk.decode("utf-8"),
                        "byte_length": len(chunk),
                        "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
                        "created_at": now,
                    },
                )
            await conn.execute(
                text(
                    """
                    UPDATE graph_revisions
                    SET state = 'published', published_at = :published_at, updated_at = :updated_at
                    WHERE revision_id = :revision_id
                    """
                ),
                {"revision_id": revision_id, "published_at": now, "updated_at": now},
            )
            # 双写：同步行式索引层，供 AI EvidenceQuery 按需检索
            await _write_graph_rows(
                conn, project_id, analysis_id, kind, graph, now
            )
        logger.info(
            "图产物已分块保存：project=%s analysis=%s kind=%s bytes=%d chunks=%d sha256=%s",
            project_id,
            analysis_id,
            kind,
            len(payload_bytes),
            len(chunks),
            digest,
        )

    async def _read_chunked_graph(
        self, project_id: str, analysis_id: str, kind: str
    ) -> IRGraph | None:
        async with self._engine.connect() as conn:
            revision = (
                await conn.execute(
                    text(
                        """
                        SELECT revision_id, payload_bytes, payload_sha256, chunk_count
                        FROM graph_revisions
                        WHERE project_id = :pid AND analysis_id = :aid
                          AND kind = :kind AND state = 'published'
                        """
                    ),
                    {"pid": project_id, "aid": analysis_id, "kind": kind},
                )
            ).first()
            if revision is None:
                return None
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT chunk_no, data, byte_length, chunk_sha256
                        FROM graph_revision_chunks
                        WHERE revision_id = :revision_id ORDER BY chunk_no
                        """
                    ),
                    {"revision_id": revision.revision_id},
                )
            ).all()
        if len(rows) != int(revision.chunk_count):
            raise GraphStorageError("分块图数量不完整，拒绝读取")
        parts: list[bytes] = []
        for expected_no, row in enumerate(rows):
            chunk = str(row.data).encode("utf-8")
            if int(row.chunk_no) != expected_no or len(chunk) != int(row.byte_length):
                raise GraphStorageError("分块图序号或大小校验失败")
            if hashlib.sha256(chunk).hexdigest() != str(row.chunk_sha256):
                raise GraphStorageError("分块图校验和不匹配")
            parts.append(chunk)
        payload_bytes = b"".join(parts)
        if len(payload_bytes) != int(revision.payload_bytes):
            raise GraphStorageError("分块图总大小校验失败")
        if hashlib.sha256(payload_bytes).hexdigest() != str(revision.payload_sha256):
            raise GraphStorageError("分块图总校验和不匹配")
        try:
            graph = IRGraph.model_validate_json(payload_bytes)
        except ValueError as error:
            raise GraphStorageError(f"分块图不符合 IR 契约：{error}") from error
        if graph.analysisId != analysis_id:
            raise GraphStorageError("分块图 analysisId 与查询条件不一致")
        return graph

    async def get_graph(self, project_id: str, analysis_id: str) -> IRGraph | None:
        if self._settings.graph_chunked_read_enabled:
            chunked = await self._read_chunked_graph(project_id, analysis_id, "static")
            if chunked is not None:
                return chunked
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
        if self._settings.graph_chunked_read_enabled:
            chunked = await self._read_chunked_graph(project_id, analysis_id, "merged")
            if chunked is not None:
                return chunked
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
        if self._settings.graph_chunked_read_enabled:
            async with self._engine.connect() as conn:
                revision = (
                    await conn.execute(
                        text(
                            """
                            SELECT analysis_id, kind FROM graph_revisions
                            WHERE project_id = :pid AND state = 'published'
                            ORDER BY published_at DESC, revision_id DESC LIMIT 1
                            """
                        ),
                        {"pid": project_id},
                    )
                ).first()
            if revision is not None:
                chunked = await self._read_chunked_graph(
                    project_id, str(revision.analysis_id), str(revision.kind)
                )
                if chunked is not None:
                    return chunked
        query = text(
            """
            SELECT payload FROM graphs FORCE INDEX (idx_graph_latest_v2)
            WHERE project_id = :pid
            ORDER BY updated_at DESC, id DESC LIMIT 1
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


class MySqlChatSessionRepository:
    """AI 需求助手会话：整包 JSON 存放，每项目保留上限条数（upsert 时淘汰）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _session(self, row: Row[Any]) -> ChatSession:
        return ChatSession.model_validate_json(row.payload)

    async def upsert(self, session: ChatSession) -> ChatSession:
        query = text(
            """
            INSERT INTO chat_sessions (id, project_id, payload, created_at, updated_at)
            VALUES (:id, :pid, :payload, :created_at, :updated_at)
            ON DUPLICATE KEY UPDATE payload = VALUES(payload),
              updated_at = VALUES(updated_at)
            """
        )
        # 淘汰：保留该项目最近活跃的 N 条（含刚写入的这一条），其余删除。
        # MySQL 不允许 DELETE 直接引用同表的子查询，包一层派生表绕开限制。
        evict = text(
            f"""
            DELETE FROM chat_sessions
            WHERE project_id = :pid
              AND id NOT IN (
                SELECT keep_id FROM (
                  SELECT id AS keep_id FROM chat_sessions
                  WHERE project_id = :pid
                  ORDER BY updated_at DESC, id DESC
                  LIMIT {MAX_CHAT_SESSIONS_PER_PROJECT}
                ) AS keep
              )
            """
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                query,
                {
                    "id": session.id,
                    "pid": session.project_id,
                    "payload": session.model_dump_json(exclude_none=True),
                    "created_at": _to_db(session.created_at),
                    "updated_at": _to_db(session.updated_at),
                },
            )
            await conn.execute(evict, {"pid": session.project_id})
        return session

    async def get(self, session_id: str) -> ChatSession | None:
        query = text("SELECT payload FROM chat_sessions WHERE id = :id")
        async with self._engine.connect() as conn:
            row = (await conn.execute(query, {"id": session_id})).first()
        return self._session(row) if row else None

    async def list_by_project(self, project_id: str) -> list[ChatSession]:
        query = text(
            "SELECT payload FROM chat_sessions WHERE project_id = :pid"
            " ORDER BY updated_at DESC, id DESC"
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query, {"pid": project_id})).all()
        return [self._session(row) for row in rows]

    async def delete(self, session_id: str) -> bool:
        query = text("DELETE FROM chat_sessions WHERE id = :id")
        async with self._engine.begin() as conn:
            result = await conn.execute(query, {"id": session_id})
        return result.rowcount > 0


class MySqlChatDocCheckpointRepository:
    """MySQL repository for resumable chat-generated spec documents."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    def _job(row: Row[Any]) -> ChatDocJob:
        return ChatDocJob(
            job_id=row.job_id,
            idempotency_key=row.idempotency_key,
            project_id=row.project_id,
            analysis_id=row.analysis_id,
            input_hash=row.input_hash,
            pipeline_version=row.pipeline_version,
            status=row.status,
            current_phase=row.current_phase,
            created_at=_from_db(row.created_at),
            expires_at=_from_db(row.expires_at),
        )

    async def get_or_create_job(self, job: ChatDocJob) -> ChatDocJob:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO chat_doc_jobs
                      (job_id, idempotency_key, project_id, analysis_id, input_hash,
                       pipeline_version, status, current_phase, created_at, updated_at, expires_at)
                    VALUES (:job_id, :idem, :pid, :aid, :input_hash, :version,
                            'running', 'locating', :created, :created, :expires)
                    ON DUPLICATE KEY UPDATE
                      updated_at = VALUES(updated_at),
                      expires_at = GREATEST(expires_at, VALUES(expires_at))
                    """
                ),
                {
                    "job_id": job.job_id,
                    "idem": job.idempotency_key,
                    "pid": job.project_id,
                    "aid": job.analysis_id,
                    "input_hash": job.input_hash,
                    "version": job.pipeline_version,
                    "created": _to_db(job.created_at),
                    "expires": _to_db(job.expires_at),
                },
            )
            row = (
                await conn.execute(
                    text("SELECT * FROM chat_doc_jobs WHERE idempotency_key = :idem FOR UPDATE"),
                    {"idem": job.idempotency_key},
                )
            ).first()
        if row is None:
            raise RuntimeError("chat doc job insert did not return a row")
        existing = self._job(row)
        if (
            existing.input_hash != job.input_hash
            or existing.pipeline_version != job.pipeline_version
        ):
            raise ValueError("idempotency key conflicts with a different chat doc input")
        return existing

    async def load_job(self, job_id: str) -> ChatDocJob | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(
                text("SELECT * FROM chat_doc_jobs WHERE job_id = :id"), {"id": job_id}
            )).first()
        return self._job(row) if row else None

    async def load_scope(self, job_id: str, scope: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT payload FROM chat_doc_checkpoints "
                        "WHERE job_id = :id AND scope = :scope"
                    ),
                    {"id": job_id, "scope": scope},
                )
            ).first()
        if row is None:
            return None
        return json.loads(row.payload) if isinstance(row.payload, str) else dict(row.payload)

    async def save_scope(
        self, job_id: str, scope: str, payload: dict[str, Any], *, phase: str
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO chat_doc_checkpoints (job_id, scope, version, payload, updated_at)
                    VALUES (:id, :scope, :version, :payload, NOW(6))
                    ON DUPLICATE KEY UPDATE version = VALUES(version),
                      payload = VALUES(payload), updated_at = VALUES(updated_at)
                    """
                ),
                {"id": job_id, "scope": scope, "version": 1,
                 "payload": json.dumps(payload, ensure_ascii=False)},
            )
            await conn.execute(
                text(
                    "UPDATE chat_doc_jobs SET current_phase = :phase, "
                    "updated_at = NOW(6) WHERE job_id = :id"
                ),
                {"phase": phase, "id": job_id},
            )

    async def finish_job(self, job_id: str, status: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE chat_doc_jobs SET status = :status, "
                    "updated_at = NOW(6) WHERE job_id = :id"
                ),
                {"status": status, "id": job_id},
            )

    async def purge_expired(self) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(text("DELETE FROM chat_doc_jobs WHERE expires_at < NOW(6)"))
        return result.rowcount
