"""确定性装配：2.x.4.2 事件清单（仅触发后端调用的事件）。

与模板语义一致：只有「点击/变更 → 调后端」的事件才入清单。纯本地交互
（导航、弹层开关）不进——它们已经在要素清单的动作类型里体现了。

事件与链路一一对应：链路的起点是元素，第二段起是 handler / state / api。
链路在断点处终止（terminatedAt 非空）时，只收集到断点为止的 api 段，
并出 chain_terminated 告警。
"""

from __future__ import annotations

from app.contracts.specdoc import Event, Handler, HttpCall, Location, Source
from app.requirement.chains import ActionChain
from app.specdoc.collect import PageMaterial

_HANDLER_ROLES = frozenset({"handler", "hook"})
_API_ROLES = frozenset({"api"})


def _parse_api_call(name: str) -> tuple[str, str]:
    """api 节点名是 "METHOD url"（ast-worker 的 api.ts 保证 method 恒非空）。

    防御老图/外部图：没有 method 前缀时按 GET 兜底，整段视作 url。
    """
    parts = name.split(None, 1)
    if len(parts) == 2 and parts[0].isupper() and len(parts[0]) <= 10:
        return parts[0], parts[1]
    return "GET", name


def _handler_for(chain: ActionChain) -> Handler | None:
    """链路里第一个 handler/hook 段就是触发点。"""
    for step in chain.steps:
        if step.role in _HANDLER_ROLES:
            return Handler(
                name=step.name,
                file=step.file,
                loc=(
                    Location(line=step.line, col=step.col)
                    if step.line is not None or step.col is not None
                    else None
                ),
            )
    return None


def _http_calls(chain: ActionChain) -> list[HttpCall]:
    """链路的 api 段按出现顺序去重（同一 handler 重试同一接口只记一次）。"""
    calls: list[HttpCall] = []
    seen: set[tuple[str, str]] = set()
    for step in chain.steps:
        if step.role not in _API_ROLES:
            continue
        method, url = _parse_api_call(step.name)
        key = (method, url)
        if key not in seen:
            seen.add(key)
            calls.append(HttpCall(method=method, url=url))
    return calls


def _element_node(material: PageMaterial, node_id: str):
    """链路起点元素对应的 IRNode（ChainStep 不带 attrs，元素事实从素材里取）。"""
    for source in material.elements:
        if source.node.id == node_id:
            return source.node
    return None


def _event_type(material: PageMaterial, chain: ActionChain) -> str | None:
    """从元素的事件绑定里认领这条链：绑定的 targets 覆盖链路下游节点。

    认领不到时退回第一个绑定名。都没有则缺省。
    """
    element = _element_node(material, chain.element.node_id)
    bindings = element.attrs.get("events") if element is not None else None
    if not isinstance(bindings, list) or not bindings:
        return None
    downstream = {step.node_id for step in chain.steps[1:]}
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        targets = binding.get("targets")
        target_ids = (
            {item.get("id") for item in targets if isinstance(item, dict)}
            if isinstance(targets, list)
            else set()
        )
        if downstream & target_ids:
            return binding.get("name") if isinstance(binding.get("name"), str) else None
    first = bindings[0]
    name = first.get("name") if isinstance(first, dict) else None
    return name if isinstance(name, str) else None


def _confidence(material: PageMaterial, chain: ActionChain) -> float | None:
    """事件置信度取元素节点证据的最高置信度（事件清单本身是图上事实）。"""
    for source in material.elements:
        if source.node.id == chain.element.node_id and source.node.evidence:
            return max(item.confidence for item in source.node.evidence)
    return None


def build_events(material: PageMaterial) -> tuple[list[Event], list]:
    """装配一个页面的事件清单与告警。

    id 规则：evt.{pageNo}.{序号}（链路按 id 排序后顺序编号）。同一提交事件
    可能同时出现在父组件透传和子表单上；只有 handler 身份、事件类型和接口
    集合都一致时才归并，避免把真实的两个入口误折叠。
    """
    from app.contracts.requirement import RequirementWarning, Severity

    candidates: list[tuple[ActionChain, list[HttpCall], Handler | None, str | None]] = []
    warnings: list[RequirementWarning] = []
    for chain in sorted(material.chains, key=lambda item: item.id):
        calls = _http_calls(chain)
        if not calls:
            continue
        if chain.terminated_at is not None:
            warnings.append(
                RequirementWarning(
                    code="chain_terminated",
                    severity=Severity.warning,
                    message=(
                        f"链路 {chain.id} 在 {chain.terminated_at} 处断开："
                        "事件只收集到断点前的接口段，断点之后的调用链未能确认。"
                    ),
                )
            )
        handler = _handler_for(chain)
        if handler is None:
            warnings.append(
                RequirementWarning(
                    code="handler_unresolved",
                    severity=Severity.warning,
                    message=f"链路 {chain.id} 缺少 handler 段，事件归到元素级触发。",
                )
            )
        candidates.append((chain, calls, handler, _event_type(material, chain)))

    grouped: dict[tuple, tuple[ActionChain, list[HttpCall], Handler | None, str | None]] = {}
    for candidate in candidates:
        key = _dedupe_key(candidate[0], candidate[1], candidate[2], candidate[3])
        current = grouped.get(key)
        if current is None or _event_preference(material, candidate[0]) < _event_preference(material, current[0]):
            grouped[key] = candidate

    events: list[Event] = []
    for index, (chain, calls, handler, event_type) in enumerate(
        sorted(grouped.values(), key=lambda item: item[0].id), start=1
    ):
        events.append(
            Event(
                id=f"evt.{material.page.pageNo}.{index}",
                pageId=material.page.pageId,
                elementId=chain.element.node_id,
                eventType=event_type,
                handler=handler or Handler(name=chain.element.name),
                httpCalls=calls,
                chainId=chain.id,
                source=Source.static,
                confidence=_confidence(material, chain),
            )
        )
    return events, warnings


def _dedupe_key(
    chain: ActionChain,
    calls: list[HttpCall],
    handler: Handler | None,
    event_type: str | None,
) -> tuple:
    """Return a conservative identity for parent/child duplicate submissions."""
    if handler is None:
        handler_key = ("element", chain.element.node_id)
    elif handler.file and handler.loc is not None:
        handler_key = ("location", handler.file, handler.loc.line, handler.loc.col, handler.name)
    elif handler.file:
        handler_key = ("file_name", handler.file, handler.name)
    else:
        handler_key = ("name", handler.name)
    calls_key = tuple((call.method, call.url) for call in calls)
    return (event_type, handler_key, calls_key)


def _event_preference(material: PageMaterial, chain: ActionChain) -> tuple[int, str]:
    """Prefer a concrete form/control over a component-level duplicate."""
    for source in material.elements:
        if source.node.id != chain.element.node_id:
            continue
        tag = source.node.attrs.get("tag")
        if isinstance(tag, str) and tag.lower() == "form":
            return (0, chain.id)
        if isinstance(tag, str) and tag.lower() in {"button", "input", "select", "textarea"}:
            return (1, chain.id)
        return (2, chain.id)
    return (3, chain.id)
