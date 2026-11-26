"""EvidenceQuery SQL 层的单元测试。

用内存 SQLite（aiosqlite）作为 AsyncEngine，验证：
- 注入的 page_nodes / chain_nodes / chain_pages 决定证据范围
- chainId 是「element::node_path」而非节点 id，仍能正确解析（P0-1 回归）
- category + pageId / nodeId 过滤正确，越界 id 抛 evidence_out_of_scope
- 事实口径与 EvidenceSearch 在 uiText / element / form / apiFact 上一致
- build_skeleton 只产出导航信息，三个推断章节共用
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.specdoc.collect import Materials, collect_materials
from app.specdoc.evidence import build_evidence_pack, build_skeleton
from app.specdoc.evidence_query import (
    MAX_FACTS_PER_QUERY,
    EvidenceLedger,
    EvidenceQuery,
    graph_rows_exist,
)
from app.specdoc.infer import infer_sections
from app.specdoc.llm import SpecDocToolError
from app.specdoc.retrieval import EvidenceSearch
from app.storage.mysql import _write_graph_rows
from tests.graph_fixture import user_graph
from tests.specdoc_fixture import spec_graph, spec_requirement_map

PAGE_ID = "src/pages/Transfer.tsx#Transfer"

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
    yield engine
    await engine.dispose()


async def _seed(
    engine,
    graph_,
    *,
    project_id: str = "prj_test",
    analysis_id: str | None = None,
) -> tuple[str, str]:
    aid = analysis_id or graph_.analysisId or "an_test"
    async with engine.begin() as conn:
        await _write_graph_rows(
            conn, project_id, aid, "static", graph_, datetime.now(UTC)
        )
    return project_id, aid


def _scope_maps(
    materials: Materials,
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
    dict[str, str],
]:
    """与 infer._scope_maps 相同：页面 / 链路授权映射。"""
    page_nodes: dict[str, tuple[str, ...]] = {}
    chain_nodes: dict[str, tuple[str, ...]] = {}
    chain_pages: dict[str, str] = {}
    for material in materials.pages:
        node_ids = [material.node.id]
        node_ids.extend(source.node.id for source in material.elements)
        node_ids.extend(material.api_nodes.keys())
        for chain in material.chains:
            chain_nodes[chain.id] = tuple(chain.node_ids)
            chain_pages[chain.id] = material.page.pageId
            node_ids.extend(chain.node_ids)
        page_nodes[material.page.pageId] = tuple(dict.fromkeys(node_ids))
    return page_nodes, chain_nodes, chain_pages


def _form_materials() -> tuple[object, Materials]:
    """spec_graph + 表单注册 + 请求体：覆盖 form / apiFact 两类事实。"""
    graph_ = spec_graph("an_form")
    select = next(node for node in graph_.nodes if node.id.endswith("jsx:select"))
    select.attrs["formField"] = {
        "name": "payeeAccount",
        "quote": '<select name="payeeAccount">',
    }
    api = next(node for node in graph_.nodes if node.id == "api:POST:/api/transfer")
    api.attrs["requestBody"] = {
        "wrapper": "payload",
        "fields": [{"name": "payeeAccount"}],
    }
    requirement_map = spec_requirement_map(graph_)
    return graph_, collect_materials(requirement_map, graph_)


@pytest.fixture()
async def spec_query(sqlite_engine):
    graph_ = spec_graph("an_spec")
    requirement_map = spec_requirement_map(graph_)
    materials = collect_materials(requirement_map, graph_)
    project_id, analysis_id = await _seed(
        sqlite_engine, graph_, analysis_id="an_spec"
    )
    page_nodes, chain_nodes, chain_pages = _scope_maps(materials)
    query = EvidenceQuery(
        sqlite_engine,
        project_id,
        analysis_id,
        graph_kind="static",
        ledger=EvidenceLedger(),
        page_nodes=page_nodes,
        chain_nodes=chain_nodes,
        chain_pages=chain_pages,
    )
    return query, materials


# =====================================================================
# P0-1：chainId 解析
# =====================================================================


def _button_chain(materials: Materials):
    return next(
        chain for chain in materials.pages[0].chains
        if chain.element.node_id.endswith("jsx:button")
    )


async def test_chain_id_resolves_to_chain_nodes(spec_query):
    """chainId 是 element::node_path，不是节点 id；仍必须能查到链路事实。"""
    query, materials = spec_query
    chain = _button_chain(materials)

    result = await query.execute(
        "search_evidence", {"chainId": chain.id, "category": "all"}
    )

    assert result["status"] == "ok"
    assert result["facts"]
    assert all(fact["chainId"] == chain.id for fact in result["facts"])
    assert all(fact["pageId"] == PAGE_ID for fact in result["facts"])
    assert all(fact["nodeId"] in chain.node_ids for fact in result["facts"])
    texts = {fact["text"] for fact in result["facts"]}
    assert "转账" in texts
    assert "handleTransfer" in texts
    assert "POST /api/transfer" in texts


async def test_unknown_chain_id_is_out_of_scope(spec_query):
    query, _ = spec_query
    with pytest.raises(SpecDocToolError) as excinfo:
        await query.execute(
            "search_evidence", {"chainId": "not-a-chain", "category": "all"}
        )
    assert excinfo.value.code == "evidence_out_of_scope"


async def test_chain_query_only_emits_chain_category(spec_query):
    """链路查询的全部文本都标为 chain，与 EvidenceSearch 口径一致。"""
    query, materials = spec_query
    chain = _button_chain(materials)
    chain_facts = await query.execute(
        "search_evidence", {"chainId": chain.id, "category": "chain"}
    )
    element_facts = await query.execute(
        "search_evidence", {"chainId": chain.id, "category": "element"}
    )
    assert chain_facts["facts"]
    assert element_facts["facts"] == []


# =====================================================================
# category + 定位过滤
# =====================================================================


async def test_node_query_respects_category(spec_query):
    query, _ = spec_query
    api_node_id = "api:POST:/api/transfer"

    api_facts = await query.execute(
        "search_evidence", {"nodeId": api_node_id, "category": "apiFact"}
    )
    assert api_facts["facts"]
    assert all(fact["nodeId"] == api_node_id for fact in api_facts["facts"])
    assert all(fact["category"] == "apiFact" for fact in api_facts["facts"])
    assert "POST /api/transfer" in {fact["text"] for fact in api_facts["facts"]}

    ui_facts = await query.execute(
        "search_evidence", {"nodeId": api_node_id, "category": "uiText"}
    )
    assert ui_facts["facts"] == []


async def test_node_outside_scope_is_rejected(sqlite_engine):
    graph_ = user_graph("an_scope")
    project_id, analysis_id = await _seed(
        sqlite_engine, graph_, analysis_id="an_scope"
    )
    allowed_page = "src/pages/UserList.tsx#UserList"
    query = EvidenceQuery(
        sqlite_engine,
        project_id,
        analysis_id,
        page_nodes={allowed_page: (allowed_page,)},
    )
    with pytest.raises(SpecDocToolError) as excinfo:
        await query.execute(
            "search_evidence",
            {"nodeId": "src/pages/UserDetail.tsx#UserDetail", "category": "all"},
        )
    assert excinfo.value.code == "evidence_out_of_scope"


async def test_page_query_does_not_cross_pages(sqlite_engine):
    graph_ = user_graph("an_pages")
    project_id, analysis_id = await _seed(
        sqlite_engine, graph_, analysis_id="an_pages"
    )
    list_page = "src/pages/UserList.tsx#UserList"
    detail_page = "src/pages/UserDetail.tsx#UserDetail"
    query = EvidenceQuery(
        sqlite_engine,
        project_id,
        analysis_id,
        page_nodes={list_page: (list_page,), detail_page: (detail_page,)},
    )

    result = await query.execute(
        "search_evidence", {"pageId": list_page, "category": "uiText"}
    )
    texts = {fact["text"] for fact in result["facts"]}
    assert "搜索用户" in texts
    assert "用户详情" not in texts
    assert all(fact["pageId"] == list_page for fact in result["facts"])


async def test_node_query_with_wrong_page_returns_empty(sqlite_engine):
    """pageId + nodeId 组合过滤：节点必须属于该页面（对齐 EvidenceSearch）。"""
    graph_ = user_graph("an_combo")
    project_id, analysis_id = await _seed(
        sqlite_engine, graph_, analysis_id="an_combo"
    )
    list_page = "src/pages/UserList.tsx#UserList"
    detail_page = "src/pages/UserDetail.tsx#UserDetail"
    query = EvidenceQuery(
        sqlite_engine,
        project_id,
        analysis_id,
        page_nodes={list_page: (list_page,), detail_page: (detail_page,)},
    )

    result = await query.execute(
        "search_evidence",
        {"pageId": list_page, "nodeId": detail_page, "category": "all"},
    )
    assert result["status"] == "ok"
    assert result["facts"] == []


async def test_unknown_page_is_out_of_scope(spec_query):
    query, _ = spec_query
    with pytest.raises(SpecDocToolError) as excinfo:
        await query.execute(
            "search_evidence", {"pageId": "not-a-page", "category": "all"}
        )
    assert excinfo.value.code == "evidence_out_of_scope"


async def test_invalid_category_and_tool_name_are_rejected(spec_query):
    query, _ = spec_query
    with pytest.raises(SpecDocToolError) as category_error:
        await query.execute("search_evidence", {"category": "magic"})
    assert category_error.value.code == "invalid_tool_arguments"

    with pytest.raises(SpecDocToolError) as tool_error:
        await query.execute("other_tool", {"category": "all"})
    assert tool_error.value.code == "unknown_tool"


# =====================================================================
# 边界：超长文本不崩
# =====================================================================


async def test_overlong_text_is_dropped_without_crash(sqlite_engine):
    graph_ = spec_graph("an_long")
    balance_text = next(
        node for node in graph_.nodes if node.id.endswith("jsx:span.balance")
    )
    balance_text.attrs["viewText"] = "超" * 2_500
    requirement_map = spec_requirement_map(graph_)
    materials = collect_materials(requirement_map, graph_)
    project_id, analysis_id = await _seed(
        sqlite_engine, graph_, analysis_id="an_long"
    )
    page_nodes, chain_nodes, chain_pages = _scope_maps(materials)
    query = EvidenceQuery(
        sqlite_engine, project_id, analysis_id,
        page_nodes=page_nodes, chain_nodes=chain_nodes, chain_pages=chain_pages,
    )

    result = await query.execute(
        "search_evidence", {"pageId": PAGE_ID, "category": "all"}
    )
    assert result["status"] == "ok"
    assert all(len(fact["text"]) <= 2_000 for fact in result["facts"])
    assert "超" * 2_500 not in {fact["text"] for fact in result["facts"]}


# =====================================================================
# 事实口径：与 EvidenceSearch 对齐
# =====================================================================


async def test_fact_parity_with_evidence_search_on_four_categories(sqlite_engine):
    graph_, materials = _form_materials()
    project_id, analysis_id = await _seed(
        sqlite_engine, graph_, analysis_id="an_form"
    )
    page_nodes, chain_nodes, chain_pages = _scope_maps(materials)
    query = EvidenceQuery(
        sqlite_engine, project_id, analysis_id,
        page_nodes=page_nodes, chain_nodes=chain_nodes, chain_pages=chain_pages,
    )
    search = EvidenceSearch(build_evidence_pack(materials).prompt_pages, EvidenceLedger())

    for category in ("uiText", "element", "form", "apiFact"):
        sql_result = await query.execute(
            "search_evidence", {"pageId": PAGE_ID, "category": category}
        )
        memory_result = await search.execute(
            "search_evidence", {"pageId": PAGE_ID, "category": category}
        )
        sql_facts = {
            (fact["pageId"], fact["nodeId"], fact["category"], fact["text"])
            for fact in sql_result["facts"]
        }
        memory_facts = {
            (fact["pageId"], fact["nodeId"], fact["category"], fact["text"])
            for fact in memory_result["facts"]
        }
        assert sql_facts == memory_facts, category


# =====================================================================
# 骨架：三个推断章节共用
# =====================================================================


async def test_build_skeleton_has_navigation_only(sqlite_engine):
    _, materials = _form_materials()
    skeleton = build_skeleton(materials, {"f1": (PAGE_ID,)})
    pages = skeleton["pages"]

    assert len(pages) == 1
    page = pages[0]
    assert set(page) == {
        "pageId", "name", "route", "featureIds", "counts", "forms", "apiFacts", "chains",
    }
    assert page["pageId"] == PAGE_ID
    assert page["route"] == "/transfer"
    assert page["featureIds"] == ["f1", "f2"]
    assert page["counts"] == {
        "elements": page["counts"]["elements"],
        "forms": 1,
        "apiFacts": 2,
        "chains": 2,
    }
    assert [form["nodeId"] for form in page["forms"]] == [
        "src/pages/Transfer.tsx#Transfer/jsx:select"
    ]
    assert {fact["nodeId"] for fact in page["apiFacts"]} == {
        "api:GET:/api/account/queryBalance",
        "api:POST:/api/transfer",
    }
    assert len(page["chains"]) == 2
    # 不含契约数据
    assert "uiText" not in page
    assert "elements" not in page


# =====================================================================
# Ledger / 空图
# =====================================================================


async def test_ledger_records_returned_facts_without_duplicates(spec_query):
    query, _ = spec_query
    assert len(query.ledger.facts) == 0
    await query.execute("search_evidence", {"pageId": PAGE_ID, "category": "uiText"})
    first = len(query.ledger.facts)
    assert first > 0
    await query.execute("search_evidence", {"pageId": PAGE_ID, "category": "uiText"})
    assert len(query.ledger.facts) == first


async def test_query_all_is_bounded(sqlite_engine):
    graph_ = user_graph("an_bound")
    project_id, analysis_id = await _seed(
        sqlite_engine, graph_, analysis_id="an_bound"
    )
    query = EvidenceQuery(sqlite_engine, project_id, analysis_id)
    result = await query.execute("search_evidence", {"category": "all"})
    assert result["status"] == "ok"
    assert len(result["facts"]) <= MAX_FACTS_PER_QUERY


async def test_empty_graph_returns_no_facts(sqlite_engine):
    from app.contracts import IRGraph

    empty_graph = IRGraph(version="1.1", analysisId="an_empty", nodes=[], edges=[])
    project_id, analysis_id = await _seed(
        sqlite_engine, empty_graph, project_id="prj", analysis_id="an_empty"
    )
    query = EvidenceQuery(sqlite_engine, project_id, analysis_id)
    result = await query.execute("search_evidence", {"category": "all"})
    assert result["status"] == "ok"
    assert result["facts"] == []


# =====================================================================
# P0-5：行表空/缺失时的兜底回退
# =====================================================================


async def test_graph_rows_exist_detects_projection(sqlite_engine):
    assert await graph_rows_exist(sqlite_engine, "prj", "an_none") is False

    graph_ = user_graph("an_rows")
    project_id, analysis_id = await _seed(
        sqlite_engine, graph_, analysis_id="an_rows"
    )
    assert await graph_rows_exist(sqlite_engine, project_id, analysis_id) is True


class _ChainToolLLM:
    """工具模式 LLM：步骤章节真实执行一次链路检索，其余章节返回空列表。"""

    def __init__(self, chain_id: str) -> None:
        self._chain_id = chain_id

    async def chat_with_tools(self, system, user, *, tools, executor, max_tool_calls):
        if "操作步骤" in system:
            result = await executor(
                "search_evidence", {"chainId": self._chain_id, "category": "chain"}
            )
            fact = result["facts"][0]
            return json.dumps({"steps": [{
                "featureId": "f1", "chainId": self._chain_id,
                "nodeId": fact["nodeId"], "actions": ["点击转账"],
                "quote": fact["text"], "confidence": 0.8,
            }]}, ensure_ascii=False)
        if "业务规则" in system:
            return json.dumps({"rules": []})
        return json.dumps({"fieldMappings": []})


async def test_infer_sections_runs_on_sql_rows(sqlite_engine):
    """engine 有行数据时走 EvidenceQuery，步骤章节能正常产出（SQL 端到端）。"""
    graph_ = spec_graph("an_sql")
    requirement_map = spec_requirement_map(graph_)
    materials = collect_materials(requirement_map, graph_)
    project_id, analysis_id = await _seed(
        sqlite_engine, graph_, analysis_id="an_sql"
    )
    chain = _button_chain(materials)

    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, materials,
        _ChainToolLLM(chain.id), max_calls=1,
        engine=sqlite_engine, project_id=project_id, analysis_id=analysis_id,
    )

    assert len(outcome.steps) == 1
    assert outcome.steps[0].evidence[0].nodeId in chain.node_ids
    assert not any(w.code == "evidence_sql_fallback" for w in outcome.warnings)


async def test_infer_falls_back_to_memory_when_rows_missing(sqlite_engine):
    """历史图没有行式投影：自动回退 EvidenceSearch，并留可见告警。"""
    graph_ = spec_graph("an_fallback")
    requirement_map = spec_requirement_map(graph_)
    materials = collect_materials(requirement_map, graph_)
    chain = _button_chain(materials)

    outcome = await infer_sections(
        requirement_map.requirement, requirement_map, materials,
        _ChainToolLLM(chain.id), max_calls=1,
        engine=sqlite_engine, project_id="prj_empty", analysis_id="an_fallback",
    )

    assert len(outcome.steps) == 1
    assert any(w.code == "evidence_sql_fallback" for w in outcome.warnings)
