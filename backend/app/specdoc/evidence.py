"""Deterministic, structured evidence projection for specdoc inference."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.contracts.specdoc import Event
from app.specdoc.collect import Materials

MAX_ELEMENTS_PER_PAGE = 80
MAX_CHAINS_PER_PAGE = 40
MAX_PACK_CHARS = 60_000


@dataclass(frozen=True)
class EvidencePack:
    """Prompt projection plus only the text that was actually exposed to the model."""

    pages: tuple[dict[str, Any], ...]
    texts_by_page_node: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def _serialized(self, pages: list[dict[str, Any]]) -> str:
        return json.dumps({"pages": pages}, ensure_ascii=False, separators=(",", ":"))

    def to_json(self) -> str:
        return self._serialized(list(self.pages))

    def prompt_view(self) -> tuple[str, dict[str, dict[str, tuple[str, ...]]]]:
        """Return the bounded payload and exactly the indexed text it exposes."""
        pages = [dict(page) for page in self.pages]
        encoded = self._serialized(pages)
        if len(encoded) > MAX_PACK_CHARS:
            for page in pages:
                page.pop("forms", None)
                page.pop("apiFacts", None)
            encoded = self._serialized(pages)
        if len(encoded) > MAX_PACK_CHARS:
            for page in pages:
                page["chains"] = page["chains"][:20]
            encoded = self._serialized(pages)
        if len(encoded) > MAX_PACK_CHARS:
            for page in pages:
                page["elements"] = page["elements"][:20]
                page["events"] = page["events"][:20]
            encoded = self._serialized(pages)
        if len(encoded) > MAX_PACK_CHARS:
            for page in pages:
                page["uiText"] = []
                page["chains"] = []
                page["elements"] = []
                page["events"] = []
            encoded = self._serialized(pages)

        text_index: dict[str, dict[str, tuple[str, ...]]] = {}
        for page in pages:
            values: dict[str, list[str]] = {}

            def add(node_id: Any, text: Any, target: dict[str, list[str]] = values) -> None:
                if isinstance(node_id, str) and isinstance(text, str) and text.strip():
                    current = target.setdefault(node_id, [])
                    if text not in current:
                        current.append(text)

            page_id = page.get("pageId")
            if not isinstance(page_id, str):
                continue
            for text in page.get("uiText", []):
                add(page_id, text)
            for element in page.get("elements", []):
                add(element.get("nodeId"), element.get("viewText"))
                for column in element.get("columns", []):
                    if isinstance(column, dict):
                        add(element.get("nodeId"), column.get("title"))
            for form in page.get("forms", []):
                field_info = form.get("frontField")
                if isinstance(field_info, dict):
                    add(form.get("nodeId"), field_info.get("quote"))
            for event in page.get("events", []):
                targets = event.get("targets")
                target_id = next(
                    (target.get("id") for target in targets or []
                     if isinstance(target, dict) and isinstance(target.get("id"), str)),
                    event.get("elementId"),
                )
                add(target_id, event.get("handler"))
            for chain in page.get("chains", []):
                for step in chain.get("steps", []):
                    for text in step.get("texts", []):
                        add(step.get("nodeId"), text)
            for fact in page.get("apiFacts", []):
                node_id = fact.get("nodeId")
                add(node_id, fact.get("method"))
                add(node_id, fact.get("url"))
                request_body = fact.get("requestBody")
                if isinstance(request_body, str):
                    add(node_id, request_body)
                elif isinstance(request_body, dict):
                    for key, value in sorted(request_body.items()):
                        add(node_id, f"{key}: {value}")
            text_index[page_id] = {key: tuple(items) for key, items in values.items()}
        return encoded, text_index

    def prompt_json(self) -> str:
        """Apply deterministic truncation to the serialized prompt view."""
        return self.prompt_view()[0]


def build_evidence_pack(
    materials: Materials,
    events_by_page: dict[str, list[Event]] | None = None,
) -> EvidencePack:
    """Project existing IR-derived materials into a stable, bounded JSON payload."""
    page_values: list[dict[str, Any]] = []
    texts_by_page_node: dict[str, dict[str, tuple[str, ...]]] = {}
    warnings: list[str] = []
    for material in sorted(
        materials.pages, key=lambda item: (item.page.pageNo, item.page.pageId)
    ):
        page, node = material.page, material.node
        elements = sorted(material.elements, key=lambda item: item.node.id)[
            :MAX_ELEMENTS_PER_PAGE
        ]
        chains = sorted(material.chains, key=lambda item: item.id)
        matched = set(material.matched_chain_ids)
        chains = sorted(chains, key=lambda item: (item.id not in matched, item.id))[
            :MAX_CHAINS_PER_PAGE
        ]
        element_values: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        forms: list[dict[str, Any]] = []
        texts: dict[str, list[str]] = {}

        def add_text(target: dict[str, list[str]], node_id: Any, value: Any) -> None:
            if isinstance(value, str) and value.strip():
                values = target.setdefault(node_id, [])
                if value not in values:
                    values.append(value)

        ui_text = node.attrs.get("uiText")
        page_texts = []
        for entry in (ui_text.get("texts") if isinstance(ui_text, dict) else []) or []:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                page_texts.append(entry["text"])
                add_text(texts, node.id, entry["text"])

        for source in elements:
            attrs = source.node.attrs
            columns = attrs.get("columns")
            exposed_columns = (
                [column for column in columns if isinstance(column, dict)]
                if isinstance(columns, list) else []
            )
            element_values.append(
                {
                    "nodeId": source.node.id,
                    "tag": attrs.get("tag"),
                    "viewText": attrs.get("viewText"),
                    "viewRole": attrs.get("viewRole"),
                    "formField": attrs.get("formField"),
                    "columns": exposed_columns,
                }
            )
            add_text(texts, source.node.id, attrs.get("viewText"))
            for column in exposed_columns:
                add_text(texts, source.node.id, column.get("title"))
            field_info = attrs.get("formField")
            if isinstance(field_info, dict):
                forms.append({"nodeId": source.node.id, "frontField": field_info})
                add_text(texts, source.node.id, field_info.get("quote"))
            if events_by_page is None:
                bindings = attrs.get("events")
                if isinstance(bindings, list):
                    for index, binding in enumerate(bindings):
                        if not isinstance(binding, dict):
                            continue
                        handler = binding.get("handler")
                        events.append(
                            {
                                "eventId": f"{source.node.id}::evt:{index + 1}",
                                "elementId": source.node.id,
                                "eventType": binding.get("name"),
                                "handlerName": binding.get("handlerName"),
                                "handler": handler,
                                "targets": binding.get("targets"),
                            }
                        )
                        target_node = next(
                            (target.get("id") for target in binding.get("targets", []) or []
                             if isinstance(target, dict) and isinstance(target.get("id"), str)),
                            source.node.id,
                        )
                        add_text(texts, target_node, handler)

        if events_by_page is not None:
            for event in events_by_page.get(page.pageId, []):
                source = next((item for item in elements if item.node.id == event.elementId), None)
                bindings = source.node.attrs.get("events", []) if source else []
                if not isinstance(bindings, list):
                    continue
                for binding in bindings:
                    if not isinstance(binding, dict):
                        continue
                    handler_text = binding.get("handler")
                    if not isinstance(handler_text, str):
                        continue
                    targets = binding.get("targets")
                    target_node = next(
                        (target.get("id") for target in targets or []
                         if isinstance(target, dict) and isinstance(target.get("id"), str)),
                        event.elementId,
                    )
                    add_text(texts, target_node, handler_text)
            for event in events_by_page.get(page.pageId, []):
                events.append(
                    {
                        "eventId": event.id,
                        "chainId": event.chainId,
                        "elementId": event.elementId,
                        "eventType": event.eventType,
                        "handler": event.handler.name,
                        "handlerFile": event.handler.file,
                        "handlerLoc": (
                            {"line": event.handler.loc.line, "col": event.handler.loc.col}
                            if event.handler.loc is not None else None
                        ),
                        "httpCalls": [
                            {"method": call.method, "url": call.url}
                            for call in event.httpCalls
                        ],
                    }
                )

        chain_values: list[dict[str, Any]] = []
        for chain in chains:
            chain_values.append(
                {
                    "chainId": chain.id,
                    "elementId": chain.element.node_id,
                    "terminatedAt": chain.terminated_at,
                    "steps": [
                        {
                            "role": step.role,
                            "name": step.name,
                            "nodeId": step.node_id,
                            "file": step.file,
                            "loc": (
                                {"line": step.line, "col": step.col}
                                if step.line
                                else None
                            ),
                            "texts": list(
                                next(
                                    (fragments for node_id, fragments in chain.step_corpus
                                     if node_id == step.node_id),
                                    (),
                                )
                            ),
                        }
                        for step in chain.steps
                    ],
                }
            )
            for node_id, fragments in chain.step_corpus:
                for text in fragments:
                    add_text(texts, node_id, text)

        page_value = {
            "pageNo": page.pageNo,
            "pageId": page.pageId,
            "name": page.name,
            "file": page.file,
            "route": material.route_path,
            "uiText": page_texts,
            "featureIds": sorted(page.featureIds or []),
            "elements": element_values,
            "events": sorted(events, key=lambda item: item["eventId"]),
            "chains": chain_values,
            "forms": sorted(forms, key=lambda item: item["nodeId"]),
            "apiFacts": [
                {
                    "nodeId": api_id,
                    "method": api.attrs.get("method"),
                    "url": api.attrs.get("url"),
                    "requestBody": api.attrs.get("requestBody"),
                }
                for api_id, api in sorted(material.api_nodes.items())
            ],
        }
        page_values.append(page_value)
        exposed_nodes = {node.id, *(source.node.id for source in elements)}
        exposed_nodes.update(step.node_id for chain in chains for step in chain.steps)
        texts_by_page_node[page.pageId] = {
            key: tuple(value) for key, value in texts.items() if key in exposed_nodes
        }

    encoded = json.dumps({"pages": page_values}, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_PACK_CHARS:
        warnings.append("evidence_pack_truncated")
    return EvidencePack(tuple(page_values), texts_by_page_node, tuple(warnings))
