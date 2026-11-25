"""确定性装配：2.x.4.5 前后端接口映射。

事件清单按「事件 → 接口」组织；接口映射反过来按「要素 → 接口」平铺，
同一要素触发的多个接口各占一行（接口按 url 去重：同一要素对同一接口
的多次调用——分页、轮询——只记一行）。source 恒为 static：接口段是图上
事实，置信度对齐锚点直连的 0.95 分档。
"""

from __future__ import annotations

from app.contracts.specdoc import ApiMapping, Event, Source
from app.requirement.chains import ActionChain
from app.specdoc.collect import PageMaterial
from app.specdoc.events import _parse_api_call

# 链路 api 段是确定性事实：对齐锚点直连的置信度分档（见 requirement.anchors）
_STATIC_CONFIDENCE = 0.95


def build_api_mappings(material: PageMaterial, events: list[Event]) -> list[ApiMapping]:
    """装配一个页面的接口映射。

    以事件为锚：events 里每条事件的每个 httpCall 各一行。事件的 elementId
    即触发要素；actionName 取该要素的可见文案（取不到时用 tag 兜底）。
    """
    view_text_by_id = {
        source.node.id: source.node.attrs.get("viewText") for source in material.elements
    }
    tag_by_id = {
        source.node.id: source.node.attrs.get("tag") for source in material.elements
    }

    mappings: list[ApiMapping] = []
    seen: set[tuple[str, str, str]] = set()  # (elementId, method, url)
    for event in events:
        for call in event.httpCalls or []:
            key = (event.elementId, call.method, call.url)
            if key in seen:
                continue
            seen.add(key)
            view_text = view_text_by_id.get(event.elementId)
            action_name = (
                view_text
                if isinstance(view_text, str) and view_text.strip()
                else _tag_of(tag_by_id.get(event.elementId))
            )
            mappings.append(
                ApiMapping(
                    elementId=event.elementId,
                    eventId=event.id,
                    actionName=action_name,
                    method=call.method,
                    url=call.url,
                    source=Source.static,
                    confidence=_STATIC_CONFIDENCE,
                )
            )
    return mappings


def _tag_of(tag_value: object) -> str | None:
    return tag_value if isinstance(tag_value, str) and tag_value.strip() else None


def all_api_steps(chains: list[ActionChain]) -> list[tuple[str, str]]:
    """链路集合里全部 api 调用（method, url），按出现顺序去重。字段映射章节用。"""
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for chain in chains:
        for step in chain.steps:
            if step.role == "api":
                method, url = _parse_api_call(step.name)
                if (method, url) not in seen:
                    seen.add((method, url))
                    result.append((method, url))
    return result
