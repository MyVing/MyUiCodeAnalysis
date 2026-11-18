/**
 * explore 模式：确定性执行探索计划。
 *
 * 核心约束：执行只认计划里的 selector，不做任何页面内容驱动的决策——
 * 「AI 决策」发生在编排层预生成计划时，Worker 只负责忠实执行并如实记录。
 * 失败处理：可选步骤失败继续；非可选步骤失败后，剩余步骤全部标记 skipped。
 */

import { mkdirSync } from "node:fs";
import { join } from "node:path";

import type { Page } from "playwright-core";

import type { ExploreOutcome, ExploreStepOutcome, PlanStepRequest } from "./protocol.js";

export class ExploreError extends Error {
  constructor(
    readonly code: "plan_invalid" | "page_load_failed" | "explore_failed",
    message: string,
  ) {
    super(message);
  }
}

const STEP_TIMEOUT_MS = 8_000;
const ASSERT_TIMEOUT_MS = 5_000;
const SETTLE_BETWEEN_STEPS_MS = 500;

/** selector → Playwright locator；与后端下发契约一一对应。 */
function resolveLocator(page: Page, step: PlanStepRequest) {
  const selector = step.selector;
  if (!selector) {
    throw new ExploreError("plan_invalid", `步骤 ${step.index} 缺少 selector`);
  }
  switch (selector.strategy) {
    case "role": {
      const options: { name?: string } = {};
      if (selector.name) {
        options.name = selector.name;
      }
      // role 值来自探索计划的枚举产物，AriaRole 的字面量联合在此处无法静态穷举
      return page
        .getByRole(selector.value as Parameters<Page["getByRole"]>[0], options)
        .first();
    }
    case "testId":
      // Playwright 默认 testIdAttribute 即 data-testid
      return page.getByTestId(selector.value).first();
    case "text":
      return page.getByText(selector.value, { exact: false }).first();
    case "css":
      return page.locator(selector.value).first();
  }
}

function truncate(text: string, limit = 200): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export interface ExploreRunContext {
  artifactsDir: string;
}

export async function executeExplorePlan(
  page: Page,
  steps: PlanStepRequest[],
  context: ExploreRunContext,
  log: (message: string) => void,
): Promise<ExploreOutcome> {
  const startUrl = page.url();
  const startTitle = await page.title().catch(() => undefined);

  try {
    mkdirSync(context.artifactsDir, { recursive: true });
  } catch (error) {
    throw new ExploreError("explore_failed", `无法创建截图目录 ${context.artifactsDir}：${String(error)}`);
  }

  // 网络事件按步骤归因：记录总量游标，每步结束取增量
  const networkLog: string[] = [];
  page.on("request", (request) => {
    networkLog.push(`${request.method()} ${truncate(request.url(), 180)}`);
  });

  const results: ExploreStepOutcome[] = [];
  const screenshots: { stepIndex: number; fileName: string }[] = [];
  let aborted = false;

  for (const step of steps) {
    if (aborted) {
      results.push({
        index: step.index,
        action: step.action,
        target: stepTargetLabel(step),
        result: "skipped",
        error: "前序非可选步骤失败，本步骤被跳过",
      });
      continue;
    }

    const urlBefore = page.url();
    const titleBefore = await page.title().catch(() => undefined);
    const networkBefore = networkLog.length;
    const outcome = await runStep(page, step, context, screenshots, log);
    const networkAfter = networkLog.length;

    const triggered = networkLog.slice(networkBefore, networkAfter);
    const pageTransitioned = page.url() !== urlBefore;
    // 页面可见变化口径：URL 变化、新网络请求、标题变化，任一成立即视为有变化
    const titleAfter = await page.title().catch(() => undefined);
    const noChange =
      outcome.result === "success" &&
      !pageTransitioned &&
      triggered.length === 0 &&
      titleAfter === titleBefore;

    const enriched: ExploreStepOutcome = {
      ...outcome,
      ...(triggered.length > 0 ? { triggeredNetworkEvents: triggered } : {}),
      pageTransitioned,
      url: page.url(),
    };
    if (noChange) {
      enriched.result = "no_change";
    }
    results.push(enriched);

    if (outcome.result === "failed" && !step.optional) {
      aborted = true;
      log(`步骤 ${step.index}（${step.action}）失败且不可选，后续步骤跳过`);
    } else {
      await page.waitForTimeout(SETTLE_BETWEEN_STEPS_MS);
    }
  }

  return {
    pageUrl: startUrl,
    ...(startTitle !== undefined ? { title: startTitle } : {}),
    results,
    screenshots,
  };
}

function stepTargetLabel(step: PlanStepRequest): string {
  if (step.selector) {
    const name = step.selector.name ? ` name="${step.selector.name}"` : "";
    return `${step.selector.strategy}:${step.selector.value}${name}`;
  }
  if (step.action === "assert") {
    return `文本「${step.assertion ?? ""}」可见`;
  }
  if (step.action === "navigate") {
    return step.value ?? "";
  }
  return step.action;
}

/** 值摘要：输入值原样记录（契约层注明敏感字段需脱敏，编排层负责判断）。 */
function valueSummary(step: PlanStepRequest): string | undefined {
  if (step.value !== undefined) {
    return truncate(step.value, 120);
  }
  return undefined;
}

async function takeScreenshot(
  page: Page,
  stepIndex: number,
  context: ExploreRunContext,
  screenshots: { stepIndex: number; fileName: string }[],
): Promise<string | undefined> {
  const fileName = `step-${String(stepIndex).padStart(3, "0")}.png`;
  try {
    await page.screenshot({ path: join(context.artifactsDir, fileName), fullPage: false });
    screenshots.push({ stepIndex, fileName });
    return fileName;
  } catch {
    // 截图失败不影响步骤本身的成败，只记录
    return undefined;
  }
}

async function runStep(
  page: Page,
  step: PlanStepRequest,
  context: ExploreRunContext,
  screenshots: { stepIndex: number; fileName: string }[],
  log: (message: string) => void,
): Promise<ExploreStepOutcome> {
  const base: ExploreStepOutcome = {
    index: step.index,
    action: step.action,
    target: stepTargetLabel(step),
    result: "success",
  };
  const summary = valueSummary(step);
  if (summary !== undefined) {
    base.valueSummary = summary;
  }

  try {
    switch (step.action) {
      case "click": {
        const locator = resolveLocator(page, step);
        await locator.click({ timeout: STEP_TIMEOUT_MS });
        break;
      }
      case "fill": {
        const locator = resolveLocator(page, step);
        await locator.fill(step.value ?? "", { timeout: STEP_TIMEOUT_MS });
        break;
      }
      case "select": {
        const locator = resolveLocator(page, step);
        await locator.selectOption(step.value ?? "", { timeout: STEP_TIMEOUT_MS });
        break;
      }
      case "press": {
        await page.keyboard.press(step.value ?? "Enter");
        break;
      }
      case "wait": {
        const ms = Number.parseInt(step.value ?? "1000", 10);
        await page.waitForTimeout(Number.isFinite(ms) && ms > 0 ? Math.min(ms, 10_000) : 1000);
        break;
      }
      case "screenshot": {
        const ref = await takeScreenshot(page, step.index, context, screenshots);
        if (ref !== undefined) {
          base.screenshotRef = ref;
        }
        break;
      }
      case "assert": {
        await page
          .getByText(step.assertion ?? "", { exact: false })
          .first()
          .waitFor({ state: "visible", timeout: ASSERT_TIMEOUT_MS });
        break;
      }
      case "navigate": {
        await page.goto(step.value ?? page.url(), { waitUntil: "load", timeout: 30_000 });
        break;
      }
    }

    // 每步执行后都拍一张截图（B 模式每步必拍），screenshot 动作除外（已拍）
    if (step.action !== "screenshot") {
      const ref = await takeScreenshot(page, step.index, context, screenshots);
      if (ref !== undefined) {
        base.screenshotRef = ref;
      }
    }

    log(`步骤 ${step.index}（${step.action} ${base.target}）→ ${base.result}`);
    return base;
  } catch (error) {
    const failed: ExploreStepOutcome = {
      ...base,
      result: "failed",
      error: truncate(error instanceof Error ? error.message : String(error), 300),
    };
    // 失败也留一张现场截图，便于排查页面到底处于什么状态
    const ref = await takeScreenshot(page, step.index, context, screenshots);
    if (ref !== undefined) {
      failed.screenshotRef = ref;
    }
    log(`步骤 ${step.index}（${step.action} ${base.target}）失败：${failed.error}`);
    return failed;
  }
}
