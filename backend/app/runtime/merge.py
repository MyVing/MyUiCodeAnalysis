"""静态图 + 运行时 trace 的合并：纯函数，不触碰存储。

合并的增量是「运行时证据」，不是新节点：
- 运行时组件树来自 React fiber，与静态图节点按「文件 + 组件名」对上后，
  给对应 IRNode 追加一条 source=runtime 的 Evidence；
- 对不上号的运行时组件如实记 info 告警，不编造节点与边——与静态分析的
  「告警而非静默降级」同一立场。

置信度反映「结论有多确定」：
- 文件 + 组件名双证 0.9（fiber 给出定义/创建位点，且名字吻合）
- 仅文件 0.8（位点吻合但名字不同：包装渲染、HOC 改名等情形）
- 仅组件名 0.7（React≥19 的 name-only 降级链；项目内重名组件不匹配）
"""

from datetime import UTC, datetime

from app.contracts import (
    ComponentNode,
    Evidence,
    IRGraph,
    IRNode,
    Location,
    RuntimeTrace,
    Source,
    Stage,
    Warning,
)

CONFIDENCE_FILE_AND_NAME = 0.9
CONFIDENCE_FILE_ONLY = 0.8
CONFIDENCE_NAME_ONLY = 0.7

_COMPONENT_KINDS = ("component", "server_component")


def _normalize_file(file: str) -> str:
    """统一分隔符并去掉 ./ 前缀；fiber 位点与 ast-worker 产物要能对上。"""
    normalized = file.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def flatten_component_tree(tree: list[ComponentNode]) -> list[ComponentNode]:
    """深度优先摊平组件挂载树；同一组件挂载多次会重复出现，如实保留。"""
    flattened: list[ComponentNode] = []
    for node in tree:
        flattened.append(node)
        if node.children:
            flattened.extend(flatten_component_tree(node.children))
    return flattened


def _evidence_key(evidence: Evidence) -> tuple[object, ...]:
    """去重键；Location 不是可哈希类型，转成 (line, col) 元组。"""
    loc = (evidence.loc.line, evidence.loc.col) if evidence.loc is not None else None
    return (evidence.file, loc, evidence.confidence, evidence.traceId)


def _match_node(
    component: ComponentNode, static_nodes: list[IRNode]
) -> tuple[IRNode | None, float | None]:
    name = component.name
    file = _normalize_file(component.file) if component.file else None

    if file is not None:
        same_file = [
            node for node in static_nodes if node.file and _normalize_file(node.file) == file
        ]
        named = [node for node in same_file if node.name == name]
        if named:
            return named[0], CONFIDENCE_FILE_AND_NAME
        if len(same_file) == 1:
            return same_file[0], CONFIDENCE_FILE_ONLY
        if len(same_file) > 1:
            # 位点对上但名字都对不上，且该文件有多个组件：归属不明确，不硬选
            return None, None

    by_name = [node for node in static_nodes if node.name == name]
    if len(by_name) == 1:
        return by_name[0], CONFIDENCE_NAME_ONLY
    return None, None


def merge_static_with_runtime(static: IRGraph, trace: RuntimeTrace) -> IRGraph:
    """返回新的 IRGraph：不修改入参；运行时证据追加到匹配节点的 evidence 列表。"""
    component_nodes = [
        node
        for node in static.nodes
        if node.kind in _COMPONENT_KINDS and node.file is not None
    ]

    warnings: list[Warning] = list(static.warnings or [])
    tree = trace.componentTree or []
    occurrences = flatten_component_tree(tree)

    # 同一节点可能被多次挂载命中：先按节点聚合证据，再一次性重建
    matched: dict[str, list[Evidence]] = {}
    unmatched: list[ComponentNode] = []
    for component in occurrences:
        node, confidence = _match_node(component, component_nodes)
        if node is None or confidence is None:
            unmatched.append(component)
            continue
        evidence = Evidence(
            source=Source.runtime,
            confidence=confidence,
            file=_normalize_file(component.file) if component.file else None,
            loc=Location(line=component.line, col=component.col)
            if component.line is not None and component.col is not None
            else None,
            traceId=trace.traceId,
        )
        bucket = matched.setdefault(node.id, [])
        # 同一节点在同一 trace 的同一位点只留一条证据
        key = _evidence_key(evidence)
        existing_keys = {_evidence_key(item) for item in bucket}
        if key not in existing_keys:
            bucket.append(evidence)

    if not tree:
        warnings.append(
            Warning(
                code="runtime_no_component_tree",
                severity="info",
                message="本次采集未产出组件树（可能未启用 fiber 采集），合并图与静态图一致。",
                stage=Stage.runtime,
            )
        )
    for component in unmatched:
        where = f"（{component.file}:{component.line}）" if component.file else ""
        warnings.append(
            Warning(
                code="runtime_component_unmatched",
                severity="info",
                message=f"运行时组件 {component.name}{where} 未能在静态图中找到对应节点。",
                stage=Stage.runtime,
            )
        )

    merged_nodes: list[IRNode] = []
    for node in static.nodes:
        extra = matched.get(node.id)
        if not extra:
            merged_nodes.append(node)
            continue
        existing_keys = {_evidence_key(item) for item in node.evidence}
        additions = [
            item for item in extra if _evidence_key(item) not in existing_keys
        ]
        merged_nodes.append(
            node.model_copy(update={"evidence": [*node.evidence, *additions]})
        )

    meta = static.meta.model_copy(deep=True) if static.meta is not None else None
    if meta is not None:
        meta.generatedAt = datetime.now(UTC)

    return IRGraph(
        version=static.version,
        analysisId=static.analysisId,
        nodes=merged_nodes,
        edges=list(static.edges),
        warnings=warnings,
        meta=meta,
    )
