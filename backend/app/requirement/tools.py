"""只读需求分析工具：检索项目页面/交互，并读取有边界的源码声明。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.contracts import IRGraph, IRNode, NodeKind
from app.core.tracing import log_event
from app.requirement.chains import ActionChain, derive_chains
from app.requirement.features import decompose
from app.requirement.llm import LLMCallError
from app.requirement.recall import page_nodes, recall

MAX_TOOL_RESULTS = 5
MAX_SOURCE_LINES = 120
MAX_SOURCE_CHARS = 20_000

logger = logging.getLogger(__name__)


class RequirementToolError(LLMCallError):
    """需求工具请求无效或越权。"""


REQUIREMENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_pages",
            "description": "按描述搜索当前项目中的相关页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_RESULTS},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_interactions",
            "description": "在指定页面搜索与描述相关的真实交互链路。",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "minLength": 1},
                    "query": {"type": "string", "minLength": 1},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_RESULTS},
                },
                "required": ["page_id", "query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_interaction_chain",
            "description": "读取当前项目图中真实存在的完整交互链路。",
            "parameters": {
                "type": "object",
                "properties": {"chain_id": {"type": "string", "minLength": 1}},
                "required": ["chain_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_source_range",
            "description": "读取指定 IR 源码节点的声明范围；仅支持具有可信 endLine 的节点。",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string", "minLength": 1}},
                "required": ["node_id"],
                "additionalProperties": False,
            },
        },
    },
]


def _rank(text: str, query: str) -> int:
    normalized = query.strip().lower()
    if not normalized:
        return 0
    haystack = text.lower()
    if normalized in haystack:
        return len(normalized) * 10
    return sum(3 for token in normalized.split() if token and token in haystack)


class RequirementAnalysisTools:
    def __init__(self, project_root: str, graph: IRGraph) -> None:
        self._root = Path(project_root).resolve()
        self._graph = graph
        self._nodes = {node.id: node for node in graph.nodes}
        self._pages, _, _ = page_nodes(graph)
        self._chains = derive_chains(graph).by_id

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(arguments, dict):
                raise RequirementToolError(
                    "工具参数必须是 JSON 对象", code="invalid_tool_arguments"
                )
            if name == "search_pages":
                result = self._search_pages(arguments)
            elif name == "search_interactions":
                result = self._search_interactions(arguments)
            elif name == "get_interaction_chain":
                result = self._get_chain(arguments)
            elif name == "read_source_range":
                result = self._read_source(arguments)
            else:
                raise RequirementToolError("未知的需求分析工具", code="unknown_tool")
        except RequirementToolError as error:
            log_event(
                logger,
                logging.DEBUG,
                "requirement_tool_error",
                tool=name,
                code=error.code,
            )
            return {"status": "error", "code": error.code, "message": error.message}
        log_event(
            logger,
            logging.DEBUG,
            "requirement_tool_done",
            tool=name,
            status=result.get("status") if isinstance(result, dict) else None,
        )
        return result

    @staticmethod
    def _keys(arguments: dict[str, Any], required: set[str], allowed: set[str]) -> None:
        if not required.issubset(arguments) or set(arguments) - allowed:
            raise RequirementToolError("工具参数不符合定义", code="invalid_tool_arguments")

    def _search_pages(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._keys(arguments, {"query"}, {"query", "top_k"})
        query, top_k = arguments.get("query"), arguments.get("top_k", MAX_TOOL_RESULTS)
        if (
            not isinstance(query, str)
            or not query.strip()
            or type(top_k) is not int
            or not 1 <= top_k <= MAX_TOOL_RESULTS
        ):
            raise RequirementToolError("页面搜索参数无效", code="invalid_tool_arguments")
        by_id = {}
        for feature in decompose(query):
            for detail in recall(self._graph, feature.terms, top_k=top_k).candidates:
                by_id[detail.candidate.nodeId] = detail
        results = sorted(
            by_id.values(), key=lambda item: (-item.candidate.score, item.candidate.nodeId)
        )[:top_k]
        return {
            "status": "ok",
            "pages": [
                {
                    "page_id": item.candidate.nodeId,
                    "name": item.candidate.name,
                    "file": item.candidate.file,
                    "route_id": item.candidate.routeId,
                    "score": item.candidate.score,
                    "signals": [
                        signal.model_dump(by_alias=True) for signal in item.candidate.signals
                    ],
                }
                for item in results
            ],
        }

    def _search_interactions(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._keys(arguments, {"page_id", "query"}, {"page_id", "query", "top_k"})
        page_id, query, top_k = (
            arguments.get("page_id"),
            arguments.get("query"),
            arguments.get("top_k", MAX_TOOL_RESULTS),
        )
        if (
            not isinstance(page_id, str)
            or not isinstance(query, str)
            or not query.strip()
            or type(top_k) is not int
            or not 1 <= top_k <= MAX_TOOL_RESULTS
        ):
            raise RequirementToolError("交互搜索参数无效", code="invalid_tool_arguments")
        if page_id not in {page.id for page in self._pages}:
            raise RequirementToolError("页面不属于当前项目分析图", code="out_of_scope")
        ranked = []
        for chain in self._chains.values():
            if chain.page_id == page_id:
                score = sum(_rank(text, query) for text in chain.corpus)
                if score:
                    ranked.append((score, chain))
        ranked.sort(key=lambda item: (-item[0], item[1].id))
        return {
            "status": "ok",
            "chains": [self._chain_summary(chain, score) for score, chain in ranked[:top_k]],
        }

    def _get_chain(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._keys(arguments, {"chain_id"}, {"chain_id"})
        chain_id = arguments.get("chain_id")
        if not isinstance(chain_id, str) or chain_id not in self._chains:
            raise RequirementToolError("链路不属于当前项目分析图", code="out_of_scope")
        result = self._chain_summary(self._chains[chain_id])
        result["status"] = "ok"
        return result

    def _chain_summary(self, chain: ActionChain, score: int | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "chain_id": chain.id,
            "page_id": chain.page_id,
            "text": chain.text,
            "terminated_at": chain.terminated_at,
            "steps": [
                {
                    "node_id": step.node_id,
                    "role": step.role,
                    "name": step.name,
                    "file": step.file,
                    "line": step.line,
                    "end_line": self._end_line(self._nodes.get(step.node_id)),
                }
                for step in chain.steps
            ],
            "evidence": list(chain.corpus),
        }
        if score is not None:
            result["match_score"] = score
        return result

    @staticmethod
    def _end_line(node: IRNode | None) -> int | None:
        value = node.attrs.get("endLine") if node is not None else None
        return value if type(value) is int and value >= 1 else None

    def _read_source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._keys(arguments, {"node_id"}, {"node_id"})
        node_id = arguments.get("node_id")
        node = self._nodes.get(node_id) if isinstance(node_id, str) else None
        if node is None:
            raise RequirementToolError("节点不属于当前项目分析图", code="out_of_scope")
        if node.kind not in {NodeKind.component, NodeKind.fn, NodeKind.hook}:
            raise RequirementToolError("该节点类型不支持源码范围读取", code="unsupported_node_kind")
        if not node.file or node.loc is None:
            raise RequirementToolError("节点缺少源码文件或起始位置", code="range_unavailable")
        start_line, end_line = node.loc.line, self._end_line(node)
        if end_line is None or end_line < start_line:
            raise RequirementToolError(
                "节点没有有效的可信 endLine，无法读取声明范围", code="range_unavailable"
            )
        if end_line - start_line + 1 > MAX_SOURCE_LINES:
            raise RequirementToolError(
                f"源码范围超过 {MAX_SOURCE_LINES} 行上限", code="range_too_large"
            )
        relative = Path(node.file)
        if relative.is_absolute() or ".." in relative.parts:
            raise RequirementToolError("源码路径无效", code="file_outside_project")
        try:
            path = (self._root / relative).resolve(strict=True)
            path.relative_to(self._root)
        except (OSError, ValueError):
            raise RequirementToolError(
                "源码文件不存在或不在项目目录内", code="file_outside_project"
            ) from None
        if not path.is_file():
            raise RequirementToolError("源码路径不是普通文件", code="file_not_found")
        try:
            selected_lines: list[str] = []
            with path.open("r", encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if line_number > end_line:
                        break
                    if line_number >= start_line:
                        selected_lines.append(line)
        except (OSError, UnicodeError) as error:
            raise RequirementToolError(
                f"源码读取失败：{type(error).__name__}", code="read_error"
            ) from None
        if len(selected_lines) != end_line - start_line + 1:
            raise RequirementToolError("文件当前行数小于分析图记录的 endLine", code="range_invalid")
        content = "".join(selected_lines)
        if len(content) > MAX_SOURCE_CHARS:
            raise RequirementToolError(
                f"源码内容超过 {MAX_SOURCE_CHARS} 字符上限", code="range_too_large"
            )
        return {
            "status": "ok",
            "node_id": node.id,
            "node_name": node.name,
            "file": node.file,
            "start_line": start_line,
            "end_line": end_line,
            "content": content,
            "truncated": False,
        }
