"""确定性装配测试：三分类判定、事件拆解、接口去重、编号规则。"""

from __future__ import annotations

from app.contracts import EdgeKind, NodeKind
from app.contracts.specdoc import ElementKind, Source
from app.specdoc.apimap import build_api_mappings
from app.specdoc.collect import collect_materials
from app.specdoc.elements import build_elements, classify_element
from app.specdoc.events import build_events
from tests.specdoc_fixture import spec_graph, spec_requirement_map

PAGE_ID = "src/pages/Transfer.tsx#Transfer"


def _material():
    graph_ = spec_graph()
    return collect_materials(spec_requirement_map(graph_), graph_).pages[0]


def test_classify_three_kinds() -> None:
    """select→data（即使它发请求）、span→display、发请求按钮→action_remote。"""
    material = _material()
    kinds = {source.node.id: classify_element(source) for source in material.elements}
    assert kinds["src/pages/Transfer.tsx#Transfer/jsx:select"] is ElementKind.data
    assert kinds["src/pages/Transfer.tsx#Transfer/jsx:span.balance"] is ElementKind.display
    assert kinds["src/pages/Transfer.tsx#Transfer/jsx:button"] is ElementKind.action_remote


def test_local_action_without_api() -> None:
    """有事件绑定但链路不含 api 段 → action_local（本地动作）。

    注意：viewRole=text 的元素即使带 onClick 也是展示要素（display 优先），
    这里用无下游的按钮验证本地动作。
    """
    graph_ = spec_graph()
    local_btn = _button_without_chain(graph_)
    material = collect_materials(spec_requirement_map(graph_), graph_).pages[0]
    source = next(item for item in material.elements if item.node.id == local_btn)
    # 无下游的元素也会成单段链（terminated），但链里没有 api 段
    assert not source.has_api
    assert classify_element(source) is ElementKind.action_local


def _button_without_chain(graph_) -> str:
    """给图追加一个带事件绑定但无 calls 边的按钮，返回其 id。"""
    from tests.graph_fixture import edge as make_edge
    from tests.graph_fixture import node as make_node

    btn_id = "src/pages/Transfer.tsx#Transfer/jsx:button.local"
    btn = make_node(
        btn_id,
        NodeKind.element,
        "<button> 查看说明",
        file="src/pages/Transfer.tsx",
        attrs={
            "tag": "button",
            "viewRole": "control",
            "viewText": "查看说明",
            "events": [{"name": "onClick", "handlerName": "noop", "targets": []}],
        },
    )
    graph_.nodes.append(btn)
    graph_.edges.append(
        make_edge("src/pages/Transfer.tsx#Transfer", btn_id, EdgeKind.contains)
    )
    return btn_id


def test_container_element_is_skipped() -> None:
    """无交互容器不进要素清单；编号连续无空洞。"""
    material = _material()
    elements = build_elements(material, {})
    ids = [item.id for item in elements]
    assert not any(item.tag == "div" for item in elements)
    assert ids == ["ui.1.1", "ui.1.2", "ui.1.3"]


def test_element_ids_are_sequential_and_events_linked() -> None:
    """要素 id 形如 ui.{pageNo}.{n}；事件 id 回填到触发要素。"""
    material = _material()
    events, _ = build_events(material)
    event_ids_by_element: dict[str, list[str]] = {}
    for event in events:
        event_ids_by_element.setdefault(event.elementId, []).append(event.id)
    elements = build_elements(material, event_ids_by_element)

    assert [item.id for item in elements] == [
        f"ui.1.{index}" for index in range(1, len(elements) + 1)
    ]
    button = next(item for item in elements if item.kind is ElementKind.action_remote)
    assert button.uiText == "转账"
    assert button.eventIds and all(item.startswith("evt.1.") for item in button.eventIds)
    select = next(item for item in elements if item.kind is ElementKind.data)
    assert select.eventIds, "select 的 change 事件应回填"


def test_element_without_view_text_is_allowed() -> None:
    """无可见文案的录入控件（input 无 placeholder）也能装配：uiText 缺省为 None。

    真实项目里大量 input 不带任何文案——契约允许 uiText 缺省，装配不得崩溃。
    """
    graph_ = spec_graph()
    input_id = "src/pages/Transfer.tsx#Transfer/jsx:input.bare"
    from tests.graph_fixture import edge as make_edge
    from tests.graph_fixture import node as make_node

    graph_.nodes.append(
        make_node(
            input_id,
            NodeKind.element,
            "<input />",
            file="src/pages/Transfer.tsx",
            attrs={"tag": "input", "viewRole": "control"},
        )
    )
    graph_.edges.append(
        make_edge("src/pages/Transfer.tsx#Transfer", input_id, EdgeKind.contains)
    )
    material = collect_materials(spec_requirement_map(graph_), graph_).pages[0]
    elements = build_elements(material, {})
    bare = next(item for item in elements if item.tag == "input")
    assert bare.uiText is None
    assert bare.kind is ElementKind.data


def test_elements_carry_static_evidence() -> None:
    """确定性条目的证据 source=static、置信度取自 IR 节点证据。"""
    material = _material()
    elements = build_elements(material, {})
    button = next(item for item in elements if item.kind is ElementKind.action_remote)
    assert button.evidence is not None
    assert button.evidence[0].source is Source.static
    assert button.evidence[0].file == "src/pages/Transfer.tsx"
    assert button.evidence[0].loc is not None


def test_events_only_collect_api_calls_with_handler_evidence() -> None:
    """事件清单只收发请求的事件；handler 取链路段的真实 file/line。"""
    material = _material()
    events, warnings = build_events(material)
    # 两条链都含 api 段（余额查询 + 转账）
    assert len(events) == 2
    assert all(event.httpCalls for event in events)
    assert all(event.source is Source.static for event in events)
    transfer_event = next(item for item in events if item.handler.name == "handleTransfer")
    assert transfer_event.handler.file == "src/pages/Transfer.tsx"
    assert transfer_event.httpCalls[0].method == "POST"
    assert transfer_event.httpCalls[0].url == "/api/transfer"
    # 语料完备的图不应产生断点/缺 handler 告警
    assert warnings == []


def test_api_mappings_dedupe_by_url() -> None:
    """同一要素对同一接口的多次调用只记一行；actionName 取可见文案。"""
    material = _material()
    events, _ = build_events(material)
    # 模拟重试：把转账事件里同一调用重复一遍
    transfer_event = next(item for item in events if item.handler.name == "handleTransfer")
    transfer_event.httpCalls.append(transfer_event.httpCalls[0])
    mappings = build_api_mappings(material, events)
    transfer_rows = [item for item in mappings if item.url == "/api/transfer"]
    assert len(transfer_rows) == 1
    row = transfer_rows[0]
    assert row.method == "POST"
    assert row.source is Source.static
    assert row.confidence == 0.95
    assert row.actionName == "转账"
    assert row.elementId == transfer_event.elementId


def test_page_numbers_follow_match_order() -> None:
    """页面序号按 matches 出现顺序分配，重复页面合并。"""
    graph_ = spec_graph()
    requirement_map = spec_requirement_map(graph_)
    materials = collect_materials(requirement_map, graph_)
    assert [item.page.pageNo for item in materials.pages] == [1]
    assert materials.pages[0].page.featureIds == ["f1", "f2"]


def test_no_matches_yields_warning() -> None:
    """dry-run 产物没有 matches：无素材可装配，出 no_matched_pages 告警。"""

    graph_ = spec_graph()
    empty = spec_requirement_map(graph_).model_copy(update={"matches": []})
    materials = collect_materials(empty, graph_)
    assert not materials.has_pages
    assert [warning.code for warning in materials.warnings] == ["no_matched_pages"]
