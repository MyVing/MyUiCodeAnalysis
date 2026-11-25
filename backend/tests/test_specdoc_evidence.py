from __future__ import annotations

import json

from app.specdoc.collect import collect_materials
from app.specdoc.events import build_events
from app.specdoc.evidence import MAX_ELEMENTS_PER_PAGE, build_evidence_pack
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


def test_prompt_index_is_derived_from_exposed_page_payload() -> None:
    materials = _materials()
    api = next(iter(materials.pages[0].api_nodes.values()))
    api.attrs["requestBody"] = {"nested": {"ids": [1, 2]}, "name": "账户"}
    pack = build_evidence_pack(materials, _events_by_page(materials))
    payload, index = pack.prompt_view()
    page = json.loads(payload)["pages"][0]
    api_fact = next(item for item in page["apiFacts"] if item["nodeId"] == api.id)
    visible_request_body = json.dumps(
        api_fact["requestBody"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert visible_request_body in index[page["pageId"]][api.id]
    exposed_strings = set()

    def collect_strings(value):
        if isinstance(value, str):
            exposed_strings.add(value)
        elif isinstance(value, list):
            for item in value:
                collect_strings(item)
        elif isinstance(value, dict):
            for key, nested in value.items():
                exposed_strings.add(key)
                collect_strings(nested)

    exposed_payload = json.loads(payload)
    collect_strings(exposed_payload)
    for exposed_page in exposed_payload["pages"]:
        for fact in exposed_page["apiFacts"]:
            method, url = fact.get("method"), fact.get("url")
            if isinstance(method, str) and isinstance(url, str):
                exposed_strings.add(f"{method} {url}")
            request_body = fact.get("requestBody")
            if isinstance(request_body, (dict, list)):
                exposed_strings.add(json.dumps(
                    request_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ))
    indexed_texts = {
        text
        for by_node in index.values()
        for texts in by_node.values()
        for text in texts
    }
    assert indexed_texts.issubset(exposed_strings), indexed_texts - exposed_strings
    assert pack.prompt_pages[0] == page


def test_semantic_element_priority_preserves_form_and_matched_event() -> None:
    materials = _materials()
    material = materials.pages[0]
    for index in range(MAX_ELEMENTS_PER_PAGE + 5):
        material.elements.append(
            material.elements[0].__class__(
                node=material.elements[0].node.model_copy(
                    update={"id": f"extra:{index}", "attrs": {"tag": "div"}}
                )
            )
        )
    selected = next(item.node for item in material.elements if item.node.id.endswith("jsx:select"))
    selected.attrs["formField"] = {"name": "account", "quote": "付款账户"}
    pack = build_evidence_pack(materials, _events_by_page(materials))
    payload, index = pack.prompt_view()
    exposed = json.loads(payload)["pages"][0]
    assert any(item["nodeId"] == selected.id for item in exposed["elements"])
    assert "evidence_elements_truncated" in pack.prompt_warnings
    assert "付款账户" in index[exposed["pageId"]][selected.id]


def test_evidence_pack_has_no_code_refs_or_external_schema_fields() -> None:
    payload = json.loads(build_evidence_pack(_materials()).to_json())
    assert all("codeRefs" not in page for page in payload["pages"])
