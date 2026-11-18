"""探索计划的预生成。

设计（B 模式）：AI 决策只发生在这一步——模型基于 discover 阶段枚举出的真实
元素产出一份完整的确定性计划，Playwright 执行阶段不再有模型参与。计划必须
只引用 discovery 中真实存在的元素（targetIndex 指向元素序号），服务端逐条
校验后把 targetIndex 解析成 selector 再下发给 Worker，从协议上杜绝幻觉。

模型不可用或输出不合规时退到确定性兜底计划（填可见文本框 + 点首个按钮），
任务不因模型故障而不可用，但会携带 warning 如实记录。
"""

import json
import logging
from dataclasses import dataclass

from app.contracts import (
    Action,
    DiscoveredElement,
    ExplorePlan,
    ExploreWarning,
    PlanStep,
    Priority,
    Scenario,
    TestCase,
)
from app.requirement.llm import LLMCallError, LLMClient

logger = logging.getLogger(__name__)

PLAN_MAX_STEPS = 50

_TARGET_ACTIONS = frozenset({Action.click, Action.fill, Action.select, Action.press})
_VALUE_ACTIONS = frozenset(
    {Action.fill, Action.select, Action.press, Action.wait, Action.navigate}
)

_PLAN_SYSTEM = """你是前端测试专家。基于给定的页面可交互元素清单，为「探索目标」生成一份
确定性操作计划（Playwright 逐步执行）。要求：
1. 只允许引用清单中的元素（targetIndex 对应元素序号），禁止臆造不存在的控件。
2. 步骤尽量精简（不超过给定上限），按用户操作顺序排列。
3. 涉及文本输入时优先使用中性占位值（如 test@example.com、13800000000），
   禁止生成破坏性操作（删除、支付、提交真实工单等）。
4. 对关键交互给出 assert 步骤，断言页面应出现的可见文案。
5. scenario 给出场景标题与描述；testCases 给出 1-3 条可直接执行的测试用例。

只输出 JSON，结构如下：
{
  "summary": "整体探索路径说明",
  "steps": [
    {"action": "click|fill|select|press|wait|assert|screenshot|navigate",
     "targetIndex": 0, "value": "输入/选项/按键/地址", "assertion": "断言文案",
     "reasoning": "选择该步骤的理由", "optional": false}
  ],
  "scenario": {"title": "...", "description": "...", "preconditions": ["..."],
               "expectedOutcome": "..."},
  "testCases": [{"title": "...", "priority": "p0", "steps": ["..."],
                 "expected": ["..."], "relatedSteps": [0, 1]}]
}"""


@dataclass(frozen=True)
class PlanSource:
    """计划产物的来源与告警。source ∈ {llm, fallback}。"""

    plan: ExplorePlan
    source: str
    warnings: list[ExploreWarning]


def _element_brief(elements: list[DiscoveredElement]) -> str:
    """给模型的元素清单：紧凑一行一条，含模型决策所需的全部字段。"""
    lines: list[str] = []
    for el in elements:
        parts = [f"#{el.index}", f"<{el.tag}>", f"role={el.role}"]
        if el.name:
            parts.append(f"name={el.name!r}")
        if el.type:
            parts.append(f"type={el.type}")
        if el.testId:
            parts.append(f"testId={el.testId}")
        if el.href:
            parts.append(f"href={el.href!r}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def build_selector_payload(element: DiscoveredElement) -> dict[str, object]:
    """契约 Selector 的 JSON 形态，直接进 Worker 请求。"""
    selector: dict[str, object] = {
        "strategy": element.selector.strategy,
        "value": element.selector.value,
    }
    if element.selector.name is not None:
        selector["name"] = element.selector.name
    return selector


def resolve_step_selector(
    step: PlanStep, elements: list[DiscoveredElement]
) -> dict[str, object] | None:
    """把计划的 targetIndex 解析成 Worker 可执行的 selector。

    返回 None 表示计划步骤本身不需要 selector（wait/screenshot/assert/navigate）。
    targetIndex 越界属于编排层 bug，直接抛错（执行前有 validate_plan_elements 防线）。
    """
    if step.targetIndex is None:
        return None
    element = next((el for el in elements if el.index == step.targetIndex), None)
    if element is None:
        raise ValueError(f"计划步骤 {step.index} 的 targetIndex {step.targetIndex} 越界")
    return build_selector_payload(element)


def validate_plan_elements(plan: ExplorePlan, elements: list[DiscoveredElement]) -> None:
    """执行前的最终防线：所有 targetIndex 必须命中 discovery。"""
    known = {el.index for el in elements}
    for step in plan.steps:
        if step.targetIndex is not None and step.targetIndex not in known:
            raise ValueError(f"计划步骤 {step.index} 引用了不存在的元素 #{step.targetIndex}")


def _clean_str(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _validate_scenario(raw: object, goal: str) -> Scenario:
    if isinstance(raw, dict):
        title = _clean_str(raw.get("title")) or goal[:60]
        return Scenario(
            title=title,
            description=_clean_str(raw.get("description")) or "",
            preconditions=[
                str(item) for item in (raw.get("preconditions") or []) if isinstance(item, str)
            ],
            expectedOutcome=_clean_str(raw.get("expectedOutcome")),
        )
    return Scenario(title=goal[:60], description="")


def _validate_test_cases(raw: object) -> list[TestCase]:
    if not isinstance(raw, list):
        return []
    cases: list[TestCase] = []
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        title = _clean_str(item.get("title"))
        if not title:
            continue
        priority_raw = item.get("priority")
        priority: Priority | None = None
        if isinstance(priority_raw, str):
            try:
                priority = Priority(priority_raw)
            except ValueError:
                priority = None
        steps = [str(s) for s in (item.get("steps") or []) if isinstance(s, str) and s.strip()]
        expected = [
            str(s) for s in (item.get("expected") or []) if isinstance(s, str) and s.strip()
        ]
        related = [s for s in (item.get("relatedSteps") or []) if isinstance(s, int)]
        cases.append(
            TestCase(
                title=title,
                priority=priority,
                steps=steps,
                expected=expected,
                relatedSteps=related or None,
            )
        )
    return cases


def _validate_and_fill(
    raw: dict[str, object],
    elements: list[DiscoveredElement],
    goal: str,
    max_steps: int,
) -> tuple[ExplorePlan, list[str]]:
    """校验模型输出：目标存在、动作与元素匹配、步数截断。返回（计划, 问题列表）。

    校验是硬约束：targetIndex 越界或指向非目标动作元素的步骤直接丢弃（记问题），
    剩余步骤按丢弃后的顺序重排 index，保证计划仍然可执行。
    """
    issues: list[str] = []
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("模型输出的 steps 不是数组")

    valid_steps: list[PlanStep] = []
    for position, raw_step in enumerate(raw_steps):
        if len(valid_steps) >= max_steps:
            issues.append(f"步骤数超过上限 {max_steps}，第 {position} 步起已截断")
            break
        if not isinstance(raw_step, dict):
            issues.append(f"第 {position} 步不是对象，已丢弃")
            continue

        raw_action = raw_step.get("action")
        try:
            action = Action(raw_action) if isinstance(raw_action, str) else None
        except ValueError:
            action = None
        if action is None:
            issues.append(f"第 {position} 步动作非法：{raw_action!r}，已丢弃")
            continue

        target_index = raw_step.get("targetIndex")
        element: DiscoveredElement | None = None
        if action in _TARGET_ACTIONS:
            if not isinstance(target_index, int):
                issues.append(f"第 {position} 步（{action.value}）缺少 targetIndex，已丢弃")
                continue
            element = next((el for el in elements if el.index == target_index), None)
            if element is None:
                issues.append(
                    f"第 {position} 步目标 #{target_index} 不在页面元素清单中，已丢弃"
                )
                continue
            if action is Action.fill and element.role not in ("textbox", "searchbox"):
                issues.append(
                    f"第 {position} 步 fill 目标 #{target_index}（{element.role}）"
                    "不是文本输入，已丢弃"
                )
                continue
            if action is Action.select and element.role != "combobox":
                issues.append(
                    f"第 {position} 步 select 目标 #{target_index}（{element.role}）"
                    "不是下拉框，已丢弃"
                )
                continue

        value = _clean_str(raw_step.get("value"))
        assertion = _clean_str(raw_step.get("assertion"))
        if action is Action.assert_ and not assertion:
            issues.append(f"第 {position} 步 assert 缺少 assertion，已丢弃")
            continue
        if action is Action.navigate and not value:
            issues.append(f"第 {position} 步 navigate 缺少目标地址，已丢弃")
            continue
        if value is not None and action not in _VALUE_ACTIONS:
            # click/screenshot 不需要 value，多余字段不阻断，仅剥离
            value = None
            assertion = None if action is not Action.assert_ else assertion

        valid_steps.append(
            PlanStep(
                index=len(valid_steps),
                action=action,
                targetIndex=target_index if element is not None else None,
                value=value,
                assertion=assertion,
                reasoning=_clean_str(raw_step.get("reasoning")),
                optional=bool(raw_step.get("optional", False)),
            )
        )

    plan = ExplorePlan(
        goal=goal,
        summary=_clean_str(raw.get("summary")),
        maxSteps=max_steps,
        steps=valid_steps,
        scenario=_validate_scenario(raw.get("scenario"), goal),
        testCases=_validate_test_cases(raw.get("testCases")),
    )
    return plan, issues


async def generate_plan_with_llm(
    llm: LLMClient, goal: str, elements: list[DiscoveredElement], max_steps: int
) -> tuple[ExplorePlan, list[str]]:
    """调模型产出计划。模型返回不合规 JSON 或缺少 steps 时抛 ValueError（调用方兜底）。"""
    if not elements:
        raise ValueError("页面没有可交互元素，无法生成计划")
    user = (
        f"探索目标：{goal}\n"
        f"步骤上限：{max_steps}\n"
        f"页面可交互元素清单（targetIndex 即序号）：\n{_element_brief(elements)}"
    )
    content = await llm.chat(_PLAN_SYSTEM, user)
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"模型输出不是合法 JSON：{error}") from error
    if not isinstance(raw, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return _validate_and_fill(raw, elements, goal, max_steps)


def build_fallback_plan(
    goal: str, elements: list[DiscoveredElement], max_steps: int
) -> tuple[ExplorePlan, list[str]]:
    """确定性兜底计划：填前 2 个可见文本框（中性占位值）→ 点首个可见按钮 → 等待。

    兜底计划不追求覆盖度，只保证「模型不可用时任务仍可执行且无破坏性」。
    """
    issues: list[str] = []
    steps: list[PlanStep] = []

    textboxes = [el for el in elements if el.visible and el.role == "textbox"]
    buttons = [el for el in elements if el.visible and el.role in ("button", "link")]
    for el in textboxes[:2]:
        if len(steps) >= max_steps:
            break
        steps.append(
            PlanStep(
                index=len(steps),
                action=Action.fill,
                targetIndex=el.index,
                value="explore-auto-fill",
                reasoning="兜底计划：向文本框填入中性占位值",
            )
        )
    if buttons and len(steps) < max_steps:
        steps.append(
            PlanStep(
                index=len(steps),
                action=Action.click,
                targetIndex=buttons[0].index,
                reasoning="兜底计划：点击首个可见按钮触发页面响应",
            )
        )
    elif not buttons:
        issues.append("页面上没有可见按钮/链接，兜底计划只覆盖文本框填写")
    if len(steps) < max_steps:
        steps.append(
            PlanStep(
                index=len(steps),
                action=Action.wait,
                value="1000",
                reasoning="兜底计划：等待页面响应落定",
            )
        )
    if not steps:
        issues.append("页面没有可操作的元素，兜底计划为空")

    plan = ExplorePlan(
        goal=goal,
        summary="确定性兜底计划（模型不可用）：填写可见文本框并点击首个按钮",
        maxSteps=max_steps,
        steps=steps,
        scenario=Scenario(title=goal[:60], description="模型不可用时的基础交互冒烟"),
        testCases=[
            TestCase(
                title="页面基础交互冒烟",
                priority=Priority.p2,
                steps=[
                    f"{s.action.value} #{s.targetIndex}"
                    for s in steps
                    if s.targetIndex is not None
                ],
                expected=["页面无报错，交互可完成"],
            )
        ],
    )
    return plan, issues


async def generate_plan(
    llm: LLMClient | None,
    goal: str,
    elements: list[DiscoveredElement],
    max_steps: int,
) -> PlanSource:
    """统一入口：有模型用模型，失败或不合规退兜底。所有降级都记 warning。"""
    warnings: list[ExploreWarning] = []
    if llm is not None:
        try:
            plan, issues = await generate_plan_with_llm(llm, goal, elements, max_steps)
            for issue in issues:
                warnings.append(
                    ExploreWarning(code="plan_step_dropped", severity="warning", message=issue)
                )
            return PlanSource(plan=plan, source="llm", warnings=warnings)
        except LLMCallError as error:
            logger.warning("模型生成探索计划失败，退到兜底计划：%s", error.message)
            warnings.append(
                ExploreWarning(
                    code="llm_unavailable_fallback",
                    severity="warning",
                    message=f"模型生成计划失败（{error.code}），已使用确定性兜底计划：{error.message}",
                )
            )
        except ValueError as error:
            logger.warning("模型探索计划不合规，退到兜底计划：%s", error)
            warnings.append(
                ExploreWarning(
                    code="plan_invalid_fallback",
                    severity="warning",
                    message=f"模型输出不合规，已使用确定性兜底计划：{error}",
                )
            )
    else:
        warnings.append(
            ExploreWarning(
                code="llm_not_configured",
                severity="info",
                message="未配置模型，使用确定性兜底计划",
            )
        )
    plan, issues = build_fallback_plan(goal, elements, max_steps)
    for issue in issues:
        warnings.append(ExploreWarning(code="fallback_plan", severity="info", message=issue))
    return PlanSource(plan=plan, source="fallback", warnings=warnings)
