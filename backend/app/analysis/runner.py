"""静态分析 Worker 的子进程执行器。

Worker 通过 stdin/stdout 交换 JSON（协议见 workers/ast-worker/src/protocol.ts）：
  stdin  <- WorkerRequest
  stdout -> WorkerResponse（内含 IRGraph 或 error）

失败可区分三种情形，对应不同的 error_code：
  - worker 自身报错（退出码 0，stdout 带 { ok:false, error }）：透传其 code
  - worker 被超时终止：worker_timeout
  - 本地环境/协议问题：worker_spawn_failed / worker_protocol_error / invalid_ir
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.contracts import IRGraph

logger = logging.getLogger(__name__)

STDERR_TAIL_LIMIT = 2000

type FrameworkName = Literal["react", "next"]
type ScopeMode = Literal["all", "primary"]


@dataclass(frozen=True)
class WorkerRequest:
    project_id: str
    root_path: str
    analysis_id: str
    changed_files: list[str] | None = None
    framework: FrameworkName | None = None
    source_roots: list[str] | None = None
    exclude_tests: bool | None = None
    exclude_storybook: bool | None = None
    scope_mode: ScopeMode | None = None

    def to_payload(self) -> dict[str, object]:
        """序列化为 worker 协议的 camelCase 请求体。"""
        payload: dict[str, object] = {
            "projectId": self.project_id,
            "rootPath": self.root_path,
            "analysisId": self.analysis_id,
        }
        if self.changed_files:
            payload["changedFiles"] = self.changed_files
        if self.framework:
            payload["framework"] = self.framework
        if self.source_roots:
            payload["sourceRoots"] = self.source_roots
        if self.exclude_tests is not None:
            payload["excludeTests"] = self.exclude_tests
        if self.exclude_storybook is not None:
            payload["excludeStorybook"] = self.exclude_storybook
        if self.scope_mode:
            payload["scopeMode"] = self.scope_mode
        return payload


@dataclass(frozen=True)
class WorkerOutcome:
    ok: bool
    graph: IRGraph | None = None
    error_code: str | None = None
    error_message: str | None = None
    stderr_tail: str = ""
    duration_ms: int = 0


class WorkerRunner(Protocol):
    async def run(self, request: WorkerRequest, timeout_seconds: float) -> WorkerOutcome: ...


def _tail(text: str) -> str:
    return text[-STDERR_TAIL_LIMIT:]


class SubprocessWorkerRunner:
    """以 node 子进程方式执行 ast-worker；单次分析一个进程，分析完即退。"""

    def __init__(self, node_command: str, entry_path: str) -> None:
        self._node_command = node_command
        self._entry_path = entry_path

    async def run(self, request: WorkerRequest, timeout_seconds: float) -> WorkerOutcome:
        started = time.monotonic()
        entry = Path(self._entry_path)
        if not entry.is_file():
            return WorkerOutcome(
                ok=False,
                error_code="worker_spawn_failed",
                error_message=(
                    f"ast-worker 入口不存在：{entry}"
                    "（先在仓库根执行 npm run build -w @uicodeanalysis/ast-worker）"
                ),
                duration_ms=0,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                self._node_command,
                str(entry),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (NotImplementedError, OSError, ValueError) as error:
            return WorkerOutcome(
                ok=False,
                error_code="worker_spawn_failed",
                error_message=f"无法启动 Node 子进程（{self._node_command}）：{error}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        stdin_data = json.dumps(request.to_payload()).encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_data), timeout=timeout_seconds
            )
        except TimeoutError:  # asyncio.TimeoutError 的内建别名
            proc.kill()
            await proc.wait()
            return WorkerOutcome(
                ok=False,
                error_code="worker_timeout",
                error_message=f"ast-worker 超过 {timeout_seconds:.0f}s 未返回，已被终止",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        if stderr_text:
            logger.warning("ast-worker stderr：\n%s", stderr_text)
        duration_ms = int((time.monotonic() - started) * 1000)

        try:
            payload: object = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return WorkerOutcome(
                ok=False,
                error_code="worker_protocol_error",
                error_message=f"ast-worker 输出不是合法 JSON：{error}",
                stderr_tail=_tail(stderr_text),
                duration_ms=duration_ms,
            )

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            err = payload.get("error") if isinstance(payload, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            message = err.get("message") if isinstance(err, dict) else None
            return WorkerOutcome(
                ok=False,
                error_code=str(code) if code else "worker_protocol_error",
                error_message=str(message) if message else "ast-worker 返回了无法识别的响应",
                stderr_tail=_tail(stderr_text),
                duration_ms=duration_ms,
            )
        try:
            graph = IRGraph.model_validate(payload["graph"])
        except (KeyError, ValueError) as error:
            return WorkerOutcome(
                ok=False,
                error_code="invalid_ir",
                error_message=f"ast-worker 产物不符合 IR 契约：{error}",
                stderr_tail=_tail(stderr_text),
                duration_ms=duration_ms,
            )

        return WorkerOutcome(
            ok=True, graph=graph, stderr_tail=_tail(stderr_text), duration_ms=duration_ms
        )
