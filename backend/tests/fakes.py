"""测试用假 Worker：不启动真实子进程，用于编排层的单元测试。

timeout 语义与真实 SubprocessWorkerRunner 对齐：
run() 自身负责超时判定，超时返回 worker_timeout 结果而不是抛异常。
"""

import asyncio
import json
from pathlib import Path

from app.analysis.runner import WorkerOutcome, WorkerRequest
from app.contracts import (
    Action,
    ComponentNode,
    DiscoveredElement,
    EdgeKind,
    Evidence,
    ExploreStepResult,
    IREdge,
    IRGraph,
    IRNode,
    Location,
    NodeKind,
    PageInfo,
    Result,
    RuntimeTrace,
    Screenshot,
    Source,
)
from app.explore.runner import (
    DiscoverOutcome,
    DiscoverWorkerRequest,
    ExploreOutcome,
    ExploreWorkerRequest,
)
from app.git_client import GitError, GitSnapshot
from app.requirement.llm import LLMCallError
from app.runtime.runner import RuntimeWorkerOutcome, RuntimeWorkerRequest

_HANG_SECONDS = 5.0


class FakeGitClient:
    """假 Git 客户端：只落一个最小 package.json，用来验证去重与同步语义。"""

    def __init__(
        self,
        branch: str = "main",
        commit: str = "a" * 40,
        *,
        next_commit: str = "b" * 40,
        error: str | None = None,
    ) -> None:
        self.branch = branch
        self.commit = commit
        self.next_commit = next_commit
        self.error = error
        self.clone_calls: list[
            tuple[str, Path, str | None, str | None, str | None, str | None]
        ] = []
        self.sync_calls: list[Path] = []

    async def clone(
        self,
        remote_url: str,
        destination: Path,
        branch: str | None = None,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ) -> GitSnapshot:
        if self.error is not None:
            raise GitError(self.error)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "package.json").write_text(
            json.dumps({"dependencies": {"react": "^19.0.0"}}), encoding="utf-8"
        )
        self.clone_calls.append((remote_url, destination, branch, username, password, token))
        return GitSnapshot(branch=branch or self.branch, commit=self.commit)

    async def sync(self, root: Path) -> GitSnapshot:
        if self.error is not None:
            raise GitError(self.error)
        self.sync_calls.append(root)
        self.commit = self.next_commit
        return GitSnapshot(branch=self.branch, commit=self.commit)


class FakeLLMClient:
    """脚本化模型：按序返回响应，记录每次调用的 prompt 供断言。"""

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        error: LLMCallError | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("FakeLLMClient 的响应脚本已用尽")
        return self.responses.pop(0)


def _mini_graph(analysis_id: str) -> IRGraph:
    evidence = [
        Evidence(
            source=Source.static, confidence=1.0, file="src/App.tsx", loc=Location(line=1, col=1)
        )
    ]
    node = IRNode(
        id="src/App.tsx#App",
        kind=NodeKind.component,
        name="App",
        file="src/App.tsx",
        attrs={},
        evidence=evidence,
    )
    edge = IREdge(
        id="src/App.tsx#App renders src/Button.tsx#Button",
        from_="src/App.tsx#App",
        to="src/Button.tsx#Button",
        kind=EdgeKind.renders,
        evidence=evidence,
    )
    return IRGraph(version="1.1", analysisId=analysis_id, nodes=[node], edges=[edge])


class FakeWorkerRunner:
    def __init__(
        self,
        mode: str = "success",
        error_code: str = "analysis_failed",
        error_message: str = "模拟的分析失败",
    ) -> None:
        self.mode = mode
        self.error_code = error_code
        self.error_message = error_message
        self.requests: list[WorkerRequest] = []
        self.timeouts: list[float] = []

    async def run(self, request: WorkerRequest, timeout_seconds: float) -> WorkerOutcome:
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)

        if self.mode == "failure":
            return WorkerOutcome(
                ok=False, error_code=self.error_code, error_message=self.error_message
            )

        if self.mode == "hang":
            try:
                await asyncio.wait_for(asyncio.sleep(_HANG_SECONDS), timeout=timeout_seconds)
            except TimeoutError:
                return WorkerOutcome(
                    ok=False,
                    error_code="worker_timeout",
                    error_message=f"ast-worker 超过 {timeout_seconds:.0f}s 未返回，已被终止",
                )
            return WorkerOutcome(ok=True, graph=_mini_graph(request.analysis_id))

        return WorkerOutcome(ok=True, graph=_mini_graph(request.analysis_id))


def mini_trace(trace_id: str, project_id: str, analysis_id: str | None = None) -> RuntimeTrace:
    """最小可用的采集产物：App 组件带 debug-stack 位点，与 _mini_graph 的节点对应。"""
    return RuntimeTrace(
        schemaVersion="1.0",
        traceId=trace_id,
        projectId=project_id,
        analysisId=analysis_id,
        mode="passive",
        componentTree=[
            ComponentNode(
                name="App",
                file="src/App.tsx",
                line=23,
                col=21,
                source="debug-stack",
            )
        ],
        page=PageInfo(url="http://localhost:5199/"),
    )


class FakeRuntimeWorkerRunner:
    """运行时采集的假 Worker：不启动浏览器，用于编排层的单元测试。"""

    def __init__(
        self,
        mode: str = "success",
        error_code: str = "browser_unavailable",
        error_message: str = "模拟的采集失败",
    ) -> None:
        self.mode = mode
        self.error_code = error_code
        self.error_message = error_message
        self.requests: list[RuntimeWorkerRequest] = []
        self.timeouts: list[float] = []

    async def run(
        self, request: RuntimeWorkerRequest, timeout_seconds: float
    ) -> RuntimeWorkerOutcome:
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)

        if self.mode == "failure":
            return RuntimeWorkerOutcome(
                ok=False, error_code=self.error_code, error_message=self.error_message
            )

        if self.mode == "hang":
            try:
                await asyncio.wait_for(asyncio.sleep(_HANG_SECONDS), timeout=timeout_seconds)
            except TimeoutError:
                return RuntimeWorkerOutcome(
                    ok=False,
                    error_code="worker_timeout",
                    error_message=f"runtime-worker 超过 {timeout_seconds:.0f}s 未返回，已被终止",
                )

        return RuntimeWorkerOutcome(
            ok=True,
            trace=mini_trace(request.trace_id, request.project_id, request.analysis_id),
        )


def _mini_elements() -> list[dict[str, object]]:
    """与 worker discover 输出对齐的最小元素集：文本框 + 按钮 + 链接。"""
    return [
        {
            "index": 0,
            "tag": "input",
            "role": "textbox",
            "name": "搜索关键词",
            "type": "text",
            "visible": True,
            "selector": {"strategy": "role", "value": "textbox", "name": "搜索关键词"},
        },
        {
            "index": 1,
            "tag": "button",
            "role": "button",
            "name": "查询",
            "visible": True,
            "selector": {"strategy": "role", "value": "button", "name": "查询"},
        },
        {
            "index": 2,
            "tag": "a",
            "role": "link",
            "name": "登录",
            "href": "http://localhost:5199/login",
            "visible": True,
            "selector": {"strategy": "role", "value": "link", "name": "登录"},
        },
    ]


class FakeExploreWorkerRunner:
    """探索 Worker 的假实现：discover / explore 都返回脚本化的成功或失败。"""

    def __init__(
        self,
        mode: str = "success",
        error_code: str = "browser_unavailable",
        error_message: str = "模拟的探索失败",
    ) -> None:
        self.mode = mode
        self.error_code = error_code
        self.error_message = error_message
        self.discover_requests: list[DiscoverWorkerRequest] = []
        self.explore_requests: list[ExploreWorkerRequest] = []
        self.timeouts: list[float] = []

    async def discover(
        self, request: DiscoverWorkerRequest, timeout_seconds: float
    ) -> DiscoverOutcome:
        self.discover_requests.append(request)
        self.timeouts.append(timeout_seconds)
        if self.mode == "failure":
            return DiscoverOutcome(
                ok=False,
                error_code=self.error_code,
                error_message=self.error_message,
            )
        return DiscoverOutcome(
            ok=True,
            page_url="http://localhost:5199/",
            title="订单平台",
            elements=[
                DiscoveredElement.model_validate(el) for el in _mini_elements()
            ],
        )

    async def explore(
        self, request: ExploreWorkerRequest, timeout_seconds: float
    ) -> ExploreOutcome:
        self.explore_requests.append(request)
        self.timeouts.append(timeout_seconds)
        if self.mode == "failure":
            return ExploreOutcome(
                ok=False,
                error_code=self.error_code,
                error_message=self.error_message,
            )
        results: list[ExploreStepResult] = []
        screenshots: list[Screenshot] = []
        for position, step in enumerate(request.steps):
            results.append(
                ExploreStepResult(
                    index=step.index,
                    action=Action(step.action),
                    target=str(step.selector or step.action),
                    result=Result.success,
                    url="http://localhost:5199/",
                    pageTransitioned=False,
                )
            )
            file_name = f"step-{position:03d}.png"
            screenshots.append(Screenshot(stepIndex=step.index, fileName=file_name))
        return ExploreOutcome(
            ok=True,
            page_url="http://localhost:5199/",
            title="订单平台",
            results=results,
            screenshots=screenshots,
        )
