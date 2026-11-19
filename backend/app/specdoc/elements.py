"""确定性装配：2.x.4.1 页面要素清单。

三分类判定优先级（从上到下，先命中先归类）：
  1. 录入控件（input / select / textarea 等）→ data
     ——即使它的 change 事件会发请求（模板里 payerAcct 是数据要素，
       其 evt.acctChange 才是事件清单的一行），要素归类仍按「用户在这里录入」
  2. 展示文本（viewRole=text 或 span/p/h1-h6/label）→ display
  3. 链路含 api 段 → action_remote（发请求）
  4. 有链路或事件绑定 → action_local（本地动作）
  5. 其余（无交互的容器等）不进清单——要素清单只列「看得见、点得到、要填」的
"""

from __future__ import annotations

from app.contracts.specdoc import Element, ElementKind
from app.specdoc.collect import ElementSource, PageMaterial

# 录入控件标签：出现即数据要素
_DATA_TAGS = frozenset(
    {
        "input",
        "select",
        "textarea",
        "password",
        "radio",
        "checkbox",
        "datepicker",
        "switch",
        "slider",
        "rate",
        "upload",
        "cascader",
        "tree-select",
    }
)

# 纯展示标签
_DISPLAY_TAGS = frozenset({"span", "p", "h1", "h2", "h3", "h4", "h5", "h6", "label", "text"})

# 动作类型列的展示文案
_KIND_LABEL: dict[ElementKind, str] = {
    ElementKind.data: "数据要素",
    ElementKind.action_remote: "动作要素",
    ElementKind.action_local: "动作要素",
    ElementKind.display: "展示要素",
}


def kind_label(kind: ElementKind) -> str:
    return _KIND_LABEL[kind]


def classify_element(source: ElementSource) -> ElementKind | None:
    """三分类判定。返回 None 表示该元素不进要素清单。"""
    attrs = source.node.attrs
    tag_value = attrs.get("tag")
    tag = tag_value.lower() if isinstance(tag_value, str) else ""
    view_role = attrs.get("viewRole")

    if tag in _DATA_TAGS:
        return ElementKind.data
    if view_role == "text" or tag in _DISPLAY_TAGS:
        return ElementKind.display
    if source.has_api:
        return ElementKind.action_remote
    if source.chains or _has_event_bindings(attrs):
        return ElementKind.action_local
    # 无交互、无文案的容器/包裹元素：不进清单
    return None


def _has_event_bindings(attrs: dict) -> bool:
    events = attrs.get("events")
    return isinstance(events, list) and len(events) > 0


def _describe(source: ElementSource, kind: ElementKind) -> str:
    """说明列：确定性描述 + 已知线索（链接目标 / 触发的接口）。"""
    href = source.node.attrs.get("href")
    if kind is ElementKind.data:
        return "数据录入"
    if kind is ElementKind.display:
        return "展示内容"
    if kind is ElementKind.action_remote:
        urls = [
            step.name
            for chain in source.chains
            for step in chain.steps
            if step.role == "api"
        ]
        return "点击发起后端调用：" + "、".join(dict.fromkeys(urls)) if urls else "发起后端调用"
    if isinstance(href, str) and href:
        return f"链接至 {href}"
    return "本地交互"


def build_elements(
    material: PageMaterial, event_ids_by_element: dict[str, list[str]]
) -> list[Element]:
    """装配一个页面的要素清单。id 规则：ui.{pageNo}.{序号}（按元素 node id 排序）。"""
    from app.specdoc.collect import element_evidence

    elements: list[Element] = []
    # 编号必须连续：跳过的容器不占号，否则要素 id 会出现 1,3,4 的空洞
    index = 0
    for source in material.elements:
        kind = classify_element(source)
        if kind is None:
            continue
        index += 1
        attrs = source.node.attrs
        tag_value = attrs.get("tag")
        view_text = attrs.get("viewText")
        required = attrs.get("required")
        evidence = element_evidence(source.node)
        elements.append(
            Element(
                id=f"ui.{material.page.pageNo}.{index}",
                pageId=material.page.pageId,
                kind=kind,
                tag=(
                    tag_value
                    if isinstance(tag_value, str) and tag_value
                    else source.node.name
                ),
                uiText=view_text if isinstance(view_text, str) and view_text.strip() else None,
                required=required if isinstance(required, bool) else None,
                desc=_describe(source, kind),
                eventIds=(
                    event_ids_by_element[source.node.id]
                    if event_ids_by_element.get(source.node.id)
                    else None
                ),
                featureIds=sorted(source.matched_features) or None,
                evidence=[evidence] if evidence is not None else None,
            )
        )
    return elements
