from __future__ import annotations

from app.specdoc.llm import SpecDocToolError
from app.specdoc.retrieval import EvidenceLedger, EvidenceSearch


def _pages():
    return ({
        "pageId": "page-1",
        "name": "转账页",
        "route": "/transfer",
        "uiText": ["请选择付款账户"],
        "elements": [
            {
                "nodeId": "button-1",
                "viewText": "提交转账",
                "formField": None,
                "columns": [],
            }
        ],
        "events": [],
        "chains": [
            {
                "chainId": "chain-1",
                "elementId": "button-1",
                "steps": [
                    {
                        "nodeId": "button-1",
                        "role": "element",
                        "name": "转账",
                        "file": "Transfer.tsx",
                        "loc": {"line": 12},
                        "texts": ["点击转账"],
                    }
                ],
            }
        ],
    },)


async def test_search_records_only_returned_facts_and_rejects_out_of_scope_ids() -> None:
    ledger = EvidenceLedger()
    search = EvidenceSearch(_pages(), ledger)
    result = await search.execute("search_evidence", {
        "pageId": "page-1", "chainId": "chain-1", "category": "chain"
    })
    assert result["status"] == "ok"
    fact = result["facts"][0]
    assert fact["text"] == "点击转账"
    assert set(ledger.facts) == {fact["evidenceId"]}

    try:
        await search.execute("search_evidence", {"pageId": "other", "category": "all"})
    except SpecDocToolError as error:
        assert error.code == "evidence_out_of_scope"
    else:
        raise AssertionError("out-of-scope page must be rejected")


async def test_search_budget_is_explicit() -> None:
    ledger = EvidenceLedger(chars_used=12_000)
    search = EvidenceSearch(_pages(), ledger)
    result = await search.execute("search_evidence", {
        "pageId": "page-1", "category": "uiText"
    })
    assert result["status"] == "budget_exhausted"
    assert result["facts"] == []


async def test_forms_and_api_facts_have_separate_search_categories() -> None:
    page = dict(_pages()[0])
    page["forms"] = [{"nodeId": "button-1", "frontField": {"quote": "付款账户"}}]
    page["apiFacts"] = [{
        "nodeId": "api-1", "method": "POST", "url": "/transfer",
        "requestBody": {"accountId": "付款账户"},
    }]
    search = EvidenceSearch((page,), EvidenceLedger())

    forms = await search.execute("search_evidence", {
        "pageId": "page-1", "nodeId": "button-1", "category": "form"
    })
    api = await search.execute("search_evidence", {
        "pageId": "page-1", "nodeId": "api-1", "category": "apiFact"
    })

    assert [fact["text"] for fact in forms["facts"]] == ["付款账户"]
    assert "POST /transfer" in [fact["text"] for fact in api["facts"]]
    assert '{"accountId":"付款账户"}' in [fact["text"] for fact in api["facts"]]
