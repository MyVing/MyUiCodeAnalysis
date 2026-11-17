"""真实 runtime-worker 集成测试（APP_RUNTIME_INTEGRATION=1 门控）。

前置条件：
- 仓库根执行过 `npm run build -w @uicodeanalysis/runtime-worker`（dist/index.js 存在）；
- 本机 PATH 上有 node 与 npm；
- tests/fixtures/runtime-demo-app 已安装依赖（node_modules 存在）。

覆盖两条采集路径：
- url：测试自己启动 vite（strictPort 5199），Worker 只负责打开页面采集；
- command：Worker 托管启动并停止 dev server（生产主路径），trace 记录 devServer。

任务编排层（状态机/存储/合并）由 test_runtime.py 的 Fake 覆盖；本文件只回答
「真实 worker 在真实浏览器里能不能采到东西」。
"""

import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from app.contracts import ComponentNode, Evidence, IRGraph, IRNode, NodeKind
from app.runtime.merge import merge_static_with_runtime
from app.runtime.runner import RuntimeWorkerRequest, SubprocessRuntimeRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_APP = REPO_ROOT / "tests" / "fixtures" / "runtime-demo-app"
WORKER_ENTRY = REPO_ROOT / "workers" / "runtime-worker" / "dist" / "index.js"
DEMO_PORT = 5199
COLLECT_TIMEOUT_SECONDS = 120.0

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("APP_RUNTIME_INTEGRATION"),
        reason="真实采集集成测试需设置 APP_RUNTIME_INTEGRATION=1",
    ),
    pytest.mark.skipif(
        not WORKER_ENTRY.is_file(),
        reason="runtime-worker 未构建（先执行 npm run build -w @uicodeanalysis/runtime-worker）",
    ),
    pytest.mark.skipif(
        not (DEMO_APP / "node_modules").is_dir(),
        reason="runtime-demo-app 未安装依赖",
    ),
]


def _port_open(port: int, timeout_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _port_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture
def demo_url() -> str:
    """启动 fixture 应用的 vite dev server，采集结束后杀掉整棵进程树。"""
    proc = subprocess.Popen(  # noqa: S603 - npm 需经 shell 解析（npm.cmd）
        "npm run dev",
        cwd=DEMO_APP,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _port_open(DEMO_PORT), f"vite dev server 未在 {DEMO_PORT} 端口就绪"
        yield f"http://localhost:{DEMO_PORT}/"
    finally:
        subprocess.run(  # noqa: S603
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
        )


def _runner() -> SubprocessRuntimeRunner:
    return SubprocessRuntimeRunner(node_command="node", entry_path=str(WORKER_ENTRY))


def _flatten(tree: list[ComponentNode]) -> list[ComponentNode]:
    flattened: list[ComponentNode] = []
    for node in tree:
        flattened.append(node)
        flattened.extend(_flatten(node.children or []))
    return flattened


def test_collect_via_url(demo_url: str) -> None:
    outcome = asyncio.run(
        _runner().run(
            RuntimeWorkerRequest(
                project_id="prj_it",
                root_path=str(DEMO_APP),
                trace_id="trace_it1",
                url=demo_url,
            ),
            COLLECT_TIMEOUT_SECONDS,
        )
    )

    assert outcome.ok, f"采集失败：{outcome.error_code} {outcome.error_message}"
    trace = outcome.trace
    assert trace is not None
    assert trace.page.url.rstrip("/") == demo_url.rstrip("/")
    components = _flatten(trace.componentTree or [])
    names = {component.name for component in components}
    # fixture 应用含 App 与 UserList 组件
    assert "App" in names
    assert all(
        component.source in ("debug-stack", "debug-source", "name-only")
        for component in components
        if component.source is not None
    )


def test_collect_via_command() -> None:
    outcome = asyncio.run(
        _runner().run(
            RuntimeWorkerRequest(
                project_id="prj_it",
                root_path=str(DEMO_APP),
                trace_id="trace_it2",
                command="npm run dev",
            ),
            COLLECT_TIMEOUT_SECONDS,
        )
    )

    assert outcome.ok, f"采集失败：{outcome.error_code} {outcome.error_message}"
    trace = outcome.trace
    assert trace is not None
    # 托管路径必须记录 dev server 事实（端口以运行时探测为准）
    assert trace.devServer is not None
    assert trace.devServer.port == DEMO_PORT
    components = _flatten(trace.componentTree or [])
    assert any(component.name == "UserList" for component in components)
    # dev server 已被 Worker 停止：进程退出后端口应释放
    time.sleep(1.0)
    assert _port_closed(DEMO_PORT)


def test_merge_with_real_trace(demo_url: str) -> None:
    outcome = asyncio.run(
        _runner().run(
            RuntimeWorkerRequest(
                project_id="prj_it",
                root_path=str(DEMO_APP),
                trace_id="trace_it3",
                url=demo_url,
            ),
            COLLECT_TIMEOUT_SECONDS,
        )
    )
    assert outcome.ok and outcome.trace is not None

    static = IRGraph(
        version="1.1",
        analysisId="an_it",
        nodes=[
            IRNode(
                id="src/App.tsx#App",
                kind=NodeKind.component,
                name="App",
                file="src/App.tsx",
                attrs={},
                evidence=[Evidence(source="static", confidence=1.0, file="src/App.tsx")],
            ),
            IRNode(
                id="src/App.tsx#UserList",
                kind=NodeKind.component,
                name="UserList",
                file="src/App.tsx",
                attrs={},
                evidence=[Evidence(source="static", confidence=1.0, file="src/App.tsx")],
            ),
        ],
        edges=[],
    )

    merged = merge_static_with_runtime(static, outcome.trace)

    runtime_hits = [
        evidence
        for node in merged.nodes
        for evidence in node.evidence
        if evidence.source == "runtime"
    ]
    # 真实 React≥19 采集：至少一个组件通过 file+name / 仅 file / 仅 name 对上静态节点
    assert len(runtime_hits) >= 1
    assert all(evidence.confidence in (0.7, 0.8, 0.9) for evidence in runtime_hits)
