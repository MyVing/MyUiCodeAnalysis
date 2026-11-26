"""Bounded on-demand evidence queries for specdoc inference."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.specdoc.llm import SpecDocToolError

MAX_FACTS_PER_QUERY = 12
MAX_TEXT_CHARS_PER_FACT = 2_000
MAX_LEDGER_CHARS = 12_000

EVIDENCE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_evidence",
        "description": "按已提供的页面、链路或节点 ID 查询该范围内的代码证据。",
        "parameters": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "chainId": {"type": "string"},
                "nodeId": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": ["chain", "element", "event", "uiText", "form", "apiFact", "all"],
                },
            },
            "required": ["category"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class EvidenceFact:
    evidence_id: str
    page_id: str
    node_id: str
    category: str
    text: str
    chain_id: str | None = None
    file: str | None = None
    line: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidenceId": self.evidence_id,
            "pageId": self.page_id,
            "nodeId": self.node_id,
            "chainId": self.chain_id,
            "category": self.category,
            "text": self.text,
            "file": self.file,
            "line": self.line,
        }


# 跨章节共享的查询结果缓存：键 = (pageId, chainId, nodeId, category)。
# 三个章节各自持有独立 ledger（预算独立），但同一批事实无需重复检索/SQL。
QueryCache = dict[tuple[str | None, str | None, str | None, str], list[EvidenceFact]]


@dataclass
class EvidenceLedger:
    """Only facts actually returned to the model are eligible for validation."""

    facts: dict[str, EvidenceFact] = field(default_factory=dict)
    chars_used: int = 0

    def record(self, facts: list[EvidenceFact]) -> list[EvidenceFact]:
        accepted: list[EvidenceFact] = []
        for fact in facts:
            if fact.evidence_id in self.facts:
                accepted.append(self.facts[fact.evidence_id])
                continue
            if self.chars_used + len(fact.text) > MAX_LEDGER_CHARS:
                break
            self.facts[fact.evidence_id] = fact
            self.chars_used += len(fact.text)
            accepted.append(fact)
        return accepted

    def texts_by_node(self) -> dict[str, tuple[str, ...]]:
        values: dict[str, list[str]] = {}
        for fact in self.facts.values():
            texts = values.setdefault(fact.node_id, [])
            if fact.text not in texts:
                texts.append(fact.text)
        return {node_id: tuple(texts) for node_id, texts in values.items()}


class EvidenceSearch:
    """Read-only search over an allowlisted, already constructed evidence projection."""

    def __init__(
        self,
        pages: tuple[dict[str, Any], ...],
        ledger: EvidenceLedger,
        allowed_page_ids: set[str] | None = None,
        *,
        query_cache: QueryCache | None = None,
    ) -> None:
        self._facts: list[EvidenceFact] = []
        self._pages: dict[str, dict[str, Any]] = {}
        self.ledger = ledger
        # 由调用方注入以跨章节复用；缺省时退化为实例内缓存
        self._query_cache: QueryCache = query_cache if query_cache is not None else {}
        for page in pages:
            page_id = page.get("pageId")
            if not isinstance(page_id, str):
                continue
            self._pages[page_id] = page
            if allowed_page_ids is not None and page_id not in allowed_page_ids:
                continue
            for text in page.get("uiText", []):
                self._add(page_id, page_id, "uiText", text)
            for element in page.get("elements", []):
                if not isinstance(element, dict):
                    continue
                node_id = element.get("nodeId")
                self._add(page_id, node_id, "element", element.get("viewText"))
                field = element.get("formField")
                if isinstance(field, dict):
                    self._add(page_id, node_id, "element", field.get("quote"))
                for column in element.get("columns", []):
                    if isinstance(column, dict):
                        self._add(page_id, node_id, "element", column.get("title"))
            for event in page.get("events", []):
                if not isinstance(event, dict):
                    continue
                self._add(page_id, event.get("elementId"), "event", event.get("handler"))
                targets = event.get("targets")
                if isinstance(targets, list):
                    for target in targets:
                        if isinstance(target, dict):
                            self._add(page_id, target.get("id"), "event", event.get("handler"))
            for form in page.get("forms", []):
                if isinstance(form, dict):
                    field_info = form.get("frontField")
                    if isinstance(field_info, dict):
                        self._add(page_id, form.get("nodeId"), "form", field_info.get("quote"))
            for fact in page.get("apiFacts", []):
                if not isinstance(fact, dict):
                    continue
                node_id = fact.get("nodeId")
                method, url = fact.get("method"), fact.get("url")
                self._add(page_id, node_id, "apiFact", method)
                self._add(page_id, node_id, "apiFact", url)
                if isinstance(method, str) and isinstance(url, str):
                    self._add(page_id, node_id, "apiFact", f"{method} {url}")
                request_body = fact.get("requestBody")
                if isinstance(request_body, (dict, list)):
                    self._add(
                        page_id, node_id, "apiFact",
                        json.dumps(request_body, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")),
                    )

                def add_body_text(
                    value: Any, current_page_id: str = page_id,
                    current_node_id: Any = node_id,
                ) -> None:
                    if isinstance(value, dict):
                        for key, nested in value.items():
                            self._add(current_page_id, current_node_id, "apiFact", key)
                            add_body_text(nested, current_page_id, current_node_id)
                    elif isinstance(value, list):
                        for nested in value:
                            add_body_text(nested, current_page_id, current_node_id)
                    else:
                        self._add(current_page_id, current_node_id, "apiFact", value)

                add_body_text(request_body)
            for chain in page.get("chains", []):
                if not isinstance(chain, dict):
                    continue
                chain_id = chain.get("chainId")
                for step in chain.get("steps", []):
                    if not isinstance(step, dict):
                        continue
                    for text in step.get("texts", []):
                        self._add(
                            page_id, step.get("nodeId"), "chain", text,
                            chain_id=chain_id, file=step.get("file"),
                            line=(step.get("loc") or {}).get("line"),
                        )

    def _add(
        self, page_id: Any, node_id: Any, category: str, text: Any,
        *, chain_id: Any = None, file: Any = None, line: Any = None,
    ) -> None:
        if not isinstance(page_id, str) or not isinstance(node_id, str):
            return
        if not isinstance(text, str) or not text.strip():
            return
        if len(text) > MAX_TEXT_CHARS_PER_FACT:
            return
        identity = json.dumps(
            [page_id, node_id, chain_id, category, text], ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        self._facts.append(
            EvidenceFact(
                evidence_id=f"ev_{digest}", page_id=page_id,
                node_id=node_id, category=category, text=text,
                chain_id=chain_id if isinstance(chain_id, str) else None,
                file=file if isinstance(file, str) else None,
                line=line if isinstance(line, int) else None,
            )
        )

    def skeleton(self, feature_pages: dict[str, tuple[str, ...]]) -> dict[str, Any]:
        included = {page_id for values in feature_pages.values() for page_id in values}
        return {
            "pages": [
                {
                    "pageId": page_id,
                    "name": page.get("name"),
                    "route": page.get("route"),
                    "forms": [
                        {"nodeId": form.get("nodeId")}
                        for form in page.get("forms", [])
                        if isinstance(form, dict)
                    ],
                    "apiFacts": [
                        {
                            "nodeId": fact.get("nodeId"),
                            "method": fact.get("method"),
                            "url": fact.get("url"),
                        }
                        for fact in page.get("apiFacts", [])
                        if isinstance(fact, dict)
                    ],
                    "chains": [
                        {
                            "chainId": chain.get("chainId"),
                            "elementId": chain.get("elementId"),
                            "steps": [
                                {
                                    key: step.get(key)
                                    for key in ("role", "name", "nodeId", "file", "loc")
                                }
                                for step in chain.get("steps", [])
                            ],
                        }
                        for chain in page.get("chains", [])
                    ],
                }
                for page_id, page in self._pages.items() if page_id in included
            ]
        }

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if set(arguments) - {"pageId", "chainId", "nodeId", "category"}:
            raise SpecDocToolError("证据检索参数包含未允许字段", code="invalid_tool_arguments")
        if name != "search_evidence":
            raise SpecDocToolError("未知的 specdoc 证据工具", code="unknown_tool")
        category = arguments.get("category")
        if category not in {"chain", "element", "event", "uiText", "form", "apiFact", "all"}:
            raise SpecDocToolError("证据类别无效", code="invalid_tool_arguments")
        page_id, chain_id, node_id = (
            arguments.get("pageId"), arguments.get("chainId"), arguments.get("nodeId")
        )
        if page_id is not None and page_id not in self._pages:
            raise SpecDocToolError("页面不在本次证据范围内", code="evidence_out_of_scope")
        if node_id is not None and not any(fact.node_id == node_id for fact in self._facts):
            raise SpecDocToolError("节点不在本次证据范围内", code="evidence_out_of_scope")
        if chain_id is not None and not any(fact.chain_id == chain_id for fact in self._facts):
            raise SpecDocToolError("链路不在本次证据范围内", code="evidence_out_of_scope")
        facts = self._query_cache.get((page_id, chain_id, node_id, category))
        if facts is None:
            facts = [
                fact for fact in self._facts
                if (page_id is None or fact.page_id == page_id)
                and (chain_id is None or fact.chain_id == chain_id)
                and (node_id is None or fact.node_id == node_id)
                and (category == "all" or fact.category == category)
            ]
            self._query_cache[(page_id, chain_id, node_id, category)] = facts
        if chain_id is not None and not any(fact.chain_id == chain_id for fact in self._facts):
            raise SpecDocToolError("链路不在本次证据范围内", code="evidence_out_of_scope")
        matched = facts[:MAX_FACTS_PER_QUERY]
        accepted = self.ledger.record(matched)
        if len(accepted) < len(matched):
            return {"status": "budget_exhausted", "facts": [item.as_dict() for item in accepted]}
        return {"status": "ok", "facts": [item.as_dict() for item in accepted]}
