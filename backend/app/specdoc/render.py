"""Markdown 渲染：SpecDocMap → 与模板《2.2 转账》同构的规格文档。

章节骨架（与模板保持一致）：
  ## 2.{pageNo} {页面名}
    ### 2.{pageNo}.3 业务操作步骤
    ### 2.{pageNo}.4 前端交互视图
      #### 2.{pageNo}.4.1 页面要素清单
      #### 2.{pageNo}.4.2 事件清单（仅触发后端调用的事件）
      #### 2.{pageNo}.4.3 前端业务规则（可校验）
      #### 2.{pageNo}.4.4 前后端字段映射
      #### 2.{pageNo}.4.5 前后端接口映射

渲染只做呈现，不做判定：空章节如实标注「暂无」，dropped 与 warnings
必须出现在文档末尾——审核人要能看见模型哪些输出被服务端拒了。
"""

from __future__ import annotations

from collections import defaultdict

from app.contracts.specdoc import ElementKind, SpecDocMap
from app.specdoc.elements import kind_label

_CATEGORY_LABEL = {
    "validation": "规则A·交互校验",
    "display": "规则B·展示规则",
    "linkage": "规则C·联动规则",
    "session": "规则D·会话状态",
    "permission": "规则E·权限规则",
}

_KIND_REQUIRED_COLUMN = {
    ElementKind.data: "必填 / 选填",
    ElementKind.action_remote: "**发请求**",
    ElementKind.action_local: "**本地动作**",
    ElementKind.display: "-",
}

# 规则类别的展示顺序与模板一致（A-E）
_CATEGORY_ORDER = ["validation", "display", "linkage", "session", "permission"]


def render_markdown(doc: SpecDocMap) -> str:
    parts: list[str] = []
    title = doc.requirement.strip().splitlines()[0][:40] if doc.requirement.strip() else "规格文档"
    parts.append(f"# 2. {title}\n")
    parts.append(
        f"> 数据来源：需求定位 {doc.requirementId}（图 {doc.analysisId or '未知'}），"
        f"生成于 {doc.createdAt:%Y-%m-%d %H:%M} UTC。"
        f"共 {len(doc.pages)} 个页面、{len(doc.elements)} 个要素、{len(doc.events)} 个事件、"
        f"{len(doc.rules)} 条规则、{len(doc.fieldMappings)} 条字段映射。"
        f"{'（dry-run：仅确定性章节）' if doc.dryRun else ''}\n"
    )

    if not doc.pages:
        parts.append(
            "> 来源定位产物没有命中任何页面，本次未产出内容。"
            "请先以 dryRun=false 重新执行需求定位。\n"
        )

    element_by_id = {element.id: element for element in doc.elements}
    steps_by_page = _group(doc.steps, lambda step: step.pageId or "")
    rules_by_page = _group(doc.rules, lambda rule: rule.pageId)
    fields_by_page = _group(doc.fieldMappings, lambda mapping: mapping.pageId or "")
    apis_by_page = _group_by_element_page(doc, element_by_id)

    for page in doc.pages:
        parts.append(f"## 2.{page.pageNo} {page.name}\n")
        if page.file:
            parts.append(f"页面组件：`{page.file}`\n")

        parts.append(f"### 2.{page.pageNo}.3 业务操作步骤\n")
        page_steps = steps_by_page.get(page.pageId, [])
        if not page_steps:
            parts.append(
                "_本页面暂无操作步骤：推断章节留空（dry-run、模型不可用，或该页面的功能点未被产出）。_\n"
            )
        for step in page_steps:
            if step.precondition:
                parts.append("**前置条件**\n")
                parts.extend(f"- {item}" for item in step.precondition)
                parts.append("")
            parts.append("| 步骤 | 操作 |")
            parts.append("| --- | --- |")
            for index, action in enumerate(step.actions, start=1):
                parts.append(f"| {index} | {action} |")
            if step.evidence:
                parts.append("")
                parts.append(f"依据：{_evidence_text(step.evidence[0])}")
            parts.append("")

        parts.append(f"### 2.{page.pageNo}.4 前端交互视图\n")

        parts.append(f"#### 2.{page.pageNo}.4.1 页面要素清单\n")
        page_elements = [item for item in doc.elements if item.pageId == page.pageId]
        if not page_elements:
            parts.append("_本页面无要素。_\n")
        else:
            parts.append("| 要素ID | 要素名称 | 类型 | 必填/动作类型 | 说明 | 关联事件 | 证据 |")
            parts.append("| --- | --- | --- | --- | --- | --- | --- |")
            for element in page_elements:
                parts.append(
                    "| {id} | {name} | {kind} | {required} | {desc} | {events} | {ev} |".format(
                        id=element.id,
                        name=element.uiText or "-",
                        kind=kind_label(element.kind),
                        required=_required_column(element),
                        desc=element.desc,
                        events=(
                            "、".join(element.eventIds) if element.eventIds else "-"
                        ),
                        ev=(
                            _evidence_text(element.evidence[0]) if element.evidence else "-"
                        ),
                    )
                )
            parts.append("")

        parts.append(f"#### 2.{page.pageNo}.4.2 事件清单（仅触发后端调用的事件）\n")
        page_events = [item for item in doc.events if item.pageId == page.pageId]
        if not page_events:
            parts.append("_本页面无触发后端调用的事件。_\n")
        else:
            parts.append("| 事件ID | 触发要素 | 事件类型 | Handler | HttpCall |")
            parts.append("| --- | --- | --- | --- | --- |")
            for event in page_events:
                element = element_by_id.get(event.elementId)
                name = element.uiText if element and element.uiText else event.elementId
                handler = event.handler.name
                if event.handler.file:
                    handler = f"`{event.handler.file}#{event.handler.name}`"
                calls = "、".join(f"{call.method} {call.url}" for call in event.httpCalls)
                row = f"| {event.id} | {name} | {event.eventType or '-'} | {handler} | {calls} |"
                parts.append(row)
            parts.append("")

        parts.append(f"#### 2.{page.pageNo}.4.3 前端业务规则（可校验）\n")
        page_rules = rules_by_page.get(page.pageId, [])
        if not page_rules:
            parts.append("_本页面暂无业务规则（推断章节留空或无产出）。_\n")
        else:
            by_category = _group(page_rules, lambda rule: rule.category.value)
            for category in _CATEGORY_ORDER:
                items = by_category.get(category, [])
                if not items:
                    continue
                parts.append(f"**{_CATEGORY_LABEL[category]}**\n")
                parts.append("| 规则ID | 作用对象 | 条件 | 结果 | 证据 |")
                parts.append("| --- | --- | --- | --- | --- |")
                for rule in items:
                    parts.append(
                        "| {id} | {target} | {condition} | {result} | {evidence} |".format(
                            id=rule.id,
                            target=rule.target or "-",
                            condition=rule.condition,
                            result=rule.result,
                            evidence=_evidence_text(rule.evidence[0]) if rule.evidence else "-",
                        )
                    )
                parts.append("")

        parts.append(f"#### 2.{page.pageNo}.4.4 前后端字段映射\n")
        page_fields = fields_by_page.get(page.pageId, [])
        if not page_fields:
            parts.append("_本页面暂无字段映射（推断章节留空或无产出）。_\n")
        else:
            parts.append("| 前端字段 | 后端参数 | 接口 | 证据 |")
            parts.append("| --- | --- | --- | --- |")
            for mapping in page_fields:
                parts.append(
                    f"| {mapping.frontField} | {mapping.backendParam} | {mapping.api} | "
                    f"{_evidence_text(mapping.evidence[0]) if mapping.evidence else '-'} |"
                )
            parts.append("")

        parts.append(f"#### 2.{page.pageNo}.4.5 前后端接口映射\n")
        page_apis = apis_by_page.get(page.pageId, [])
        if not page_apis:
            parts.append("_本页面无后端接口调用。_\n")
        else:
            parts.append("| 动作名称 | 接口名称 | 动作类型 | 证据 |")
            parts.append("| --- | --- | --- | --- |")
            for mapping in page_apis:
                row = (
                    f"| {mapping.actionName or '-'} | {mapping.method} {mapping.url} "
                    f"| **{mapping.method}** | {mapping.confidence:.2f} |"
                )
                parts.append(row)
            parts.append("")

    if doc.dropped:
        parts.append("## 附录：未采信的模型输出\n")
        parts.append("以下条目因引文校验失败或形状非法被服务端拒绝，仅供审计。\n")
        parts.append("| 原因 | featureId | 引文 |")
        parts.append("| --- | --- | --- |")
        for item in doc.dropped:
            parts.append(f"| {item.reason.value} | {item.featureId or '-'} | {item.quote or '-'} |")
        parts.append("")

    if doc.warnings:
        parts.append("## 附录：告警\n")
        for warning in doc.warnings:
            parts.append(f"- `{warning.code}` {warning.message}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _required_column(element) -> str:
    if element.kind is ElementKind.data:
        if element.required is True:
            return "**必填**"
        if element.required is False:
            return "选填"
        return "-"
    return _KIND_REQUIRED_COLUMN[element.kind]


def _evidence_text(evidence) -> str:
    if evidence.file is None:
        return f"`{evidence.nodeId}`"
    anchor = f"#L{evidence.loc.line}" if evidence.loc is not None else ""
    return f"`{evidence.file}{anchor}`"


def _group(items, key_of) -> dict[str, list]:
    result: dict[str, list] = defaultdict(list)
    for item in items:
        result[key_of(item)].append(item)
    return result


def _group_by_element_page(doc: SpecDocMap, element_by_id) -> dict[str, list]:
    """接口映射按触发要素的页面归组。"""
    result: dict[str, list] = defaultdict(list)
    for mapping in doc.apiMappings:
        element = element_by_id.get(mapping.elementId)
        page_id = element.pageId if element is not None else ""
        result[page_id].append(mapping)
    return result
