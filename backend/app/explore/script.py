"""Playwright .spec.ts 生成：从探索计划确定性生成测试脚本。

生成只依赖计划（含场景与用例清单），与执行结果解耦——dry-run 也能拿到
脚本草稿；执行完成后同一个脚本即「探索轨迹的可重放形态」。

测试函数按 testCases 划分：每条用例一个 test，其步骤映射 relatedSteps
指向的计划步骤；没有用例时把全部计划步骤合成一个 test。
"""

from app.contracts import Action, ExplorePlan, GeneratedScript, Selector, Strategy, TestCase

_FILENAME = "explore-plan.spec.ts"

def _ts(value: str) -> str:
    """TS 单引号字符串字面量：转义反斜杠、单引号、换行与模板插值 ${。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("${", "\\${")
    )
    return f"'{escaped}'"


def _locator(selector: Selector) -> str:
    """与 Worker 的 resolveLocator 同一映射规则，保证脚本与探索行为一致。"""
    if selector.strategy is Strategy.role:
        if selector.name:
            return f"page.getByRole({_ts(selector.value)}, {{ name: {_ts(selector.name)} }})"
        return f"page.getByRole({_ts(selector.value)})"
    if selector.strategy is Strategy.testId:
        return f"page.getByTestId({_ts(selector.value)})"
    if selector.strategy is Strategy.text:
        return f"page.getByText({_ts(selector.value)})"
    return f"page.locator({_ts(selector.value)})"


def build_script(
    plan: ExplorePlan,
    page_url: str,
    selectors: dict[int, Selector],
    test_cases: list[TestCase],
) -> GeneratedScript:
    """selectors：计划步骤 index → discovery 解析出的元素 selector。"""
    lines: list[str] = [
        "// 由前端代码智能分析平台自动生成（AI 探索计划 → Playwright 脚本）。",
        "// 探索目标：" + plan.goal.replace("\n", " "),
        "import { test, expect } from '@playwright/test';",
        "",
    ]

    cases = test_cases or [
        TestCase(
            title=plan.scenario.title,
            steps=[],
            expected=[],
        )
    ]
    used_indices: set[int] = set()
    for case in cases:
        related = case.relatedSteps or []
        if related:
            chosen = [i for i in related if i in selectors or _has_plan_step(plan, i)]
            used_indices.update(chosen)
        else:
            chosen = [s.index for s in plan.steps]
            used_indices.update(chosen)
        lines.append(f"test({_ts(case.title)}, async {{ page }} => {{")
        lines.append(f"  await page.goto({_ts(page_url)});")
        for index in chosen:
            lines.extend(_emit_step(index, plan, selectors))
        lines.append("});")
        lines.append("")

    # 用例没覆盖到的计划步骤补一条兜底 test，保证轨迹不丢
    leftover = [s.index for s in plan.steps if s.index not in used_indices]
    if leftover:
        lines.append(f"test({_ts(plan.scenario.title + '（补充覆盖）')}, async {{ page }} => {{")
        lines.append(f"  await page.goto({_ts(page_url)});")
        for index in leftover:
            lines.extend(_emit_step(index, plan, selectors))
        lines.append("});")
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    return GeneratedScript(
        fileName=_FILENAME,
        content=content,
        testCount=len(cases) + (1 if leftover else 0),
        stepCount=len(plan.steps),
    )


def _has_plan_step(plan: ExplorePlan, index: int) -> bool:
    return any(s.index == index for s in plan.steps)


def _emit_step(index: int, plan: ExplorePlan, selectors: dict[int, Selector]) -> list[str]:
    step = next((s for s in plan.steps if s.index == index), None)
    if step is None:
        return []
    if step.action is Action.navigate:
        return [f"  await page.goto({_ts(step.value or '')});"]
    if step.action is Action.wait:
        return [f"  await page.waitForTimeout({_ts(step.value or '1000')});"]
    if step.action is Action.screenshot:
        return ["  await page.screenshot();"]
    if step.action is Action.assert_:
        return [f"  await expect(page.getByText({_ts(step.assertion or '')})).toBeVisible();"]
    if step.action is Action.press:
        return [f"  await page.keyboard.press({_ts(step.value or 'Enter')});"]
    selector = selectors.get(index)
    if selector is None:
        return [f"  // 步骤 {index}（{step.action.value}）缺少元素定位信息，已跳过"]
    locator = _locator(selector)
    if step.action is Action.click:
        return [f"  await {locator}.click();"]
    if step.action is Action.fill:
        return [f"  await {locator}.fill({_ts(step.value or '')});"]
    if step.action is Action.select:
        return [f"  await {locator}.selectOption({_ts(step.value or '')});"]
    return []
