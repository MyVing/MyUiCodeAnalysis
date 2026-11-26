"""基于 graph_nodes / graph_edges 行式索引层的按需证据查询。

与 EvidenceSearch（全量 flatten 内存过滤）的核心差异：
- 只走 SQL，不反序列化完整 IRGraph；内存占用 = 查询结果大小，与全图规模无关
- 直接利用 MySQL 普通列索引（kind / file / from_node / to_node），避免 JSON 列全表扫描
- 写入路径上 _save_graph 已双写，两张表永远与 payload JSON 保持同步

EvidenceQuery 实现了 EvidenceSearch 的 execute 接口，
可以作为 specdoc EvidenceSearch 的零破坏替代：
EVIDENCE_TOOL schema 不变 → orchestrator 无感替换 → AI 侧行为等价。

证据范围（page / chain / node）由调用方在构造时注入 Authorization 映射：
- page_nodes:  pageId → 该页面授权的节点 id（页面节点 + 元素 + 接口 + 链路节点）
- chain_nodes: chainId → 链路节点 id（ActionChain.node_ids）
- chain_pages: chainId → 所属页面 id

chainId 是 ``f"{element.id}::{node_path}"``（见 requirement.chains），不是节点 id，
因此不能再拿 chainId 当 node_id 去查节点，必须按注入映射展开。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
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

_VALID_CATEGORIES = frozenset(
    {"chain", "element", "event", "uiText", "form", "apiFact", "all"}
)


async def graph_rows_exist(
    engine: AsyncEngine,
    project_id: str,
    analysis_id: str,
    graph_kind: str = "static",
) -> bool:
    """该版本的行式投影是否存在。

    行表只在保存图时双写，升级前已保存的图没有行数据。空表（或表尚未创建）
    意味着必须回退 EvidenceSearch，否则所有查询都会 ``evidence_out_of_scope``。
    """
    query = text(
        """
        SELECT 1 FROM graph_nodes
        WHERE project_id = :pid AND analysis_id = :aid AND graph_kind = :kind
        LIMIT 1
        """
    )
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    query,
                    {"pid": project_id, "aid": analysis_id, "kind": graph_kind},
                )
            ).first()
    except SQLAlchemyError:
        # schema 未升级（表不存在）也按「无行数据」处理，交给调用方回退
        return False
    return row is not None


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
    不会跨版本漏查或串查。证据范围由注入的 page_nodes / chain_nodes /
    chain_pages 决定；未提供映射时退化为按数据库存在性校验（兼容旧调用）。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        project_id: str,
        analysis_id: str,
        graph_kind: str = "static",
        ledger: EvidenceLedger | None = None,
        *,
        page_nodes: dict[str, tuple[str, ...]] | None = None,
        chain_nodes: dict[str, tuple[str, ...]] | None = None,
        chain_pages: dict[str, str] | None = None,
    ) -> None:
        self._engine = engine
        self._pid = project_id
        self._aid = analysis_id
        self._kind = graph_kind
        self.ledger = ledger or EvidenceLedger()
        self._page_nodes = dict(page_nodes or {})
        self._chain_nodes = dict(chain_nodes or {})
        self._chain_pages = dict(chain_pages or {})
        # 节点 → 所属页面：page_nodes 的第一归属优先，用于事实归属与范围校验
        self._node_page: dict[str, str] = {}
        for page_id, node_ids in self._page_nodes.items():
            for node_id in node_ids:
                self._node_page.setdefault(node_id, page_id)
        # 未注入任何映射时不限制节点范围（旧调用 / 内存 fixture）
        self._authorized_nodes: set[str] | None = None
        if self._page_nodes or self._chain_nodes:
            authorized: set[str] = set(self._node_page)
            for node_ids in self._chain_nodes.values():
                authorized.update(node_ids)
            self._authorized_nodes = authorized
        # 内存缓存：避免同一次 specdoc 生成里重复执行相同 SQL
        # 存 dict 而非 Row——SQLAlchemy Row 在不同驱动下字符串索引行为不一致
        self._node_cache: dict[str, dict[str, Any]] = {}

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
        if category not in _VALID_CATEGORIES:
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
            return await self._query_node(node_id, category, page_id=page_id)
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
        nodes = await self._get_nodes([node_id])
        return nodes.get(node_id)

    async def _get_nodes(
        self, node_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """批量取节点：一次 SQL 取回未缓存的节点，避免链路查询的 N+1。"""
        missing = [
            node_id for node_id in dict.fromkeys(node_ids)
            if node_id not in self._node_cache
        ]
        if missing:
            query = text(
                """
                SELECT * FROM graph_nodes
                WHERE project_id = :pid AND analysis_id = :aid
                  AND graph_kind = :kind AND node_id IN :node_ids
                """
            ).bindparams(bindparam("node_ids", expanding=True))
            async with self._engine.connect() as conn:
                rows = (
                    await conn.execute(
                        query,
                        {"pid": self._pid, "aid": self._aid,
                         "kind": self._kind, "node_ids": missing},
                    )
                ).all()
            for row in rows:
                data = _row_dict(row)
                self._node_cache[data["node_id"]] = data
        return {
            node_id: self._node_cache[node_id]
            for node_id in node_ids if node_id in self._node_cache
        }

    def _check_node_scope(self, node_id: str) -> None:
        if self._authorized_nodes is not None and node_id not in self._authorized_nodes:
            raise SpecDocToolError(
                "节点不在本次证据范围内", code="evidence_out_of_scope"
            )

    # -- 按 node_id 查询 ------------------------------------------------

    async def _query_node(
        self, node_id: str, category: str, *, page_id: str | None = None
    ) -> list[EvidenceFact]:
        self._check_node_scope(node_id)
        # 与 EvidenceSearch 的 pageId AND nodeId 过滤对齐：节点必须属于该页面
        if page_id is not None and self._authorized_nodes is not None:
            allowed = self._page_nodes.get(page_id)
            if allowed is None or node_id not in allowed:
                return []
        node = await self._get_node(node_id)
        if node is None:
            raise SpecDocToolError(
                "节点不在本次证据范围内", code="evidence_out_of_scope"
            )
        page_id = self._node_page.get(node_id) or self._page_id_from_node(node)
        facts = self._node_to_facts(node, page_id=page_id, chain_id=None)
        return self._filter_by_category(facts, category)

    # -- 按 chain_id 查询 -----------------------------------------------

    async def _query_chain(
        self,
        chain_id: str,
        category: str,
        *,
        page_id: str | None = None,
    ) -> list[EvidenceFact]:
        """按注入的 chain_nodes 映射展开链路节点。

        chainId 不是节点 id（见模块 docstring），不能直接查节点；
        缺失映射即视为越界，避免把任意 id 当成链路的起点。
        """
        node_ids = self._chain_nodes.get(chain_id)
        if node_ids is None:
            raise SpecDocToolError(
                "链路不在本次证据范围内", code="evidence_out_of_scope"
            )
        chain_page = self._chain_pages.get(chain_id)
        if page_id is not None and chain_page is not None and page_id != chain_page:
            return []

        nodes = await self._get_nodes(list(node_ids))
        facts: list[EvidenceFact] = []
        for node_id in node_ids:
            node = nodes.get(node_id)
            if node is not None:
                facts.extend(
                    self._node_to_facts(
                        node, page_id=chain_page, chain_id=chain_id
                    )
                )
        return self._filter_by_category(facts, category)

    # -- 按 page_id 查询 ------------------------------------------------

    async def _query_page(
        self, page_id: str, category: str
    ) -> list[EvidenceFact]:
        """按注入的 page_nodes 精确取该页面授权的节点，杜绝跨页错误归属。"""
        node_ids = self._page_nodes.get(page_id)
        if node_ids is None:
            raise SpecDocToolError(
                "页面不在本次证据范围内", code="evidence_out_of_scope"
            )
        nodes = await self._get_nodes(list(node_ids))
        facts: list[EvidenceFact] = []
        for node_id in node_ids:
            node = nodes.get(node_id)
            if node is not None:
                facts.extend(
                    self._node_to_facts(node, page_id=page_id, chain_id=None)
                )
        return self._filter_by_category(facts, category)

    # -- 全量查询 --------------------------------------------------------

    async def _query_all(self, category: str) -> list[EvidenceFact]:
        """不指定 pageId/chainId/nodeId 的兜底查询。

        有授权映射时只查授权节点；没有时退化为前 500 个节点。
        这种模式应尽量避免——优先让 AI 提供 pageId 或 chainId。
        """
        if self._authorized_nodes is not None:
            node_ids = sorted(self._authorized_nodes)
        else:
            query = text(
                """
                SELECT node_id FROM graph_nodes
                WHERE project_id = :pid AND analysis_id = :aid AND graph_kind = :kind
                ORDER BY node_id
                LIMIT 500
                """
            )
            async with self._engine.connect() as conn:
                rows = (
                    await conn.execute(
                        query,
                        {"pid": self._pid, "aid": self._aid, "kind": self._kind},
                    )
                ).all()
            node_ids = [str(_row_dict(row)["node_id"]) for row in rows]

        nodes = await self._get_nodes(node_ids)
        facts: list[EvidenceFact] = []
        for node_id in node_ids:
            node = nodes.get(node_id)
            if node is None:
                continue
            pid = self._node_page.get(node_id) or self._page_id_from_node(node)
            facts.extend(self._node_to_facts(node, page_id=pid, chain_id=None))
        return self._filter_by_category(facts, category)

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
        """按 EvidenceSearch 的事实口径展开节点。

        - component / server-component：只出 uiText（页面级文案）
        - element：viewText + formField.quote（element 与 form 两类）
          + columns.title + events[].handler（锚元素节点，event 类）
        - fn：不产自然事实；链路场景用节点名作为一步文本
        - api：method / url / 「METHOD url」/ requestBody JSON 与递归 key/value

        链路查询（chain_id 非空）下所有文本统一标为 category="chain"，
        与 EvidenceSearch 的 chain 事实保持同一口径。
        """
        kind = node["kind"]
        node_id = node["node_id"]
        attrs = self._parse_attrs(node["attrs_json"])
        pid = page_id or self._page_id_from_node(node)
        file = node["file"]
        line = node["line"]
        chain_mode = chain_id is not None
        facts: list[EvidenceFact] = []

        def add(category: str, value: Any) -> None:
            if not isinstance(value, str):
                return
            fact = self._mk(
                pid, node_id, "chain" if chain_mode else category, value,
                chain_id=chain_id, file=file, line=line,
            )
            if fact is not None:
                facts.append(fact)

        if kind in ("component", "server-component"):
            texts = self._safe_get(attrs, "uiText", "texts")
            if isinstance(texts, list):
                for entry in texts:
                    if isinstance(entry, dict):
                        add("uiText", entry.get("text"))

        elif kind == "element":
            add("element", attrs.get("viewText"))
            field = attrs.get("formField")
            if isinstance(field, dict):
                add("element", field.get("quote"))
                add("form", field.get("quote"))
            columns = attrs.get("columns")
            if isinstance(columns, list):
                for column in columns:
                    if isinstance(column, dict):
                        add("element", column.get("title"))
            events = attrs.get("events")
            if isinstance(events, list):
                for binding in events:
                    if not isinstance(binding, dict):
                        continue
                    # 生产链路传入 events_by_page，事件事实的文本是 handler 名
                    # （Event.handler.name）；与 EvidenceSearch 保持一致。
                    handler_name = binding.get("handlerName")
                    add("event", handler_name)

        elif kind == "fn":
            if chain_mode:
                add("chain", node["name"])

        elif kind == "api":
            method = attrs.get("method")
            url = attrs.get("url")
            add("apiFact", method)
            add("apiFact", url)
            if isinstance(method, str) and isinstance(url, str):
                add("apiFact", f"{method} {url}")
            body = attrs.get("requestBody")
            if isinstance(body, (dict, list)):
                add(
                    "apiFact",
                    json.dumps(
                        body, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                self._add_body_texts(add, body)

        return facts

    @staticmethod
    def _add_body_texts(add: Any, value: Any) -> None:
        """递归展开 requestBody：key 与字符串值都作为 apiFact 事实。"""
        if isinstance(value, dict):
            for key, nested in value.items():
                add("apiFact", key)
                EvidenceQuery._add_body_texts(add, nested)
        elif isinstance(value, list):
            for nested in value:
                EvidenceQuery._add_body_texts(add, nested)
        else:
            add("apiFact", value)

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
            file=file if isinstance(file, str) else None,
            line=line if isinstance(line, int) else None,
        )

    def _filter_by_category(
        self, facts: list[EvidenceFact], category: str
    ) -> list[EvidenceFact]:
        return [
            fact for fact in facts
            if fact is not None and (category == "all" or fact.category == category)
        ]

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
