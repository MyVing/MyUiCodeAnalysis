from __future__ import annotations

from app.contracts import NodeKind
from app.requirement.tools import RequirementAnalysisTools
from tests.graph_fixture import node, user_graph


def test_search_pages_and_interactions_stay_within_graph(tmp_path) -> None:
    graph = user_graph()
    tools = RequirementAnalysisTools(str(tmp_path), graph)

    pages = await_result(tools.execute("search_pages", {"query": "用户列表"}))
    assert pages["status"] == "ok"
    page_id = "src/pages/UserList.tsx#UserList"
    assert any(page["page_id"] == page_id for page in pages["pages"])

    chains = await_result(
        tools.execute("search_interactions", {"page_id": page_id, "query": "加载"})
    )
    assert chains["status"] == "ok"
    assert chains["chains"] == []  # no clickable element is present in this fixture
    out_of_scope = await_result(
        tools.execute("search_interactions", {"page_id": "unknown", "query": "x"})
    )
    assert out_of_scope["code"] == "out_of_scope"


def test_read_source_uses_trusted_node_range(tmp_path) -> None:
    source = tmp_path / "src" / "Page.tsx"
    source.parent.mkdir()
    source.write_text("first\nfunction handler() {\n  return true\n}\nlast\n", encoding="utf-8")
    graph = user_graph()
    target = node(
        "src/pages/UserList.tsx#handler",
        NodeKind.fn,
        "handler",
        file="src/Page.tsx",
        attrs={"endLine": 4},
    )
    graph.nodes.append(target)
    tools = RequirementAnalysisTools(str(tmp_path), graph)

    result = await_result(tools.execute("read_source_range", {"node_id": target.id}))
    assert result["status"] == "ok"
    assert result["start_line"] == 1 and result["end_line"] == 4
    assert "function handler()" in result["content"]


def test_read_source_rejects_missing_end_line_and_traversal(tmp_path) -> None:
    graph = user_graph()
    no_range = node("fn:no-range", NodeKind.fn, "missing", file="src/Page.tsx")
    traversal = node("fn:traversal", NodeKind.fn, "bad", file="../outside.ts", attrs={"endLine": 2})
    graph.nodes.extend([no_range, traversal])
    tools = RequirementAnalysisTools(str(tmp_path), graph)

    assert (
        await_result(tools.execute("read_source_range", {"node_id": no_range.id}))["code"]
        == "range_unavailable"
    )
    assert (
        await_result(tools.execute("read_source_range", {"node_id": traversal.id}))["code"]
        == "file_outside_project"
    )


def await_result(awaitable):
    import asyncio

    return asyncio.run(awaitable)
