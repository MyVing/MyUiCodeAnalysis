"""测试用假 Worker：不启动真实子进程，用于编排层的单元测试。

timeout 语义与真实 SubprocessWorkerRunner 对齐：
run() 自身负责超时判定，超时返回 worker_timeout 结果而不是抛异常。
"""

import asyncio

from app.analysis.runner import WorkerOutcome, WorkerRequest
from app.contracts import (
    ComponentNode,
    EdgeKind,
    Evidence,
    IREdge,
    IRGraph,
    IRNode,
    Location,
    NodeKind,
    PageInfo,
    RuntimeTrace,
    Source,
)
from app.runtime.runner import RuntimeWorkerOutcome, RuntimeWorkerRequest

_HANG_SECONDS = 5.0


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
