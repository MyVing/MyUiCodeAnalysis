"""接口 → 页面反向检索（技术设计 6.11）：回答「这个接口在哪个页面被调用」。

需求文档、PRD、接口文档里最稳定的信息就是 method + path。与其让模型猜哪个
页面调了 `/api/orders`，不如直接沿图里的 `requests` 边反向走——这一步零模型、
全确定性，结论的每一环都是图上真实存在的边。

三条规则贯穿始终：

1. **匹配靠归一化**：需求里写的 URL 与图里记的 URL 必须算成同一个才叫命中。
   归一化规则与 ast-worker 的 `normalizeUrlText` 保持同一套（L1 剥离
   origin / query / hash，L2 参数段统一成 `:param`），两边不共享代码，但
   测试锁定行为一致；`GET /api/orders` 与 `POST /api/orders` 永远是两个接口。
2. **置信度沿路径取最小值，再乘锚点系数**：精确匹配（method + path 都对上）
   锚点 0.95；只有 URL 后缀对上（method 未提供或走 match=suffix）锚点 0.85，
   并在结果里如实标注 `urlSuffixMatch`。静态「语法事实恒为 1.0」的说法不适用
   于跨节点路径——每多一跳就多一分符号推断的成分。
3. **歧义显式告警，不自动聚焦**：同一接口被多个页面调用是常态，返回全部
   页面按置信度排序；超过 3 个页面时记 `api_usage_ambiguous`，由调用方
   （人或需求里同时出现的页面词）做二次过滤。

反向遍历的边方向与深度上限（含实现扩展 `uses`：hook 的请求归属修复后，
`组件 --uses--> hook --requests--> api` 是真实链路，不走 `uses` 就漏页）：

| 目标 | 反向使用的边 | 原始方向 | 深度上限 |
| --- | --- | --- | --- |
| api → 调用者 | `requests` | caller → api | 1 |
| 函数 → 上层调用者 | `calls` | caller → callee | 3 |
| 元素 → 所属组件 | `contains` | parent → child | 2 |
| 子组件 → 父组件 | `renders` | parent → child | 3 |
| hook → 使用者 | `uses` | user → hook | 2 |
| 页面 → 路由 | `renders` | route → page | 1（终跳，不占上面预算） |
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Literal

from app.contracts import EdgeKind, Evidence, IREdge, IRGraph, IRNode, NodeKind, RuntimeTrace
from app.contracts.requirement import RequirementWarning, Severity

MatchMode = Literal["exact", "suffix"]

# 精确锚点：method + path 都对上（6.11 置信度规则第 1 条）
_ANCHOR_EXACT = 0.95
# 宽松锚点：URL 后缀对上（method 未提供，或前缀无法确定）
_ANCHOR_SUFFIX = 0.85
# 运行时双证（trace 的请求 URL 与页面 URL 同时出现）：与精确锚点同档，但带
# runtimeBacked 标记——它比任何静态推断都可信
_ANCHOR_RUNTIME = 0.95

# 同一接口被超过这么多页面调用时记 api_usage_ambiguous
AMBIGUOUS_PAGE_LIMIT = 3

# UUID（8-4-4-4-4 十六进制）与纯数字段一样，都是「具体资源实例」而非接口身份
_UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
# 参数段的各种写法：`${…}`（模板骨架占位）、`:id`、`{id}`
_PLACEHOLDER_PATTERNS = (
    re.compile(r"^\$\{[^}]*\}$"),
    re.compile(r"^:.+$"),
    re.compile(r"^\{[^}]+\}$"),
)
_NUMERIC_SEGMENT = re.compile(r"^\d+$")
_ORIGIN_PREFIX = re.compile(r"^https?://", re.IGNORECASE)

# 每种边在单条路径上最多走几跳
_HOP_BUDGETS: dict[EdgeKind, int] = {
    EdgeKind.requests: 1,
    EdgeKind.calls: 3,
    EdgeKind.contains: 2,
    EdgeKind.renders: 3,
    EdgeKind.uses: 2,
}


def _normalize_segment(segment: str) -> str:
    if any(pattern.match(segment) for pattern in _PLACEHOLDER_PATTERNS):
        return ":param"
    if _NUMERIC_SEGMENT.match(segment):
        return ":param"
    if _UUID_SEGMENT.match(segment):
        return ":param"
    return segment


def normalize_url(text: str) -> str | None:
    """URL 归一化（L1 剥离 + L2 参数化），与 ast-worker `normalizeUrlText` 同规则。

    `https://x.com/api/users/123?page=1#/list` → `/api/users/:param`。
    全部归一化后剩不下路径（只有 origin / query 的 URL）视为无法确定，返回 None。
    """
    compact = "".join(text.split())
    if not compact:
        return None

    rest = compact
    if origin := _ORIGIN_PREFIX.match(rest):
        rest = rest[origin.end() :]
        # origin 后保留路径部分；没有路径（`https://x.com`）就没有接口身份
        slash = rest.find("/")
        rest = "" if slash == -1 else rest[slash:]
    for separator in ("#", "?"):
        index = rest.find(separator)
        if index != -1:
            rest = rest[:index]
    rest = re.sub(r"/{2,}", "/", rest).rstrip("/")
    if not rest:
        return None

    return "/".join(_normalize_segment(segment) for segment in rest.split("/")).lower()


def _edge_confidence(edge_evidence: list[Evidence]) -> float:
    """一条边的置信度取其证据里的最高值：证据列表里静态与推断并存，取最可信那条。"""
    return max((item.confidence for item in edge_evidence), default=0.0)


@dataclass(frozen=True)
class ApiUsageHit:
    """一条「页面 → 接口」的反查结果：路径上每一环都可回图核对。"""

    page_node_id: str
    route_node_id: str | None
    # 完整调用链：api → … → 页面（→ 路由）
    path: tuple[str, ...]
    edge_path: tuple[str, ...]
    confidence: float
    url_suffix_match: bool
    runtime_backed: bool
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class ApiUsageLookup:
    """反查结果：命中页面（按置信度降序）与告警。"""

    pages: tuple[ApiUsageHit, ...]
    warnings: tuple[RequirementWarning, ...]


@dataclass(frozen=True)
class _WalkState:
    """反向 DFS 的路径状态：预算按边类型各计各的，防止一类边把其他类挤掉。"""

    node_id: str
    budgets: tuple[tuple[EdgeKind, int], ...]
    path: tuple[str, ...]
    edge_path: tuple[str, ...]
    min_confidence: float

    def with_edge(self, edge: IREdge) -> _WalkState:
        used = dict(self.budgets)
        used[edge.kind] = used.get(edge.kind, 0) + 1
        return _WalkState(
            node_id=edge.from_,
            budgets=tuple(used.items()),
            path=(*self.path, edge.from_),
            edge_path=(*self.edge_path, edge.id),
            min_confidence=min(self.min_confidence, _edge_confidence(edge.evidence)),
        )


class ApiUsageIndex:
    """一张分析图的反向索引：api 键、入边邻接、页面 → 路由映射。

    每张图构建一次（端点层按 analysisId 缓存），不落库——图更新即失效。
    """

    def __init__(self, graph: IRGraph) -> None:
        nodes = {node.id: node for node in graph.nodes}
        self._nodes = nodes
        # api 匹配键：method + 归一化 url。api 节点的 url 在 ast-worker 侧已归一化，
        # 但示例图与旧产物未必，构建索引时统一再过一遍 normalize_url 兜底。
        self._apis: dict[str, list[IRNode]] = defaultdict(list)
        for node in graph.nodes:
            if node.kind is not NodeKind.api:
                continue
            method = node.attrs.get("method")
            # ast-worker 写 attrs.url；示例图用的是 attrs.path，读取时兼容两种键
            url = node.attrs.get("url") or node.attrs.get("path")
            if not isinstance(method, str) or not isinstance(url, str):
                continue
            normalized = normalize_url(url)
            if normalized is None:
                continue
            self._apis[f"{method.upper()} {normalized}"].append(node)

        self._in_edges: dict[str, list[IREdge]] = defaultdict(list)  # node_id → 指向它的边
        self._edges_by_id: dict[str, IREdge] = {}
        for edge in graph.edges:
            self._in_edges[edge.to].append(edge)
            self._edges_by_id[edge.id] = edge

        # 页面 → 渲染它的 route（同一页面被多条路由挂载时全部保留：反查结果
        # 应该告诉用户「从哪个路由进来能看到这个页面」）
        self._routes_by_page: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for edge in graph.edges:
            if edge.kind is not EdgeKind.renders:
                continue
            source = nodes.get(edge.from_)
            target = nodes.get(edge.to)
            if source is None or source.kind is not NodeKind.route:
                continue
            if target is None or target.kind not in (NodeKind.component, NodeKind.server_component):
                continue
            self._routes_by_page[target.id].append((source.id, edge.id))

    def lookup(self, *, method: str | None, url: str, match: MatchMode) -> ApiUsageLookup:
        """按 method + url 反查页面。method 缺省视为「任意方法」。"""
        normalized = normalize_url(url)
        if normalized is None:
            return ApiUsageLookup(
                pages=(),
                warnings=(
                    RequirementWarning(
                        code="url_unresolvable",
                        severity=Severity.warning,
                        message=f"URL 无法归一化出路径，无法反查：{url}",
                    ),
                ),
            )

        method_key = method.strip().upper() if method and method.strip() else None
        api_nodes = self._match_api_nodes(method_key, normalized, match)
        warnings: list[RequirementWarning] = []
        if not api_nodes:
            message = (
                f"图中没有 method 为 {method_key}、URL 匹配 {normalized} 的接口节点。"
                if method_key
                else f"图中没有 URL 匹配 {normalized} 的接口节点。"
            )
            return ApiUsageLookup(
                pages=(),
                warnings=(
                    RequirementWarning(
                        code="api_not_found",
                        severity=Severity.warning,
                        message=message,
                    ),
                ),
            )

        pages: list[ApiUsageHit] = []
        suffix_match = match == "suffix" or method_key is None
        for api_node in api_nodes:
            pages.extend(self._walk_from_api(api_node, suffix_match))
        pages.sort(key=lambda hit: (-hit.confidence, hit.page_node_id, hit.path))
        pages = _dedupe(pages)

        if not pages:
            # 接口存在但没有页面可达：死接口（只在未挂载的工具函数、废弃组件里出现）。
            # 这条结论比任何影响面分析都直接——它回答「这个接口能不能下线」。
            warnings.append(
                RequirementWarning(
                    code="api_no_page_reachable",
                    severity=Severity.warning,
                    message=(
                        f"接口 {method_key or ''} {normalized} 存在，"
                        "但没有任何页面可达（可能是死接口或仅被未挂载代码调用）。"
                    ),
                )
            )
        elif len({hit.page_node_id for hit in pages}) > AMBIGUOUS_PAGE_LIMIT:
            ambiguous_count = len({hit.page_node_id for hit in pages})
            warnings.append(
                RequirementWarning(
                    code="api_usage_ambiguous",
                    severity=Severity.warning,
                    message=(
                        f"该接口被 {ambiguous_count} 个页面调用，存在歧义；"
                        "请结合需求中的页面词 / 组件名二次过滤，不要取第一个。"
                    ),
                )
            )

        return ApiUsageLookup(pages=tuple(pages), warnings=tuple(warnings))

    def route_path_of(self, route_node_id: str | None) -> str | None:
        """route 节点声明的路径（attrs.path）；运行时双证用它和 trace 页面 URL 对齐。"""
        if route_node_id is None:
            return None
        node = self._nodes.get(route_node_id)
        path = node.attrs.get("path") if node is not None else None
        return path if isinstance(path, str) else None

    def _match_api_nodes(
        self,
        method_key: str | None,
        normalized: str,
        match: MatchMode,
    ) -> list[IRNode]:
        if match == "exact" and method_key is not None:
            return list(self._apis.get(f"{method_key} {normalized}", []))

        # 宽松匹配：后缀对段边界（/orders 是 /api/orders 的后缀，但 /ders 不是）。
        # method 未提供时所有方法都参与，但 GET /a 与 POST /a 仍视为不同接口节点，
        # 只是不再按 method 预筛。
        hits: list[IRNode] = []
        for key, nodes in self._apis.items():
            node_url = key.split(" ", 1)[1]
            if node_url == normalized or _is_suffix_match(node_url, normalized):
                if method_key is not None and not key.startswith(f"{method_key} "):
                    continue
                hits.extend(nodes)
        return hits

    def _walk_from_api(self, api_node: IRNode, suffix_match: bool) -> list[ApiUsageHit]:
        """从 api 节点反向走整张图，收集所有可达页面的命中。"""
        anchor = _ANCHOR_SUFFIX if suffix_match else _ANCHOR_EXACT
        hits: list[ApiUsageHit] = []
        start = _WalkState(
            node_id=api_node.id,
            budgets=(),
            path=(api_node.id,),
            edge_path=(),
            min_confidence=1.0,
        )
        stack: list[_WalkState] = [start]
        while stack:
            state = stack.pop()
            for route_id, route_edge_id in self._routes_by_page.get(state.node_id, []):
                edge = self._edge_by_id(route_edge_id)
                if edge is None:
                    continue
                confidence = round(
                    min(state.min_confidence, _edge_confidence(edge.evidence)) * anchor, 4
                )
                hits.append(
                    ApiUsageHit(
                        page_node_id=state.node_id,
                        route_node_id=route_id,
                        path=(*state.path, route_id),
                        edge_path=(*state.edge_path, route_edge_id),
                        confidence=confidence,
                        url_suffix_match=suffix_match,
                        runtime_backed=False,
                        evidence=self._path_evidence(state.edge_path),
                    )
                )
            # 页面本身不再向上展开：layout 复用、父级传递都会把「谁在用」冲淡成
            # 「谁可能沾边」。反查的答案到 route 为止。
            if state.node_id in self._routes_by_page:
                continue
            for edge in self._in_edges.get(state.node_id, []):
                budget = _HOP_BUDGETS.get(edge.kind)
                if budget is None:
                    continue
                used = dict(state.budgets)
                if used.get(edge.kind, 0) >= budget:
                    continue
                if edge.from_ in state.path:
                    continue  # 环路：图上不该有，防御性跳过
                stack.append(state.with_edge(edge))
        return hits

    def _edge_by_id(self, edge_id: str) -> IREdge | None:
        return self._edges_by_id.get(edge_id)

    def _path_evidence(self, edge_path: tuple[str, ...]) -> tuple[Evidence, ...]:
        evidence: list[Evidence] = []
        for edge_id in edge_path:
            edge = self._edge_by_id(edge_id)
            if edge is not None:
                evidence.extend(edge.evidence)
        return tuple(evidence)


def _is_suffix_match(node_url: str, query_url: str) -> bool:
    """query 是否为 node 的段对齐后缀：/orders 匹配 /api/orders，不匹配 /myorders。"""
    if len(query_url) <= len(node_url):
        return False
    if not query_url.endswith(node_url):
        return False
    boundary = query_url[len(query_url) - len(node_url) - 1]
    return boundary == "/"


def _dedupe(pages: list[ApiUsageHit]) -> list[ApiUsageHit]:
    """同一 (页面, 路由) 只留一条：置信度最高者优先，平手取路径更短的。"""
    best: dict[tuple[str, str | None], ApiUsageHit] = {}
    for hit in pages:
        key = (hit.page_node_id, hit.route_node_id)
        current = best.get(key)
        if (
            current is None
            or hit.confidence > current.confidence
            or (hit.confidence == current.confidence and len(hit.path) < len(current.path))
        ):
            best[key] = hit
    ranked = sorted(
        best.values(),
        key=lambda hit: (-hit.confidence, hit.page_node_id, hit.route_node_id or ""),
    )
    return ranked


def apply_runtime_backing(
    pages: tuple[ApiUsageHit, ...],
    route_paths: dict[str, str | None],
    *,
    method: str | None,
    url: str,
    trace: RuntimeTrace | None,
) -> tuple[ApiUsageHit, ...]:
    """运行时双证强化：trace 的请求 URL 与页面 URL 同时出现 → 该页面 0.95 + runtimeBacked。

    静态分析定不下来的 baseURL、运行时才确定的 env 值，trace 都能补上：这是一条
    独立于静态路径的证据，不改变路径本身，只把置信度抬到运行时档并如实标记。

    `route_paths` 是 pageNodeId → 路由路径（route 节点 attrs.path，可能为 None）。
    页面匹配用归一化后的路径段比较：trace.page.route（路由模板）优先，其次
    trace.page.url——数字段归一化后与 `:param` 自然对齐。
    """
    if trace is None or not pages:
        return pages

    query = normalize_url(url)
    if query is None:
        return pages
    method_key = method.strip().upper() if method and method.strip() else None
    request_seen = any(
        (method_key is None or (event.method or "").upper() == method_key)
        and normalize_url(event.url) == query
        for event in trace.networkEvents or ()
    )
    if not request_seen:
        return pages

    page_identity = trace.page.route or trace.page.url
    page_path = normalize_url(page_identity) if page_identity else None
    if page_path is None:
        return pages

    backed = {
        page_id
        for page_id, route_path in route_paths.items()
        if route_path and normalize_url(route_path) == page_path
    }
    if not backed:
        return pages

    return tuple(
        replace(
            hit,
            runtime_backed=True,
            confidence=max(hit.confidence, _ANCHOR_RUNTIME),
        )
        if hit.page_node_id in backed
        else hit
        for hit in pages
    )
