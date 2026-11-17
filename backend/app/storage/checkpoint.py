"""LangGraph checkpointer 的 MySQL 实现。

用途定位是**审计而非恢复**：需求定位是短事务（最多几次模型调用），进程重启后
任务会被 boot recovery 标记为 failed，由用户重试；这里落盘的中间状态用来回答
「模型为什么给出这个结论」——decompose / recall / judge 每一步的输入输出都
留在 requirement 线程下，可按 thread_id（= task.id）回放。

因此实现只做协议的最小闭环：aput / aget_tuple / alist / aput_writes /
adelete_thread。同步方法不被编排层使用，直接拒绝以暴露误用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

CHECKPOINT_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
      thread_id VARCHAR(64) NOT NULL,
      checkpoint_ns VARCHAR(64) NOT NULL DEFAULT '',
      checkpoint_id VARCHAR(64) NOT NULL,
      parent_id VARCHAR(64) NULL,
      type VARCHAR(32) NOT NULL,
      payload LONGBLOB NOT NULL,
      meta_type VARCHAR(32) NOT NULL,
      meta_payload LONGBLOB NULL,
      created_at DATETIME(6) NOT NULL,
      PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS langgraph_checkpoint_writes (
      thread_id VARCHAR(64) NOT NULL,
      checkpoint_ns VARCHAR(64) NOT NULL DEFAULT '',
      checkpoint_id VARCHAR(64) NOT NULL,
      task_id VARCHAR(64) NOT NULL,
      idx INT NOT NULL,
      channel VARCHAR(64) NOT NULL,
      type VARCHAR(32) NOT NULL,
      payload LONGBLOB NOT NULL,
      PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


def _thread_config(config: RunnableConfig) -> tuple[str, str]:
    configurable = config.get("configurable") or {}
    return (
        str(configurable.get("thread_id", "")),
        str(configurable.get("checkpoint_ns", "")),
    )


def _config_for(thread_id: str, ns: str, checkpoint_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": ns,
            "checkpoint_id": checkpoint_id,
        }
    }


class MySqlCheckpointSaver(BaseCheckpointSaver):
    """复用业务侧的 AsyncEngine：不另建连接池。"""

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__()
        self._engine = engine

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, Any],
    ) -> RunnableConfig:
        thread_id, ns = _thread_config(config)
        cp_type, cp_blob = self.serde.dumps_typed(checkpoint)
        meta_type, meta_blob = self.serde.dumps_typed(dict(metadata))
        parent_id = (config.get("configurable") or {}).get("checkpoint_id")
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO langgraph_checkpoints
                      (thread_id, checkpoint_ns, checkpoint_id, parent_id,
                       type, payload, meta_type, meta_payload, created_at)
                    VALUES (:thread_id, :ns, :cp_id, :parent_id,
                            :type, :payload, :meta_type, :meta_payload, NOW(6))
                    ON DUPLICATE KEY UPDATE
                      parent_id = VALUES(parent_id), type = VALUES(type),
                      payload = VALUES(payload), meta_type = VALUES(meta_type),
                      meta_payload = VALUES(meta_payload), created_at = VALUES(created_at)
                    """
                ),
                {
                    "thread_id": thread_id,
                    "ns": ns,
                    "cp_id": checkpoint["id"],
                    "parent_id": parent_id,
                    "type": cp_type,
                    "payload": cp_blob,
                    "meta_type": meta_type,
                    "meta_payload": meta_blob,
                },
            )
        return _config_for(thread_id, ns, checkpoint["id"])

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, ns = _thread_config(config)
        checkpoint_id = (config.get("configurable") or {}).get("checkpoint_id")
        if checkpoint_id is None:
            return
        async with self._engine.begin() as conn:
            for idx, (channel, value) in enumerate(writes):
                blob_type, blob = self.serde.dumps_typed(value)
                await conn.execute(
                    text(
                        """
                        INSERT INTO langgraph_checkpoint_writes
                          (thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
                           channel, type, payload)
                        VALUES (:thread_id, :ns, :cp_id, :task_id, :idx,
                                :channel, :type, :payload)
                        ON DUPLICATE KEY UPDATE
                          channel = VALUES(channel), type = VALUES(type),
                          payload = VALUES(payload)
                        """
                    ),
                    {
                        "thread_id": thread_id,
                        "ns": ns,
                        "cp_id": checkpoint_id,
                        "task_id": task_id,
                        # 特殊通道（__error__/__interrupt__ 等）有协议约定的固定槽位
                        "idx": WRITES_IDX_MAP.get(channel, idx),
                        "channel": channel,
                        "type": blob_type,
                        "payload": blob,
                    },
                )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, ns = _thread_config(config)
        requested_id = (config.get("configurable") or {}).get("checkpoint_id")
        async with self._engine.connect() as conn:
            checkpoint_id = requested_id or await self._latest_checkpoint_id(conn, thread_id, ns)
            if checkpoint_id is None:
                return None
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT checkpoint_id, parent_id, type, payload,
                               meta_type, meta_payload
                        FROM langgraph_checkpoints
                        WHERE thread_id = :thread_id AND checkpoint_ns = :ns
                          AND checkpoint_id = :cp_id
                        """
                    ),
                    {"thread_id": thread_id, "ns": ns, "cp_id": checkpoint_id},
                )
            ).first()
            if row is None:
                return None
            writes = await self._load_writes(conn, thread_id, ns, checkpoint_id)

        checkpoint = self.serde.loads_typed((row.type, bytes(row.payload)))
        parent_config = _config_for(thread_id, ns, row.parent_id) if row.parent_id else None
        return CheckpointTuple(
            config=_config_for(thread_id, ns, row.checkpoint_id),
            checkpoint=checkpoint,
            metadata=self.serde.loads_typed((row.meta_type, bytes(row.meta_payload)))
            if row.meta_payload is not None
            else {},
            parent_config=parent_config,
            pending_writes=writes,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        # 编排层只用 aget_tuple（单线程回放）；alist 保留协议闭环，不支持
        # filter/before，审计场景直接按时间倒序给出即可。
        if config is None:
            return
        thread_id, ns = _thread_config(config)
        sql = (
            "SELECT checkpoint_id FROM langgraph_checkpoints "
            "WHERE thread_id = :thread_id AND checkpoint_ns = :ns "
            "ORDER BY created_at DESC, checkpoint_id DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        async with self._engine.connect() as conn:
            rows = (await conn.execute(text(sql), {"thread_id": thread_id, "ns": ns})).all()
        for row in rows:
            tuple_result = await self.aget_tuple(_config_for(thread_id, ns, row.checkpoint_id))
            if tuple_result is not None:
                yield tuple_result

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._engine.begin() as conn:
            for table in ("langgraph_checkpoints", "langgraph_checkpoint_writes"):
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),
                    {"thread_id": thread_id},
                )

    async def _latest_checkpoint_id(
        self, conn: AsyncConnection, thread_id: str, ns: str
    ) -> str | None:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT checkpoint_id FROM langgraph_checkpoints
                    WHERE thread_id = :thread_id AND checkpoint_ns = :ns
                    ORDER BY created_at DESC, checkpoint_id DESC
                    LIMIT 1
                    """
                ),
                {"thread_id": thread_id, "ns": ns},
            )
        ).first()
        return row.checkpoint_id if row else None

    async def _load_writes(
        self, conn: AsyncConnection, thread_id: str, ns: str, checkpoint_id: str
    ) -> list[PendingWrite]:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT task_id, idx, channel, type, payload
                    FROM langgraph_checkpoint_writes
                    WHERE thread_id = :thread_id AND checkpoint_ns = :ns
                      AND checkpoint_id = :cp_id
                    ORDER BY task_id, idx
                    """
                ),
                {"thread_id": thread_id, "ns": ns, "cp_id": checkpoint_id},
            )
        ).all()
        return [
            (
                row.task_id,
                row.channel,
                self.serde.loads_typed((row.type, bytes(row.payload))),
            )
            for row in rows
        ]

    # 同步协议方法不被本工程的编排层使用；显式拒绝比静默走线程池更诚实
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError("编排层只支持异步路径（aget_tuple）")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, Any],
    ) -> RunnableConfig:
        raise NotImplementedError("编排层只支持异步路径（aput）")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError("编排层只支持异步路径（aput_writes）")

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        raise NotImplementedError("编排层只支持异步路径（alist）")

    def delete_thread(self, thread_id: str) -> None:
        raise NotImplementedError("编排层只支持异步路径（adelete_thread）")
