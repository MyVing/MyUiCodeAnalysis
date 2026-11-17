"""合并纯函数的单元测试：置信度分档、证据去重、告警而非编造。"""

from app.contracts import (
    ComponentNode,
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
from app.runtime.merge import (
    CONFIDENCE_FILE_AND_NAME,
    CONFIDENCE_FILE_ONLY,
    CONFIDENCE_NAME_ONLY,
    flatten_component_tree,
    merge_static_with_runtime,
)


def _static_node(
    node_id: str, name: str, file: str, kind: NodeKind = NodeKind.component
) -> IRNode:
    return IRNode(
        id=node_id,
        kind=kind,
        name=name,
        file=file,
        attrs={},
        evidence=[
            Evidence(
                source=Source.static, confidence=1.0, file=file, loc=Location(line=1, col=1)
            )
        ],
    )


def _static_graph(nodes: list[IRNode]) -> IRGraph:
    return IRGraph(version="1.1", analysisId="an_1", nodes=nodes, edges=[])


def _trace(tree: list[ComponentNode], trace_id: str = "trace_1") -> RuntimeTrace:
    return RuntimeTrace(
        schemaVersion="1.0",
        traceId=trace_id,
        projectId="prj_1",
        mode="passive",
        componentTree=tree,
        page=PageInfo(url="http://localhost:5199/"),
    )


def test_flatten_tree_is_depth_first() -> None:
    tree = [
        ComponentNode(
            name="A", children=[ComponentNode(name="B", children=[ComponentNode(name="C")])]
        )
    ]
    assert [node.name for node in flatten_component_tree(tree)] == ["A", "B", "C"]


def test_file_and_name_match_adds_runtime_evidence() -> None:
    graph = _static_graph([_static_node("src/App.tsx#App", "App", "src/App.tsx")])
    trace = _trace(
        [ComponentNode(name="App", file="src\\App.tsx", line=23, col=21, source="debug-stack")]
    )

    merged = merge_static_with_runtime(graph, trace)

    node = merged.nodes[0]
    assert len(node.evidence) == 2
    runtime_evidence = node.evidence[-1]
    assert runtime_evidence.source == Source.runtime
    assert runtime_evidence.confidence == CONFIDENCE_FILE_AND_NAME
    assert runtime_evidence.file == "src/App.tsx"
    assert runtime_evidence.traceId == "trace_1"
    assert runtime_evidence.loc is not None and runtime_evidence.loc.line == 23
    # 静态图本体不被修改
    assert len(graph.nodes[0].evidence) == 1


def test_file_only_match_lower_confidence() -> None:
    graph = _static_graph([_static_node("src/Card.tsx#Card", "Card", "src/Card.tsx")])
    trace = _trace(
        [ComponentNode(name="MemoCard", file="src/Card.tsx", line=3, col=1, source="debug-stack")]
    )

    merged = merge_static_with_runtime(graph, trace)

    runtime_evidence = merged.nodes[0].evidence[-1]
    assert runtime_evidence.confidence == CONFIDENCE_FILE_ONLY


def test_name_only_match_lowest_confidence() -> None:
    graph = _static_graph([_static_node("src/App.tsx#App", "App", "src/App.tsx")])
    # React≥19 name-only：没有位点，仅组件名
    trace = _trace([ComponentNode(name="App", source="name-only")])

    merged = merge_static_with_runtime(graph, trace)

    runtime_evidence = merged.nodes[0].evidence[-1]
    assert runtime_evidence.confidence == CONFIDENCE_NAME_ONLY
    assert runtime_evidence.file is None
    assert runtime_evidence.loc is None


def test_ambiguous_name_does_not_match() -> None:
    # 项目内两个同名组件：仅凭名字归属不明确，不硬选
    graph = _static_graph(
        [
            _static_node("src/a/Table.tsx#Table", "Table", "src/a/Table.tsx"),
            _static_node("src/b/Table.tsx#Table", "Table", "src/b/Table.tsx"),
        ]
    )
    trace = _trace([ComponentNode(name="Table", source="name-only")])

    merged = merge_static_with_runtime(graph, trace)

    for node in merged.nodes:
        assert len(node.evidence) == 1
    assert any(w.code == "runtime_component_unmatched" for w in (merged.warnings or []))


def test_unmatched_runtime_component_produces_warning() -> None:
    graph = _static_graph([_static_node("src/App.tsx#App", "App", "src/App.tsx")])
    trace = _trace([ComponentNode(name="Ghost", file="src/missing.tsx", line=1, col=1)])

    merged = merge_static_with_runtime(graph, trace)

    assert merged.nodes[0].evidence == graph.nodes[0].evidence
    unmatched = [w for w in (merged.warnings or []) if w.code == "runtime_component_unmatched"]
    assert len(unmatched) == 1
    assert "Ghost" in unmatched[0].message


def test_no_component_tree_records_warning() -> None:
    graph = _static_graph([_static_node("src/App.tsx#App", "App", "src/App.tsx")])
    trace = _trace([])

    merged = merge_static_with_runtime(graph, trace)

    assert any(w.code == "runtime_no_component_tree" for w in (merged.warnings or []))


def test_merged_graph_keeps_identity_and_edges() -> None:
    edge = IREdge(
        id="e1",
        from_="src/App.tsx#App",
        to="src/Button.tsx#Button",
        kind="renders",
        evidence=[
            Evidence(source=Source.static, confidence=1.0, file="src/App.tsx")
        ],
    )
    graph = _static_graph(
        [
            _static_node("src/App.tsx#App", "App", "src/App.tsx"),
            _static_node("src/Button.tsx#Button", "Button", "src/Button.tsx"),
        ]
    )
    graph = graph.model_copy(update={"edges": [edge]})
    trace = _trace([ComponentNode(name="App", file="src/App.tsx", line=1, col=1)])

    merged = merge_static_with_runtime(graph, trace)

    assert merged.analysisId == graph.analysisId
    assert merged.version == graph.version
    assert len(merged.edges) == 1 and merged.edges[0].id == "e1"


def test_duplicate_occurrence_dedupes_evidence() -> None:
    # 同一组件挂载两次（列表里渲染）：证据只落一条
    graph = _static_graph([_static_node("src/Row.tsx#Row", "Row", "src/Row.tsx")])
    occurrence = ComponentNode(name="Row", file="src/Row.tsx", line=5, col=3)
    trace = _trace([occurrence, occurrence.model_copy()])

    merged = merge_static_with_runtime(graph, trace)

    runtime_evidence = [e for e in merged.nodes[0].evidence if e.source == Source.runtime]
    assert len(runtime_evidence) == 1
