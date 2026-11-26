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
    warnings: tuple[str, ...] = ()
    _prompt_warnings: tuple[str, ...] = field(
        default=(), init=False, repr=False, compare=False
    )
    _prompt_pages: tuple[dict[str, Any], ...] = field(
        default=(), init=False, repr=False, compare=False
    )

    def _serialized(self, pages: list[dict[str, Any]]) -> str:
        return json.dumps({"pages": pages}, ensure_ascii=False, separators=(",", ":"))

    def to_json(self) -> str:
        return self._serialized(list(self.pages))

    def prompt_view(self) -> tuple[str, dict[str, dict[str, tuple[str, ...]]]]:
        """Return the bounded payload and exactly the index derived from its visible facts."""
        pages = [json.loads(json.dumps(page, ensure_ascii=False)) for page in self.pages]
        warnings = set(self.warnings)
        encoded = self._serialized(pages)
        if len(encoded) > MAX_PACK_CHARS:
            for page in pages:
                if page.get("forms"):
                    page["forms"] = []
                    warnings.add("evidence_forms_truncated")
                if page.get("apiFacts"):
                    page["apiFacts"] = []
                    warnings.add("evidence_api_facts_truncated")
            encoded = self._serialized(pages)
        if len(encoded) > MAX_PACK_CHARS:
            for page in pages:
                chains = page.get("chains", [])
                if len(chains) > 20:
                    page["chains"] = chains[:20]
                    warnings.add("evidence_chains_truncated")
            encoded = self._serialized(pages)
        if len(encoded) > MAX_PACK_CHARS:
            for page in pages:
                elements = page.get("elements", [])
                events = page.get("events", [])
                if len(elements) > 20:
                    page["elements"] = elements[:20]
                    warnings.add("evidence_elements_truncated")
                if len(events) > 20:
                    page["events"] = events[:20]
                    warnings.add("evidence_events_truncated")
            encoded = self._serialized(pages)
        if len(encoded) > MAX_PACK_CHARS:
            for page in pages:
                if page.get("uiText"):
                    page["uiText"] = []
                    warnings.add("evidence_ui_text_truncated")
                for key in ("chains", "elements", "events"):
                    if page.get(key):
                        page[key] = []
                        warnings.add(f"evidence_{key}_truncated")
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
                node_id = element.get("nodeId")
                add(node_id, element.get("viewText"))
                for column in element.get("columns", []):
                    if isinstance(column, dict):
                        add(node_id, column.get("title"))
                form_field = element.get("formField")
                if isinstance(form_field, dict):
                    add(node_id, form_field.get("quote"))
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
                method, url = fact.get("method"), fact.get("url")
                if isinstance(method, str) and isinstance(url, str):
                    add(node_id, f"{method} {url}")
                request_body = fact.get("requestBody")
                if isinstance(request_body, (dict, list)):
                    add(
                        node_id,
                        json.dumps(
                            request_body, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )

                def add_body_text(value: Any, target_node_id: str = node_id) -> None:
                    if isinstance(value, dict):
                        for key, nested in value.items():
                            add(target_node_id, key)
                            add_body_text(nested, target_node_id)
                    elif isinstance(value, list):
                        for nested in value:
                            add_body_text(nested, target_node_id)
                    else:
                        add(target_node_id, value)

                add_body_text(request_body)
            text_index[page_id] = {key: tuple(items) for key, items in values.items()}
        object.__setattr__(self, "_prompt_warnings", tuple(sorted(warnings)))
        object.__setattr__(self, "_prompt_pages", tuple(pages))
        return encoded, text_index

    def prompt_json(self) -> str:
        return self.prompt_view()[0]

    @property
    def prompt_warnings(self) -> tuple[str, ...]:
        self.prompt_view()
        return self._prompt_warnings

    @property
    def prompt_pages(self) -> tuple[dict[str, Any], ...]:
        self.prompt_view()
        return self._prompt_pages



def build_skeleton(
    materials: Materials,
    feature_pages: dict[str, tuple[str, ...]] | None = None,
    matched_chain_ids: set[str] | None = None,
) -> dict[str, Any]:
    """构造轻量导航骨架：只含定位信息，不含契约数据。

    三个推断章节共用同一份骨架，不再依赖具体检索器（SQL / 内存）实现，
    避免 EvidenceQuery 与 EvidenceSearch 各出一套结构。
    """
    included: set[str] | None = None
    if feature_pages is not None:
        included = {
            page_id
            for values in feature_pages.values()
            for page_id in values
        }
    pages: list[dict[str, Any]] = []
    for material in materials.pages:
        page = material.page
        if included is not None and page.pageId not in included:
            continue
        form_node_ids = sorted(
            source.node.id
            for source in material.elements
            if isinstance(source.node.attrs.get("formField"), dict)
        )
        api_facts = [
            {
                "nodeId": api_id,
                "method": api.attrs.get("method"),
                "url": api.attrs.get("url"),
            }
            for api_id, api in sorted(material.api_nodes.items())
        ]
        chains = [
            {
                "chainId": chain.id,
                "elementId": chain.element.node_id,
                "steps": [
                    {"role": step.role, "name": step.name, "nodeId": step.node_id}
                    for step in chain.steps
                ],
            }
            for chain in material.chains
            if matched_chain_ids is None or chain.id in matched_chain_ids
        ]
        pages.append(
            {
                "pageId": page.pageId,
                "name": page.name,
                "route": material.route_path,
                "featureIds": sorted(page.featureIds or []),
                "counts": {
                    "elements": len(material.elements),
                    "forms": len(form_node_ids),
                    "apiFacts": len(api_facts),
                    "chains": len(chains),
                },
                "forms": [{"nodeId": node_id} for node_id in form_node_ids],
                "apiFacts": api_facts,
                "chains": chains,
            }
        )
    return {"pages": pages}


def build_evidence_pack(
    materials: Materials,
    events_by_page: dict[str, list[Event]] | None = None,
) -> EvidencePack:
    """Project existing IR-derived materials into a stable, bounded JSON payload."""
    page_values: list[dict[str, Any]] = []
    warnings: list[str] = []
    for material in sorted(
        materials.pages, key=lambda item: (item.page.pageNo, item.page.pageId)
    ):
        page, node = material.page, material.node
        if len(material.elements) > MAX_ELEMENTS_PER_PAGE:
            warnings.append("evidence_elements_truncated")
        if len(material.chains) > MAX_CHAINS_PER_PAGE:
            warnings.append("evidence_chains_truncated")
        event_element_ids = {
            event.elementId for event in (events_by_page or {}).get(page.pageId, [])
        }
        matched_elements = {
            chain.element.node_id
            for chain in material.chains
            if chain.id in set(material.matched_chain_ids)
        }
        element_sources = sorted(
            material.elements,
            key=lambda item: (
                not isinstance(item.node.attrs.get("formField"), dict),
                item.node.id not in event_element_ids,
                not bool(item.matched_features),
                item.node.id not in matched_elements,
                item.node.id,
            ),
        )
        if len(element_sources) > MAX_ELEMENTS_PER_PAGE:
            if any(isinstance(item.node.attrs.get("formField"), dict)
                   for item in element_sources[MAX_ELEMENTS_PER_PAGE:]):
                warnings.append("evidence_forms_truncated")
            if any(item.node.id in event_element_ids
                   for item in element_sources[MAX_ELEMENTS_PER_PAGE:]):
                warnings.append("evidence_events_truncated")
        elements = element_sources[:MAX_ELEMENTS_PER_PAGE]
        matched = set(material.matched_chain_ids)
        chains = sorted(
            material.chains, key=lambda item: (item.id not in matched, item.id)
        )[:MAX_CHAINS_PER_PAGE]
        element_values: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        forms: list[dict[str, Any]] = []
        texts: dict[str, list[str]] = {}

        def add_text(target: dict[str, list[str]], node_id: Any, value: Any) -> None:
            if isinstance(node_id, str) and isinstance(value, str) and value.strip():
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
            if len(events_by_page.get(page.pageId, [])) > 20:
                warnings.append("evidence_events_truncated")
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

    encoded = json.dumps({"pages": page_values}, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_PACK_CHARS:
        warnings.append("evidence_pack_truncated")
    return EvidencePack(tuple(page_values), tuple(warnings))
