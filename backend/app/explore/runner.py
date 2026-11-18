"""探索 Worker（复用 runtime-worker，discover / explore 模式）的子进程执行器。

协议与 RuntimeWorkerRunner 同构，但三种模式返回不同产物：
  discover -> discovery（DiscoveredElement 列表）
  explore  -> explore（逐步结果 + 截图文件名）
产物形状由 shared/schemas/explore.schema.json 约定，此处只做契约校验。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.contracts import DiscoveredElement, ExploreStepResult, Screenshot

logger = logging.getLogger(__name__)

STDERR_TAIL_LIMIT = 2000
MAX_ELEMENTS = 500


@dataclass(frozen=True)
class DiscoverWorkerRequest:
    project_id: str
    root_path: str
    url: str | None = None
    command: str | None = None
    analysis_id: str | None = None
    settle_ms: int | None = None
    max_elements: int | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "projectId": self.project_id,
            "rootPath": self.root_path,
            "traceId": f"disc_{self.project_id}",
            "mode": "discover",
        }
        if self.url:
            payload["url"] = self.url
        if self.command:
            payload["command"] = self.command
        if self.analysis_id:
            payload["analysisId"] = self.analysis_id
        if self.settle_ms is not None:
            payload["settleMs"] = self.settle_ms
        if self.max_elements is not None:
            payload["maxElements"] = self.max_elements
        return payload


@dataclass(frozen=True)
class ExploreStepPayload:
    """下发给 Worker 的计划步骤：selector 已由编排层从 discovery 解析完成。"""

    index: int
    action: str
    selector: dict[str, object] | None = None
    value: str | None = None
    assertion: str | None = None
    optional: bool = False

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"index": self.index, "action": self.action}
        if self.selector is not None:
            payload["selector"] = self.selector
        if self.value is not None:
            payload["value"] = self.value
        if self.assertion is not None:
            payload["assertion"] = self.assertion
        if self.optional:
            payload["optional"] = True
        return payload


@dataclass(frozen=True)
class ExploreWorkerRequest:
    project_id: str
    root_path: str
    task_id: str
    steps: list[ExploreStepPayload]
    artifacts_dir: str
    url: str | None = None
    command: str | None = None
    analysis_id: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "projectId": self.project_id,
            "rootPath": self.root_path,
            "traceId": self.task_id,
            "mode": "explore",
            "steps": [step.to_payload() for step in self.steps],
            "artifactsDir": self.artifacts_dir,
        }
        if self.url:
            payload["url"] = self.url
        if self.command:
            payload["command"] = self.command
        if self.analysis_id:
            payload["analysisId"] = self.analysis_id
        return payload


@dataclass(frozen=True)
class DiscoverOutcome:
    ok: bool
    page_url: str | None = None
    title: str | None = None
    elements: list[DiscoveredElement] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    stderr_tail: str = ""
    duration_ms: int = 0


@dataclass(frozen=True)
class ExploreOutcome:
    ok: bool
    page_url: str | None = None
    title: str | None = None
    results: list[ExploreStepResult] = field(default_factory=list)
    screenshots: list[Screenshot] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    stderr_tail: str = ""
    duration_ms: int = 0


class ExploreWorkerRunner:
    """以 node 子进程方式执行 runtime-worker 的 discover / explore 模式。"""

    def __init__(self, node_command: str, entry_path: str) -> None:
        self._node_command = node_command
        self._entry_path = entry_path

    async def _communicate(
        self, payload: dict[str, object], timeout_seconds: float
    ) -> tuple[dict[str, object] | None, dict[str, str] | None, str, int]:
        """公共执行骨架：spawn → stdin → 校验响应。

        失败返回 (None, error, stderr_tail, duration)。
        """
        started = time.monotonic()
        entry = Path(self._entry_path)
        if not entry.is_file():
            return None, {
                "code": "worker_spawn_failed",
                "message": (
                    f"runtime-worker 入口不存在：{entry}"
                    "（先在仓库根执行 npm run build -w @uicodeanalysis/runtime-worker）"
                ),
            }, "", 0

        try:
            proc = await asyncio.create_subprocess_exec(
                self._node_command,
                str(entry),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (NotImplementedError, OSError, ValueError) as error:
            return None, {
                "code": "worker_spawn_failed",
                "message": f"无法启动 Node 子进程（{self._node_command}）：{error}",
            }, "", int((time.monotonic() - started) * 1000)

        stdin_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_data), timeout=timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return None, {
                "code": "worker_timeout",
                "message": f"runtime-worker 超过 {timeout_seconds:.0f}s 未返回，已被终止",
            }, "", int((time.monotonic() - started) * 1000)
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        if stderr_text:
            logger.warning("runtime-worker stderr：\n%s", stderr_text)
        duration_ms = int((time.monotonic() - started) * 1000)

        try:
            parsed: object = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return None, {
                "code": "worker_protocol_error",
                "message": f"runtime-worker 输出不是合法 JSON：{error}",
            }, stderr_text[-STDERR_TAIL_LIMIT:], duration_ms

        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            err = parsed.get("error") if isinstance(parsed, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            message = err.get("message") if isinstance(err, dict) else None
            return None, {
                "code": str(code) if code else "worker_protocol_error",
                "message": str(message) if message else "runtime-worker 返回了无法识别的响应",
            }, stderr_text[-STDERR_TAIL_LIMIT:], duration_ms
        return parsed, None, stderr_text[-STDERR_TAIL_LIMIT:], duration_ms

    async def discover(
        self, request: DiscoverWorkerRequest, timeout_seconds: float
    ) -> DiscoverOutcome:
        payload, error, stderr_tail, duration_ms = await self._communicate(
            request.to_payload(), timeout_seconds
        )
        if error is not None:
            return DiscoverOutcome(
                ok=False,
                error_code=error["code"],
                error_message=error["message"],
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
            )
        assert payload is not None
        discovery = payload.get("discovery")
        if not isinstance(discovery, dict):
            return DiscoverOutcome(
                ok=False,
                error_code="worker_protocol_error",
                error_message="discover 响应缺少 discovery 字段",
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
            )
        raw_elements = discovery.get("elements")
        if not isinstance(raw_elements, list):
            return DiscoverOutcome(
                ok=False,
                error_code="worker_protocol_error",
                error_message="discovery.elements 不是数组",
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
            )
        try:
            elements: list[DiscoveredElement] = []
            for raw in raw_elements[:MAX_ELEMENTS]:
                elements.append(DiscoveredElement.model_validate(raw))
        except ValueError as err:
            return DiscoverOutcome(
                ok=False,
                error_code="invalid_discovery",
                error_message=f"discovery 产物不符合探索契约：{err}",
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
            )
        title = discovery.get("title")
        return DiscoverOutcome(
            ok=True,
            page_url=str(discovery.get("pageUrl") or ""),
            title=str(title) if title else None,
            elements=elements,
            stderr_tail=stderr_tail,
            duration_ms=duration_ms,
        )

    async def explore(
        self, request: ExploreWorkerRequest, timeout_seconds: float
    ) -> ExploreOutcome:
        payload, error, stderr_tail, duration_ms = await self._communicate(
            request.to_payload(), timeout_seconds
        )
        if error is not None:
            return ExploreOutcome(
                ok=False,
                error_code=error["code"],
                error_message=error["message"],
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
            )
        assert payload is not None
        explore = payload.get("explore")
        if not isinstance(explore, dict):
            return ExploreOutcome(
                ok=False,
                error_code="worker_protocol_error",
                error_message="explore 响应缺少 explore 字段",
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
            )
        raw_results = explore.get("results")
        raw_screenshots = explore.get("screenshots")
        if not isinstance(raw_results, list) or not isinstance(raw_screenshots, list):
            return ExploreOutcome(
                ok=False,
                error_code="worker_protocol_error",
                error_message="explore.results / explore.screenshots 不是数组",
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
            )
        try:
            results = [ExploreStepResult.model_validate(raw) for raw in raw_results]
            screenshots = [Screenshot.model_validate(raw) for raw in raw_screenshots]
        except ValueError as err:
            return ExploreOutcome(
                ok=False,
                error_code="invalid_explore",
                error_message=f"explore 产物不符合探索契约：{err}",
                stderr_tail=stderr_tail,
                duration_ms=duration_ms,
            )
        title = explore.get("title")
        return ExploreOutcome(
            ok=True,
            page_url=str(explore.get("pageUrl") or ""),
            title=str(title) if title else None,
            results=results,
            screenshots=screenshots,
            stderr_tail=stderr_tail,
            duration_ms=duration_ms,
        )
