import json
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "shared" / "schemas" / "ir.schema.json"


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_sample_graph_matches_shared_schema(client, project) -> None:
    response = client.get(f"/api/v1/projects/{project['id']}/graph")

    assert response.status_code == 200
    graph = response.json()
    Draft202012Validator(_schema()).validate(graph)


def test_sample_graph_marks_itself_as_sample(client, project) -> None:
    graph = client.get(f"/api/v1/projects/{project['id']}/graph").json()

    assert graph["meta"]["sample"] is True
    assert graph["meta"]["projectId"] == project["id"]
    assert graph["meta"]["framework"] == "next"
    assert "示例" in graph["meta"]["note"]


def test_same_file_components_are_not_deduplicated(client, project) -> None:
    graph = client.get(f"/api/v1/projects/{project['id']}/graph").json()
    node_ids = {node["id"] for node in graph["nodes"]}

    assert "src/components/OrderTable.tsx#OrderTable" in node_ids
    assert "src/components/OrderTable.tsx#OrderRow" in node_ids


def test_next_projects_expose_rsc_nodes_and_ai_edge(client, project) -> None:
    graph = client.get(f"/api/v1/projects/{project['id']}/graph").json()
    kinds = {node["kind"] for node in graph["nodes"]}
    ai_edges = [
        edge for edge in graph["edges"] if any(item["source"] == "ai" for item in edge["evidence"])
    ]

    assert {"server-component", "server-action", "boundary"} & {
        edge["kind"] for edge in graph["edges"]
    }
    assert "server-component" in kinds
    assert ai_edges, "示例图需要包含 AI 推断边，便于前端区分事实与推断"
    assert all(item["confidence"] <= 0.6 for edge in ai_edges for item in edge["evidence"])


def test_runtime_evidence_omits_null_file_field(client, project) -> None:
    graph = client.get(f"/api/v1/projects/{project['id']}/graph").json()
    runtime_evidence = [
        evidence
        for node in graph["nodes"]
        for evidence in node["evidence"]
        if evidence["source"] == "runtime"
    ]

    assert runtime_evidence
    assert all("file" not in item for item in runtime_evidence)
    assert all(item["traceId"] for item in runtime_evidence)


def test_sample_graph_uses_1_1_contract(client, project) -> None:
    graph = client.get(f"/api/v1/projects/{project['id']}/graph").json()

    assert graph["version"] == "1.1"
    assert graph["analysisId"] == "an_sample"
    assert graph["warnings"] == []


def test_component_nodes_carry_top_level_file_not_attrs_file(client, project) -> None:
    """attrs 只放插件扩展；file 必须是一等字段，否则前端要靠隐式约定读取。"""
    graph = client.get(f"/api/v1/projects/{project['id']}/graph").json()
    component_nodes = [node for node in graph["nodes"] if node["kind"] not in {"api", "route"}]

    assert component_nodes
    for node in component_nodes:
        assert node["file"], f"{node['id']} 缺少顶层 file 字段"
        assert "file" not in node["attrs"], f"{node['id']} 仍把 file 塞在 attrs 里"


def test_api_node_may_omit_file(client, project) -> None:
    """对外接口节点没有单一源码位置，file 应可缺省而不是填一个假路径。"""
    graph = client.get(f"/api/v1/projects/{project['id']}/graph").json()
    api_node = next(node for node in graph["nodes"] if node["kind"] == "api")

    assert "file" not in api_node
    assert api_node["evidence"], "api 节点仍需保留调用点证据"
