from __future__ import annotations

import json

from app.specdoc.collect import collect_materials
from app.specdoc.evidence import build_evidence_pack
from tests.specdoc_fixture import spec_graph, spec_requirement_map


def _materials():
    graph = spec_graph()
    return collect_materials(spec_requirement_map(graph), graph)


def test_evidence_pack_is_deterministic_and_structured() -> None:
    materials = _materials()
    first = build_evidence_pack(materials)
    second = build_evidence_pack(materials)

    assert first.to_json() == second.to_json()
    payload = json.loads(first.to_json())
    page = payload["pages"][0]
    assert page["chains"]
    assert page["events"]
    assert page["featureIds"] == ["f1"] + ["f2"]
    assert page["chains"] == sorted(page["chains"], key=lambda item: item["chainId"])


def test_evidence_pack_has_no_code_refs_or_external_schema_fields() -> None:
    payload = json.loads(build_evidence_pack(_materials()).to_json())
    assert all("codeRefs" not in page for page in payload["pages"])
