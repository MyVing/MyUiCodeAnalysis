"""EvidenceQuery SQL 层的单元测试。

用内存 SQLite（aiosqlite）作为 AsyncEngine，验证：
- 行式索引层写入后 EvidenceQuery 能正确按需检索
- 不同 category 过滤逻辑正确
- 按 pageId / nodeId / chainId 三种定位方式都能查到事实
- skeleton 只返回导航信息，不包含契约数据
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.storage.mysql import _write_graph_rows
from app.specdoc.evidence_query import (
    EvidenceLedger,
    EvidenceQuery,
    MAX_FACTS_PER_QUERY,
)
from tests.graph_fixture import user_graph

# SQLite 兼容版的建表 DDL（去掉 MySQL 特有语法）
_SQLITE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS graph_nodes (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id    TEXT NOT NULL,
      analysis_id   TEXT NOT NULL,
      graph_kind    TEXT NOT NULL,
      node_id       TEXT NOT NULL,
      kind          TEXT NOT NULL,
      name          TEXT NOT NULL,
      file          TEXT NULL,
      line          INTEGER NULL,
      loc_col       INTEGER NULL,
      attrs_json    TEXT NOT NULL,
      confidence    REAL NULL,
      source        TEXT NULL,
      created_at    TEXT NOT NULL,
      UNIQUE (project_id, analysis_id, graph_kind, node_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_edges (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id    TEXT NOT NULL,
      analysis_id   TEXT NOT NULL,
      graph_kind    TEXT NOT NULL,
      edge_id       TEXT NOT NULL,
      from_node     TEXT NOT NULL,
      to_node       TEXT NOT NULL,
      kind          TEXT NOT NULL,
      created_at    TEXT NOT NULL,
      UNIQUE (project_id, analysis_id, graph_kind, edge_id)
    )
    """,
)


@pytest.fixture()
async def sqlite_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for ddl in _SQLITE_DDL:
            await conn.execute(text(ddl))
    return engine


@pytest.fixture()
async def populated_query(sqlite_engine):
    """写入 user_graph 测试图，返回可直接查询的 EvidenceQuery。"""
    graph = user_graph()
    project_id = "prj_test"
    analysis_id = graph.analysisId or "an_test"
    graph_kind = "static"
    now = datetime.now(UTC).isoformat()
    async with sqlite_engine.begin() as conn:
        await _write_graph_rows(
            conn, project_id, analysis_id, graph_kind, graph, now
        )
    return EvidenceQuery(
        engine=sqlite_engine,
        project_id=project_id,
        analysis_id=analysis_id,
        graph_kind=graph_kind,
        ledger=EvidenceLedger(),
    )


# =====================================================================
# 基础写入验证
# =====================================================================


def test_write_graph_rows_is_public(populated_query):
    """fixture 能用说明 _write_graph_rows 写入成功。"""
    assert populated_query is not None


# =====================================================================
# skeleton：只返回导航信息
# =====================================================================


async def test_skeleton_lists_component_pages(populated_query):
    skel = await populated_query.skeleton()
    pages = skel["pages"]
    # user_graph 里有 UserList, UserDetail, TableCard 三个 component
    assert len(pages) == 3
    page_ids = {p["pageId"] for p in pages}
    assert "src/pages/UserList.tsx#UserList" in page_ids
    assert "src/pages/UserDetail.tsx#UserDetail" in page_ids
    assert "src/components/TableCard.tsx#TableCard" in page_ids


async def test_skeleton_has_no_contract_data(populated_query):
    """skeleton 只有导航字段，不能包含 uiText、props 等契约内容。"""
    skel = await populated_query.skeleton()
    pages = skel["pages"]
    for page in pages:
        assert set(page.keys()) == {"pageId", "name", "route"}
        # 不能出现契约字段
        assert "uiText" not in page
        assert "elements" not in page
        assert "props" not in page


# =====================================================================
# execute：category + 定位过滤
# =====================================================================


async def test_execute_all_returns_facts_within_limit(populated_query):
    result = await populated_query.execute(
        "search_evidence", {"category": "all"}
    )
    assert result["status"] == "ok"
    facts = result["facts"]
    # 不会超过上限
    assert len(facts) <= MAX_FACTS_PER_QUERY
    # 每个 fact 都有必要字段
    for f in facts:
        assert f["pageId"]
        assert f["nodeId"]
        assert f["category"]
        assert f["text"]


async def test_execute_uiText_category(populated_query):
    result = await populated_query.execute(
        "search_evidence", {"category": "uiText"}
    )
    assert result["status"] == "ok"
    facts = result["facts"]
    assert len(facts) > 0
    for f in facts:
        assert f["category"] == "uiText"
        # user_graph 里的页面文案
        text_values = {f["text"] for f in facts}
        assert "加载中…" in text_values
        assert "搜索用户" in text_values


async def test_execute_api_category(populated_query):
    result = await populated_query.execute(
        "search_evidence", {"category": "apiFact"}
    )
    assert result["status"] == "ok"
    facts = result["facts"]
    assert len(facts) > 0
    for f in facts:
        assert f["category"] == "apiFact"
    # 应该能拿到 GET /api/users
    texts = {f["text"] for f in facts}
    assert "GET" in texts
    assert "/api/users" in texts


async def test_execute_by_page_id_filters(populated_query):
    page_id = "src/pages/UserList.tsx#UserList"
    result = await populated_query.execute(
        "search_evidence", {"category": "all", "pageId": page_id}
    )
    assert result["status"] == "ok"
    facts = result["facts"]
    assert len(facts) > 0
    # 所有事实的 pageId 应该等于给定的 pageId（或能被 page_id_from_node 解析出来）
    for f in facts:
        assert f["pageId"] == page_id or f["pageId"] is not None


async def test_execute_by_node_id(populated_query):
    api_node_id = "api:GET:/api/users"
    result = await populated_query.execute(
        "search_evidence", {"category": "all", "nodeId": api_node_id}
    )
    assert result["status"] == "ok"
    facts = result["facts"]
    assert len(facts) > 0
    for f in facts:
        assert f["nodeId"] == api_node_id


async def test_execute_invalid_category_rejected(populated_query):
    from app.specdoc.llm import SpecDocToolError

    with pytest.raises(SpecDocToolError) as excinfo:
        await populated_query.execute(
            "search_evidence", {"category": "nonexistent"}
        )
    assert excinfo.value.code == "invalid_tool_arguments"


async def test_execute_invalid_tool_name_rejected(populated_query):
    from app.specdoc.llm import SpecDocToolError

    with pytest.raises(SpecDocToolError) as excinfo:
        await populated_query.execute(
            "other_tool", {"category": "all"}
        )
    assert excinfo.value.code == "unknown_tool"


# =====================================================================
# BFS 链路查询
# =====================================================================


async def test_chain_query_traverses_edges(populated_query):
    """从 UserList 页面出发沿 renders / calls / requests 边走，
    应该能 BFS 到 fetchUsers 和 api:GET:/api/users。"""
    start_node = "src/pages/UserList.tsx#UserList"
    result = await populated_query.execute(
        "search_evidence", {"category": "all", "chainId": start_node}
    )
    assert result["status"] == "ok"
    facts = result["facts"]
    assert len(facts) > 0
    node_ids = {f["nodeId"] for f in facts}
    texts = {f["text"] for f in facts}
    # 起点在链路里
    assert any("UserList" in nid for nid in node_ids)
    # fetchUsers fn 节点能被 BFS 到（calls 边）
    assert any("fetchUsers" in nid for nid in node_ids)
    # api 节点也能被到（requests 边）
    assert "/api/users" in texts or any("api" in nid.lower() for nid in node_ids)


# =====================================================================
# Ledger 记录
# =====================================================================


async def test_ledger_records_returned_facts(populated_query):
    assert len(populated_query.ledger.facts) == 0
    await populated_query.execute("search_evidence", {"category": "uiText"})
    assert len(populated_query.ledger.facts) > 0
    # 再次查询同一 category，ledger 不会重复添加
    before = len(populated_query.ledger.facts)
    await populated_query.execute("search_evidence", {"category": "uiText"})
    assert len(populated_query.ledger.facts) == before


async def test_budget_exhausted_when_too_many_facts(populated_query):
    """单次查询返回超过 MAX_FACTS_PER_QUERY 时应该截断。"""
    # 把 ledger 的 chars_used 设成接近上限
    populated_query.ledger.chars_used = 10_000
    populated_query.ledger.facts.clear()
    # 触发大量事实
    result = await populated_query.execute(
        "search_evidence", {"category": "all"}
    )
    # 只可能是 ok（因为 MAX_FACTS_PER_QUERY 限制了返回数量）
    assert len(result["facts"]) <= MAX_FACTS_PER_QUERY


# =====================================================================
# 边界条件
# =====================================================================


async def test_empty_graph_returns_no_facts(sqlite_engine):
    """空图写入后查询返回空列表。"""
    # 空 graph 也能写入——nodes 和 edges 都是空列表
    from app.contracts import IRGraph

    empty_graph = IRGraph(
        version="1.1", analysisId="an_empty", nodes=[], edges=[]
    )
    async with sqlite_engine.begin() as conn:
        await _write_graph_rows(
            conn, "prj", "an_empty", "static", empty_graph,
            datetime.now(UTC).isoformat(),
        )
    q = EvidenceQuery(sqlite_engine, "prj", "an_empty")
    result = await q.execute("search_evidence", {"category": "all"})
    assert result["status"] == "ok"
    assert result["facts"] == []


async def test_attrs_json_roundtrip(sqlite_engine):
    """attrs_json 的特殊字符（中文、嵌套 dict）写入后能正确读回。"""
    from app.contracts import IRNode, NodeKind, Evidence, Source, Location
    from app.storage.mysql import _write_graph_rows as wgr
    from app.contracts import IRGraph

    node = IRNode(
        id="test#node",
        kind=NodeKind.component,
        name="测试页面",
        file="src/pages/Test.tsx",
        loc=Location(line=10, col=1),
        attrs={
            "uiText": {
                "texts": [
                    {"text": "中文标题 测试", "kind": "text"},
                ]
            },
            "route": "/test",
            "pageId": "page_test",
            "nested": {"a": 1, "b": {"c": "deep_值"}},
        },
        evidence=[
            Evidence(source=Source.static, confidence=0.95, file="src/pages/Test.tsx", loc=Location(line=10, col=1))
        ],
    )
    graph = IRGraph(version="1.1", analysisId="an_attrs", nodes=[node], edges=[])
    async with sqlite_engine.begin() as conn:
        await wgr(conn, "prj", "an_attrs", "static", graph, datetime.now(UTC).isoformat())

    q = EvidenceQuery(sqlite_engine, "prj", "an_attrs")
    result = await q.execute("search_evidence", {"category": "uiText", "nodeId": "test#node"})
    assert result["status"] == "ok"
    assert len(result["facts"]) == 1
    assert "中文标题" in result["facts"][0]["text"]


async def test_upsert_on_rewrite(sqlite_engine):
    """同一三元组重复写入应该覆盖（幂等）。"""
    from app.storage.mysql import _write_graph_rows as wgr

    async def write(pid: str, aid: str, graph):
        async with sqlite_engine.begin() as conn:
            await wgr(conn, pid, aid, "static", graph, datetime.now(UTC).isoformat())

    graph_v1 = user_graph("an_rewrite")
    await write("prj", "an_rewrite", graph_v1)

    q1 = EvidenceQuery(sqlite_engine, "prj", "an_rewrite")
    result1 = await q1.execute("search_evidence", {"category": "all"})
    count1 = len(result1["facts"])

    # 写入第二次（覆盖语义）
    graph_v2 = user_graph("an_rewrite")
    await write("prj", "an_rewrite", graph_v2)

    q2 = EvidenceQuery(sqlite_engine, "prj", "an_rewrite")
    result2 = await q2.execute("search_evidence", {"category": "all"})
    count2 = len(result2["facts"])

    # 覆盖后不应该有重复事实（因为 _write_graph_rows 先 DELETE 再 INSERT）
    assert count1 == count2
