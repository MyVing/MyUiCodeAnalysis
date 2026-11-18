"""离线页面画像测试：确定性输入清单 + 模型输出校验 + 落库与降级。

模型一律用 FakeLLMClient。这个模块的边界是「画像不会污染校验」——
最关键的一组断言在 TestProfileIsNotEvidence：画像文本绝不能进语料。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.contracts import EdgeKind, NodeKind
from app.requirement.chains import derive_chains
from app.requirement.locate import build_page_index
from app.requirement.page_profile import PageProfile, ProfilePayload
from app.requirement.profile import (
    MAX_SUMMARY_CHARS,
    build_profile_hints,
    profile_pages,
)
from app.storage.memory import InMemoryAnalysisRepository
from tests.fakes import FakeLLMClient
from tests.graph_fixture import edge, graph, node, page, user_graph

_LIST_PAGE = "src/pages/UserList.tsx#UserList"
_DETAIL_PAGE = "src/pages/UserDetail.tsx#UserDetail"


def _profiles_response(*entries: tuple[str, str]) -> str:
    return json.dumps(
        {"profiles": [{"nodeId": page_id, "summary": summary} for page_id, summary in entries]},
        ensure_ascii=False,
    )


# --- 输入清单（纯确定性） ---------------------------------------------------


def test_hints_cover_every_page_with_routes_and_quotes() -> None:
    hints = {hint.node_id: hint for hint in build_profile_hints(user_graph())}

    assert set(hints) == {_LIST_PAGE, _DETAIL_PAGE}
    assert hints[_LIST_PAGE].route_paths == ("/users",)
    assert hints[_DETAIL_PAGE].route_paths == ("/users/:userId",)
    # 页面自有文案进清单：纯静态页面（没有链路）只靠这些描述自己
    assert "搜索用户" in hints[_LIST_PAGE].quotes


def test_hints_include_chains_when_present() -> None:
    """有交互的页面必须把交互名带上，否则模型没有依据说「这页能做什么」。"""
    orders = "src/pages/OrderList.tsx#OrderList"
    button = node(
        f"{orders}/jsx:button",
        NodeKind.element,
        "<button> 导出",
        file="src/pages/OrderList.tsx",
        attrs={"tag": "button", "viewRole": "control", "viewText": "导出"},
    )
    handler = node("src/pages/OrderList.tsx#handleExport", NodeKind.fn, "handleExport")
    route = node("src/router.tsx#/orders", NodeKind.route, "/orders", file="src/router.tsx")
    graph_ = graph(
        [page(orders, "OrderList"), button, handler, route],
        [
            edge(route.id, orders, EdgeKind.renders),
            edge(orders, button.id, EdgeKind.contains),
            edge(button.id, handler.id, EdgeKind.calls),
        ],
    )

    hints = {hint.node_id: hint for hint in build_profile_hints(graph_)}
    (name, role), *rest = hints[orders].chains
    assert name == "<button> 导出"
    assert role == "control"
    assert rest == []


def test_hints_prefilter_keeps_pages_with_more_interactions() -> None:
    """页面过多时优先保留交互多的页面：信息量大的页面才值得一次模型调用。"""
    hints = build_profile_hints(user_graph(), max_pages=1)
    assert len(hints) == 1


# --- 模型输出校验 -----------------------------------------------------------


async def test_profile_keeps_only_known_pages() -> None:
    llm = FakeLLMClient(
        [
            _profiles_response(
                (_LIST_PAGE, "用户列表页，支持查看用户基本信息与筛选。"),
                ("src/pages/Fake.tsx#Fake", "凭空捏造的页面。"),
                (_DETAIL_PAGE, "   "),  # 空白描述不算有效
            )
        ]
    )

    outcome = await profile_pages(user_graph(), llm, analysis_id="an_test")

    assert [profile.page_id for profile in outcome.payload.profiles] == [_LIST_PAGE]
    assert outcome.payload.profiles[0].name == "UserList"
    # 被拒的条数必须如实说明，不能静默丢掉
    assert any("2 条画像未通过校验" in warning for warning in outcome.payload.warnings)
    assert any("1 个页面没有生成描述" in warning for warning in outcome.payload.warnings)


async def test_profile_truncates_overlong_summary() -> None:
    llm = FakeLLMClient([_profiles_response((_LIST_PAGE, "长" * (MAX_SUMMARY_CHARS * 2)))])
    outcome = await profile_pages(user_graph(), llm, analysis_id="an_test")
    assert len(outcome.payload.profiles[0].summary) == MAX_SUMMARY_CHARS


async def test_profile_retries_invalid_json_then_succeeds() -> None:
    llm = FakeLLMClient(["不是 JSON", _profiles_response((_LIST_PAGE, "用户列表页。"))])
    outcome = await profile_pages(user_graph(), llm, analysis_id="an_test")
    assert outcome.degraded is False
    assert outcome.calls_used == 2
    assert outcome.payload.profiles


async def test_profile_without_pages_skips_model() -> None:
    empty = graph([], [])
    llm = FakeLLMClient()
    outcome = await profile_pages(empty, llm, analysis_id="an_empty")
    assert llm.calls == []
    assert outcome.degraded is True


async def test_profile_call_failure_degrades_without_raising() -> None:
    llm = FakeLLMClient()
    llm.error = __import__("app.requirement.llm", fromlist=["LLMCallError"]).LLMCallError(
        "HTTP 503"
    )
    outcome = await profile_pages(user_graph(), llm, analysis_id="an_test", max_calls=2)
    assert outcome.degraded is True
    assert outcome.payload.profiles == []
    assert any("画像生成失败" in warning for warning in outcome.payload.warnings)


# --- 画像存储 ---------------------------------------------------------------


async def test_profiles_are_scoped_to_analysis_version() -> None:
    """画像按图版本存放：图换了版本，旧画像不能拿来描述新页面。"""
    repository = InMemoryAnalysisRepository()
    payload = ProfilePayload(
        analysis_id="an_1",
        generated_at=datetime.now(UTC),
        profiles=[PageProfile(page_id=_LIST_PAGE, name="UserList", summary="用户列表页。")],
    )
    await repository.save_page_profiles("prj_1", payload)

    assert await repository.get_page_profiles("prj_1", "an_1") is not None
    assert await repository.get_page_profiles("prj_1", "an_2") is None


# --- 画像不得成为证据 -------------------------------------------------------


class TestProfileIsNotEvidence:
    """画像文本进 prompt 可以，进语料不行。

    否则模型先编一句「本页支持导出」，下一轮引用它就能通过 quote 校验——
    校验的对象是源码原文，不是模型自己写的描述。
    """

    def test_summary_reaches_prompt_payload(self) -> None:
        chains = derive_chains(user_graph())
        profile = ProfilePayload(
            analysis_id="an_test",
            generated_at=datetime.now(UTC),
            profiles=[
                PageProfile(
                    page_id=_LIST_PAGE,
                    name="UserList",
                    summary="用户列表页，支持查看与筛选。",
                )
            ],
        )
        index = build_page_index(user_graph(), chains, profile=profile)
        payload = index.by_id[_LIST_PAGE].to_payload()

        assert payload["summary"] == "用户列表页，支持查看与筛选。"

    def test_summary_never_enters_quote_corpus(self) -> None:
        chains = derive_chains(user_graph())
        summary = "本页支持导出 CSV 文件"
        profile = ProfilePayload(
            analysis_id="an_test",
            generated_at=datetime.now(UTC),
            profiles=[PageProfile(page_id=_LIST_PAGE, name="UserList", summary=summary)],
        )
        index = build_page_index(user_graph(), chains, profile=profile)

        corpus = index.corpus_of(None, _LIST_PAGE, chains)
        assert summary not in corpus
        assert all(summary not in text for text in corpus)

    def test_index_without_profile_has_no_summary(self) -> None:
        chains = derive_chains(user_graph())
        index = build_page_index(user_graph(), chains)
        assert "summary" not in index.by_id[_LIST_PAGE].to_payload()


# --- 端到端：服务层把画像接进定位 -------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
async def test_service_reads_profile_only_for_llm_runs(dry_run: bool) -> None:
    """dry-run 不读画像：它连模型都不调，没有理由去查一份模型产物。"""
    from datetime import datetime as _dt

    from app.core.config import Settings
    from app.requirement.service import RequirementLocateService
    from app.schemas import Project, RequirementLocateStart

    analysis_id = "an_test"
    graph_ = user_graph(analysis_id=analysis_id)
    analyses = InMemoryAnalysisRepository()
    await analyses.save_graph("prj_1", graph_)
    await analyses.save_page_profiles(
        "prj_1",
        ProfilePayload(
            analysis_id=analysis_id,
            generated_at=datetime.now(UTC),
            profiles=[PageProfile(page_id=_LIST_PAGE, name="UserList", summary="用户列表页。")],
        ),
    )

    from app.storage.memory import InMemoryRequirementRepository

    llm = FakeLLMClient(
        [
            # 定位服务不生成画像（那是分析服务的职责），它只读已存的画像。
            # 所以模型脚本里只有一次定位调用。
            json.dumps(
                {
                    "matches": [
                        {
                            "featureId": "f1",
                            "pageNodeId": _LIST_PAGE,
                            "quote": "搜索用户",
                            "confidence": 0.8,
                        }
                    ],
                    "unmatched": [],
                },
                ensure_ascii=False,
            )
        ]
    )
    settings = Settings(_env_file=None, llm_enabled=True, llm_api_key="k")
    service = RequirementLocateService(
        InMemoryRequirementRepository(), analyses, settings, llm=llm
    )
    project = Project(
        id="prj_1",
        name="订单平台",
        root_path="D:/work/orders",
        framework="next",
        created_at=_dt.now(UTC),
    )

    task = await service.start(
        project,
        RequirementLocateStart(requirement="用户列表增加状态筛选", dry_run=dry_run),
    )
    import asyncio

    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        current = await service.get_task(task.id)
        assert current is not None
        if current.status in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.01)

    assert current.status == "succeeded"
    if dry_run:
        assert llm.calls == []
        assert current.match_count == 0
    else:
        assert llm.calls, "非 dry-run 必须调模型"
        # 画像文本只出现在 user prompt 里，且不进任何校验语料
        user_prompt = llm.calls[0][1]
        assert "summary" in user_prompt
        assert current.match_count == 1
