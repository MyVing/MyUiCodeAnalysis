from __future__ import annotations

import json

from app.specdoc.collect import collect_materials
from app.specdoc.events import build_events
from app.specdoc.evidence import build_evidence_pack
from tests.specdoc_fixture import spec_graph, spec_requirement_map


def _materials():
    graph = spec_graph()
    return collect_materials(spec_requirement_map(graph), graph)


def _events_by_page(materials):
    return {
        material.page.pageId: build_events(material)[0]
        for material in materials.pages
    }


def test_evidence_pack_is_deterministic_and_structured() -> None:
    materials = _materials()
    first = build_evidence_pack(materials, _events_by_page(materials))
    second = build_evidence_pack(materials, _events_by_page(materials))

    assert first.to_json() == second.to_json()
    payload = json.loads(first.to_json())
    page = payload["pages"][0]
    assert page["chains"]
    assert page["events"]
    assert page["featureIds"] == ["f1"] + ["f2"]
    assert page["chains"] == sorted(page["chains"], key=lambda item: item["chainId"])
    expected = [event.id for event in _events_by_page(materials)[page["pageId"]]]
    assert [event["eventId"] for event in page["events"]] == expected
    assert page["route"] == "/transfer"


def test_prompt_text_index_includes_form_quotes_and_column_titles() -> None:
    materials = _materials()
    element = next(
        item.node for item in materials.pages[0].elements
        if item.node.id.endswith("jsx:select")
    )
    element.attrs["formField"] = {"name": "account", "quote": "账户是必填项"}
    element.attrs["columns"] = [{"title": "账户状态", "dataIndex": "status"}]

    pack = build_evidence_pack(materials, _events_by_page(materials))
    payload, index = pack.prompt_view()
    assert "账户是必填项" in payload
    assert "账户状态" in payload
    texts = index[materials.pages[0].page.pageId][element.id]
    assert "账户是必填项" in texts
    assert "账户状态" in texts


def test_evidence_pack_has_no_code_refs_or_external_schema_fields() -> None:
    payload = json.loads(build_evidence_pack(_materials()).to_json())
    assert all("codeRefs" not in page for page in payload["pages"])
