"""契约层测试：shared/schemas 是唯一真源，必须自洽且与后端产物一致。"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "shared" / "schemas"
SCHEMA_NAMES = ["ir", "plugin", "runtime", "mapping", "requirement"]


def load_schema(name: str) -> dict[str, object]:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(load_schema(name))


def test_ir_schema_exposes_warnings_and_analysis_id() -> None:
    """文档 2.3 要求解析失败必须可见，图里必须有承载告警的字段。"""
    properties = load_schema("ir")["properties"]

    assert "warnings" in properties
    assert "analysisId" in properties


def test_ir_schema_promotes_file_and_loc_out_of_attrs() -> None:
    """attrs 是插件自由扩展区，file / loc 这类通用字段必须是一等字段。"""
    node = load_schema("ir")["$defs"]["IRNode"]

    assert "file" in node["properties"]
    assert "loc" in node["properties"]
    # file / loc 不参与 required，运行时节点与 api 节点可以没有源码位置
    assert "file" not in node["required"]
    assert "loc" not in node["required"]


def test_plugin_schema_carries_capabilities_and_dependencies() -> None:
    """文档 4.2：能力注册与依赖检查是插件体系的必须能力。"""
    manifest = load_schema("plugin")

    assert {"capabilities", "dependencies", "entrypoint", "schemaVersion"} <= set(
        manifest["properties"]
    )


def test_runtime_schema_separates_system_status_from_review() -> None:
    """文档 B.5：系统判定与人工确认结果必须分离，确认结果可复用。"""
    correlation = load_schema("runtime")["$defs"]["Correlation"]["properties"]

    assert set(correlation["status"]["enum"]) == {"matched", "candidate", "unmatched"}
    assert set(correlation["reviewStatus"]["enum"]) == {"pending", "confirmed", "rejected"}


def test_runtime_schema_keeps_large_artifacts_by_reference() -> None:
    """文档 B.7：DOM / 截图 / Trace 只能存引用，不能塞进 JSON。"""
    properties = load_schema("runtime")["properties"]

    for ref_field in ("domSnapshotRef", "screenshotRef", "traceRef"):
        assert properties[ref_field]["pattern"] == "^artifact://"


def test_requirement_schema_caps_ai_confidence_at_point_six() -> None:
    """AI 推断的置信度硬上限 0.6 必须由契约保证，不能只靠代码自觉。"""
    match = load_schema("requirement")["$defs"]["Match"]["properties"]

    assert match["confidence"]["maximum"] == 0.6
    # 模型自报值单独存，允许到 1.0，但它不参与 UI 排序
    assert match["modelConfidence"]["maximum"] == 1.0


def test_requirement_schema_stays_out_of_ir_graph() -> None:
    """需求映射是 AI 推断层，不得混入 IR 的事实层。"""
    ir_properties = load_schema("ir")["properties"]
    requirement_properties = load_schema("requirement")["properties"]

    assert "matches" not in ir_properties
    assert "matches" in requirement_properties
    assert "dryRun" in requirement_properties


def test_requirement_map_rejects_confidence_above_point_six() -> None:
    """契约测试要能真正挡住越界数据，而不只是断言 schema 里写了数字。"""
    from pydantic import ValidationError

    from app.contracts import Match, Subgraph, SubgraphCounts

    counts = SubgraphCounts(
        component=1, hook=0, state=0, api=0, event=0, fn=0, route=1, element=0
    )
    subgraph = Subgraph(nodeIds=["src/pages/UserList.tsx#UserList"], edgeIds=[], counts=counts)

    with pytest.raises(ValidationError):
        Match(
            featureId="f1",
            pageNodeId="src/pages/UserList.tsx#UserList",
            confidence=0.95,
            source="ai",
            subgraph=subgraph,
        )
