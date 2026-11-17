"""运行时采集 Worker 的子进程执行器，与 analysis/runner.py 同构。

Worker 通过 stdin/stdout 交换 JSON（协议见 workers/runtime-worker/src/protocol.ts）：
  stdin  <- RuntimeWorkerRequest
  stdout -> RuntimeWorkerResponse（内含 RuntimeTrace 或 error）

失败同样区分三种情形：worker 自身报错（透传其 code）、被超时终止（worker_timeout）、
本地环境/协议问题（worker_spawn_failed / worker_protocol_error / invalid_trace）。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.contracts import RuntimeTrace

logger = logging.getLogger(__name__)

STDERR_TAIL_LIMIT = 2000


@dataclass(frozen=True)
class RuntimeWorkerRequest:
    project_id: str
    root_path: str
    trace_id: str
    analysis_id: str | None = None
    url: str | None = None
    command: str | None = None
    settle_ms: int | None = None

    def to_payload(self) -> dict[str, object]:
        """序列化为 worker 协议的 camelCase 请求体；None 字段不下发。"""
        payload: dict[str, object] = {
            "projectId": self.project_id,
            "rootPath": self.root_path,
            "traceId": self.trace_id,
        }
        if self.analysis_id:
            payload["analysisId"] = self.analysis_id
        if self.url:
            payload["url"] = self.url
        if self.command:
            payload["command"] = self.command
        if self.settle_ms is not None:
            payload["settleMs"] = self.settle_ms
        return payload


@dataclass(frozen=True)
class RuntimeWorkerOutcome:
    ok: bool
    trace: RuntimeTrace | None = None
    error_code: str | None = None
    error_message: str | None = None
    stderr_tail: str = ""
    duration_ms: int = 0


class RuntimeWorkerRunner(Protocol):
    async def run(
        self, request: RuntimeWorkerRequest, timeout_seconds: float
    ) -> RuntimeWorkerOutcome: ...


def _tail(text: str) -> str:
    return text[-STDERR_TAIL_LIMIT:]


class SubprocessRuntimeRunner:
    """以 node 子进程方式执行 runtime-worker；单次采集一个进程，采集完即退。"""

    def __init__(self, node_command: str, entry_path: str) -> None:
        self._node_command = node_command
        self._entry_path = entry_path

    async def run(
        self, request: RuntimeWorkerRequest, timeout_seconds: float
    ) -> RuntimeWorkerOutcome:
        started = time.monotonic()
        entry = Path(self._entry_path)
        if not entry.is_file():
            return RuntimeWorkerOutcome(
                ok=False,
                error_code="worker_spawn_failed",
                error_message=(
                    f"runtime-worker 入口不存在：{entry}"
                    "（先在仓库根执行 npm run build -w @uicodeanalysis/runtime-worker）"
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
            return RuntimeWorkerOutcome(
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
            return RuntimeWorkerOutcome(
                ok=False,
                error_code="worker_timeout",
                error_message=f"runtime-worker 超过 {timeout_seconds:.0f}s 未返回，已被终止",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        if stderr_text:
            logger.warning("runtime-worker stderr：\n%s", stderr_text)
        duration_ms = int((time.monotonic() - started) * 1000)

        try:
            payload: object = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return RuntimeWorkerOutcome(
                ok=False,
                error_code="worker_protocol_error",
                error_message=f"runtime-worker 输出不是合法 JSON：{error}",
                stderr_tail=_tail(stderr_text),
                duration_ms=duration_ms,
            )

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            err = payload.get("error") if isinstance(payload, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            message = err.get("message") if isinstance(err, dict) else None
            return RuntimeWorkerOutcome(
                ok=False,
                error_code=str(code) if code else "worker_protocol_error",
                error_message=str(message) if message else "runtime-worker 返回了无法识别的响应",
                stderr_tail=_tail(stderr_text),
                duration_ms=duration_ms,
            )
        try:
            trace = RuntimeTrace.model_validate(payload["trace"])
        except (KeyError, ValueError) as error:
            return RuntimeWorkerOutcome(
                ok=False,
                error_code="invalid_trace",
                error_message=f"runtime-worker 产物不符合运行时契约：{error}",
                stderr_tail=_tail(stderr_text),
                duration_ms=duration_ms,
            )

        return RuntimeWorkerOutcome(
            ok=True, trace=trace, stderr_tail=_tail(stderr_text), duration_ms=duration_ms
        )
