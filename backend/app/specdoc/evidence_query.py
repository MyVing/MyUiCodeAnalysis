"""基于 graph_nodes / graph_edges 行式索引层的按需证据查询。

与 EvidenceSearch（全量 flatten 内存过滤）的核心差异：
- 只走 SQL，不反序列化完整 IRGraph；内存占用 = 查询结果大小，与全图规模无关
- 直接利用 MySQL 普通列索引（kind / file / from_node / to_node），避免 JSON 列全表扫描
- 写入路径上 _save_graph 已双写，两张表永远与 payload JSON 保持同步

EvidenceQuery 实现了 EvidenceSearch 的 execute 接口，
可以作为 specdoc EvidenceSearch 的零破坏替代：
EVIDENCE_TOOL schema 不变 → orchestrator 无感替换 → AI 侧行为等价。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.specdoc.llm import SpecDocToolError
from app.specdoc.retrieval import EvidenceFact, EvidenceLedger

MAX_FACTS_PER_QUERY = 24  # 原来 EvidenceSearch 是 12；SQL 查询更可控，放宽一倍
MAX_TEXT_CHARS_PER_FACT = 2_000
MAX_LEDGER_CHARS = 24_000


EVIDENCE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_evidence",
        "description": "按已提供的页面、链路或节点 ID 查询该范围内的代码证据。",
        "parameters": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "chainId": {"type": "string"},
                "nodeId": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": [
                        "chain", "element", "event", "uiText",
                        "form", "apiFact", "all",
                    ],
                },
            },
            "required": ["category"],
            "additionalProperties": False,
        },
    },
}


def _digest(page_id: str, node_id: str, chain_id: str | None, category: str, text: str) -> str:
    identity = json.dumps(
        [page_id, node_id, chain_id, category, text],
        ensure_ascii=False, separators=(",", ":"),
    )
    return "ev_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _row_dict(row: Any) -> dict[str, Any]:
    """SQLAlchemy Row 的字符串索引在 MySQL / SQLite 驱动下行为不一致。

    统一用 _mapping 转成 dict：两者都支持。
    """
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    # 纯 tuple 的兜底——SQLAlchemy Row 在 aiomysql / aiosqlite 下
    # 都实现了 _mapping，这里只处理极端情况。
    if isinstance(row, tuple):
        return {}
    return dict(row)


class EvidenceQuery:
    """SQL 层的按需证据检索器。

    构造时接收 engine + 三元组 (project_id, analysis_id, graph_kind)，
    所有查询都自动带 WHERE project_id=? AND analysis_id=? AND graph_kind=?，
    不会跨版本漏查或串查。page_id 从组件节点的 attrs_json 里取，
    与现有 specdoc 链路里 pageId 的语义完全一致。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        project_id: str,
        analysis_id: str,
        graph_kind: str = "static",
        ledger: EvidenceLedger | None = None,
    ) -> None:
        self._engine = engine
        self._pid = project_id
        self._aid = analysis_id
        self._kind = graph_kind
        self.ledger = ledger or EvidenceLedger()
        # 内存缓存：避免同一次 specdoc 生成里重复执行相同 SQL
        # 存 dict 而非 Row——SQLAlchemy Row 在不同驱动下字符串索引行为不一致
        self._node_cache: dict[str, dict[str, Any]] = {}
        self._edges_cache: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # 公共查询接口
    # ------------------------------------------------------------------

    async def execute(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """与 EvidenceSearch.execute 同签名、同返回格式。"""
        if set(arguments) - {"pageId", "chainId", "nodeId", "category"}:
            raise SpecDocToolError(
                "证据检索参数包含未允许字段", code="invalid_tool_arguments"
            )
        if name != "search_evidence":
            raise SpecDocToolError(
                "未知的 specdoc 证据工具", code="unknown_tool"
            )
        category = arguments.get("category")
        if category not in {
            "chain", "element", "event", "uiText", "form", "apiFact", "all",
        }:
            raise SpecDocToolError(
                "证据类别无效", code="invalid_tool_arguments"
            )
        page_id = arguments.get("pageId")
        chain_id = arguments.get("chainId")
        node_id = arguments.get("nodeId")

        facts = await self._query(
            page_id=page_id, chain_id=chain_id, node_id=node_id, category=category
        )
        matched = facts[:MAX_FACTS_PER_QUERY]
        accepted = self.ledger.record(matched)
        if len(accepted) < len(matched):
            return {
                "status": "budget_exhausted",
                "facts": [f.as_dict() for f in accepted],
            }
        return {"status": "ok", "facts": [f.as_dict() for f in accepted]}

    async def skeleton(
        self, feature_pages: dict[str, tuple[str, ...]] | None = None
    ) -> dict[str, Any]:
        """生成轻量导航骨架：pageId + route + 统计数，不包含契约数据。"""
        pages = await self._list_page_skeletons()
        if feature_pages is not None:
            included = {
                pid for values in feature_pages.values() for pid in values
            }
            pages = [p for p in pages if p.get("pageId") in included]
        return {"pages": pages}

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _query(
        self,
        *,
        page_id: str | None,
        chain_id: str | None,
        node_id: str | None,
        category: str,
    ) -> list[EvidenceFact]:
        """按 category + 可选 pageId/chainId/nodeId 过滤查询事实。"""
        if node_id is not None:
            return await self._query_node(node_id, category)
        if chain_id is not None:
            return await self._query_chain(chain_id, category, page_id=page_id)
        if page_id is not None:
            return await self._query_page(page_id, category)
        # 兜底：全量查询，但带 MAX_FACTS_PER_QUERY 限制
        return await self._query_all(category)

    async def _get_node(self, node_id: str) -> dict[str, Any] | None:
        """缓存式按 node_id 精查单节点（返回 dict，兼容所有 SQLAlchemy 驱动）。"""
        if node_id in self._node_cache:
            return self._node_cache[node_id]
        query = text(
            """
            SELECT * FROM graph_nodes
            WHERE project_id = :pid AND analysis_id = :aid
              AND graph_kind = :kind AND node_id = :node_id
            """
        )
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    query,
                    {"pid": self._pid, "aid": self._aid,
                     "kind": self._kind, "node_id": node_id},
                )
            ).first()
        if row is not None:
            as_dict = _row_dict(row)
            self._node_cache[node_id] = as_dict
            return as_dict
        return None

    async def _all_edges(self) -> list[dict[str, Any]]:
        if self._edges_cache is not None:
            return self._edges_cache
        query = text(
            """
            SELECT * FROM graph_edges
            WHERE project_id = :pid AND analysis_id = :aid AND graph_kind = :kind
            """
        )
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    query, {"pid": self._pid, "aid": self._aid, "kind": self._kind}
                )
            ).all()
        self._edges_cache = [_row_dict(r) for r in rows]
        return self._edges_cache

    # -- 按 node_id 查询 ------------------------------------------------

    async def _query_node(
        self, node_id: str, category: str
    ) -> list[EvidenceFact]:
        node = await self._get_node(node_id)
        if node is None:
            raise SpecDocToolError(
                "节点不在本次证据范围内", code="evidence_out_of_scope"
            )
        return self._node_to_facts(node, page_id=None, chain_id=None)

    # -- 按 chain_id 查询 -----------------------------------------------

    async def _query_chain(
        self,
        chain_id: str,
        category: str,
        *,
        page_id: str | None = None,
    ) -> list[EvidenceFact]:
        """chain_id 在现有 IR 里是 element 节点的 node_id（链路起点）。

        沿边表 BFS：从起点沿 renders/calls/requests 等有向边走，
        收集所有可达节点，再按 category 过滤生成事实。
        """
        start = await self._get_node(chain_id)
        if start is None:
            raise SpecDocToolError(
                "链路不在本次证据范围内", code="evidence_out_of_scope"
            )

        # BFS 沿边
        edges = await self._all_edges()
        outgoing: dict[str, list[dict[str, Any]]] = {}
        for e in edges:
            outgoing.setdefault(e["from_node"], []).append(e)

        visited: set[str] = set()
        queue = [chain_id]
        chain_node_ids: list[str] = []
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            chain_node_ids.append(current)
            for edge in outgoing.get(current, []):
                if edge["to_node"] not in visited:
                    queue.append(edge["to_node"])

        # 逐个节点展开事实
        page_id_guess = page_id or self._page_id_from_node(start)
        facts: list[EvidenceFact] = []
        for nid in chain_node_ids:
            node = await self._get_node(nid)
            if node is not None:
                facts.extend(
                    self._node_to_facts(
                        node, page_id=page_id_guess, chain_id=chain_id
                    )
                )
        return self._filter_by_category(facts, category)

    # -- 按 page_id 查询 ------------------------------------------------

    async def _query_page(
        self, page_id: str, category: str
    ) -> list[EvidenceFact]:
        """page_id 在现有 IR 里是 component 节点的 node_id 或
        attrs_json->'$.pageId' 字段。后者更通用，但前者是事实起点。

        先查 component+pageId 节点本身（拿到 uiText、forms），
        再查所有与该 component 相连的下游节点（elements、events、apis）。
        """
        # 1. 拿到 component 页面节点本身
        page_node = await self._get_node(page_id)
        facts: list[EvidenceFact] = []
        if page_node is not None:
            facts.extend(
                self._node_to_facts(page_node, page_id=page_id, chain_id=None)
            )

        # 2. 拿到所有与该 component 相关联的节点：
        #    下游（被 renders 边指向）+ 上游（renders 边来自 component 的）
        query = text(
            """
            SELECT DISTINCT n.* FROM graph_nodes n
            JOIN graph_edges e
              ON e.from_node = n.node_id OR e.to_node = n.node_id
            WHERE n.project_id = :pid AND n.analysis_id = :aid
              AND n.graph_kind = :kind
              AND (e.from_node = :page_id OR e.to_node = :page_id)
              AND n.node_id != :page_id
            """
        )
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    query,
                    {"pid": self._pid, "aid": self._aid,
                     "kind": self._kind, "page_id": page_id},
                )
            ).all()
        for row in rows:
            facts.extend(
                self._node_to_facts(_row_dict(row), page_id=page_id, chain_id=None)
            )
        return self._filter_by_category(facts, category)

    # -- 全量查询 --------------------------------------------------------

    async def _query_all(self, category: str) -> list[EvidenceFact]:
        """不指定 pageId/chainId/nodeId 的兜底查询：只返回前 MAX_FACTS_PER_QUERY 条。

        这种模式应该尽量避免——优先让 AI 提供 pageId 或 chainId。
        """
        query = text(
            """
            SELECT * FROM graph_nodes
            WHERE project_id = :pid AND analysis_id = :aid AND graph_kind = :kind
            LIMIT 500
            """
        )
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    query, {"pid": self._pid, "aid": self._aid, "kind": self._kind}
                )
            ).all()
        facts: list[EvidenceFact] = []
        for row in rows:
            rdict = _row_dict(row)
            pid = self._page_id_from_node(rdict)
            facts.extend(self._node_to_facts(rdict, page_id=pid, chain_id=None))
        return self._filter_by_category(facts, category)

    # -- 骨架生成 --------------------------------------------------------

    async def _list_page_skeletons(self) -> list[dict[str, Any]]:
        """列出 component 节点作为页面导航骨架。

        不在 SQL 里做 JSON_EXTRACT：attrs_json 的 JSON 解析交给 Python 层，
        让查询对 MySQL / SQLite 等后端完全数据库无关。
        """
        query = text(
            """
            SELECT node_id, name, attrs_json
            FROM graph_nodes
            WHERE project_id = :pid AND analysis_id = :aid
              AND graph_kind = :kind AND kind = 'component'
            """
        )
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    query, {"pid": self._pid, "aid": self._aid, "kind": self._kind}
                )
            ).all()

        skeletons: list[dict[str, Any]] = []
        for row in rows:
            r = _row_dict(row)
            attrs = self._parse_attrs(r["attrs_json"])
            skeletons.append({
                "pageId": attrs.get("pageId") or r["node_id"],
                "name": r["name"],
                "route": attrs.get("route"),
            })
        return skeletons

    # ------------------------------------------------------------------
    # 事实展开工具
    # ------------------------------------------------------------------

    def _node_to_facts(
        self,
        node: dict[str, Any],
        *,
        page_id: str | None,
        chain_id: str | None,
    ) -> list[EvidenceFact]:
        """根据节点 kind + attrs_json 展开多类事实，对应原来
        EvidenceSearch 里 flatten pages 字典的逻辑。"""
        kind = node["kind"]
        node_id = node["node_id"]
        attrs = self._parse_attrs(node["attrs_json"])
        pid = page_id or self._page_id_from_node(node)
        file = node["file"]
        line = node["line"]
        facts: list[EvidenceFact] = []

        # -- component（页面自身）---- uiText 全放在 component 节点的 attrs 里
        if kind == "component":
            ui_texts = self._safe_get(attrs, "uiText", "texts") or []
            for t in ui_texts:
                text_val = t.get("text") if isinstance(t, dict) else None
                if text_val:
                    facts.append(self._mk(
                        pid, node_id, "uiText", text_val,
                        chain_id=chain_id, file=file, line=line,
                    ))
            # form 字段
            for form in self._safe_get(attrs, "forms") or []:
                field_info = self._safe_get(form, "frontField")
                if isinstance(field_info, dict) and field_info.get("quote"):
                    facts.append(self._mk(
                        pid, node_id, "form", field_info["quote"],
                        chain_id=chain_id, file=file, line=line,
                    ))

        # -- element ------ viewText / formField.quote / columns.title
        elif kind == "element":
            for key in ("viewText", "name"):
                val = attrs.get(key)
                if isinstance(val, str) and val.strip():
                    facts.append(self._mk(
                        pid, node_id, "element", val.strip(),
                        chain_id=chain_id, file=file, line=line,
                    ))
            field = attrs.get("formField")
            if isinstance(field, dict) and field.get("quote"):
                facts.append(self._mk(
                    pid, node_id, "element", field["quote"],
                    chain_id=chain_id, file=file, line=line,
                ))
            for col in self._safe_get(attrs, "columns") or []:
                if isinstance(col, dict) and col.get("title"):
                    facts.append(self._mk(
                        pid, node_id, "element", col["title"],
                        chain_id=chain_id, file=file, line=line,
                    ))

        # -- fn（事件 handler）------ name 即 handler 名
        elif kind == "fn":
            handler = node["name"]
            if handler:
                facts.append(self._mk(
                    pid, node_id, "event", handler,
                    chain_id=chain_id, file=file, line=line,
                ))

        # -- api ------ method / url / requestBody
        elif kind == "api":
            method = attrs.get("method")
            url = attrs.get("url")
            if method and url:
                combined = f"{method} {url}"
                facts.append(self._mk(
                    pid, node_id, "apiFact", method,
                    chain_id=chain_id, file=file, line=line,
                ))
                facts.append(self._mk(
                    pid, node_id, "apiFact", url,
                    chain_id=chain_id, file=file, line=line,
                ))
                facts.append(self._mk(
                    pid, node_id, "apiFact", combined,
                    chain_id=chain_id, file=file, line=line,
                ))
            body = attrs.get("requestBody")
            if isinstance(body, (dict, list)):
                compact = json.dumps(
                    body, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
                facts.append(self._mk(
                    pid, node_id, "apiFact", compact,
                    chain_id=chain_id, file=file, line=line,
                ))

        return facts

    def _mk(
        self,
        page_id: str,
        node_id: str,
        category: str,
        text: str,
        *,
        chain_id: str | None = None,
        file: str | None = None,
        line: int | None = None,
    ) -> EvidenceFact | None:
        """构造 EvidenceFact，空串 / 超长直接丢弃（保持与 EvidenceSearch 一致）。"""
        if not text or not text.strip():
            return None
        if len(text) > MAX_TEXT_CHARS_PER_FACT:
            return None
        eid = _digest(page_id, node_id, chain_id, category, text)
        return EvidenceFact(
            evidence_id=eid,
            page_id=page_id,
            node_id=node_id,
            category=category,
            text=text,
            chain_id=chain_id,
            file=file,
            line=line,
        )

    def _filter_by_category(
        self, facts: list[EvidenceFact], category: str
    ) -> list[EvidenceFact]:
        if category == "all":
            return [f for f in facts if f is not None]  # 过滤 _mk 里的 None
        return [f for f in facts if f.category == category]

    @staticmethod
    def _parse_attrs(attrs_json: Any) -> dict[str, Any]:
        if attrs_json is None:
            return {}
        if isinstance(attrs_json, dict):
            return attrs_json
        try:
            if isinstance(attrs_json, (bytes, bytearray)):
                return json.loads(attrs_json.decode("utf-8"))
            return json.loads(str(attrs_json))
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _safe_get(obj: Any, *keys: str) -> Any:
        """obj 逐级取键，中间任一环节不是 dict 即返回 None。"""
        current = obj
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    @staticmethod
    def _page_id_from_node(node: dict[str, Any]) -> str | None:
        """从节点 attrs 里取 pageId，或 fallback 到 node_id 本身。"""
        attrs = EvidenceQuery._parse_attrs(node["attrs_json"])
        pid = attrs.get("pageId")
        if isinstance(pid, str) and pid:
            return pid
        return node["node_id"]
