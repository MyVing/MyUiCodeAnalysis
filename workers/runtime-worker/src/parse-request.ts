/**
 * 请求解析与前置校验：从 stdin 原始文本到 RuntimeWorkerRequest。
 *
 * 校验失败抛错（index 捕获后回传 invalid_request），不让 Worker 带着残缺
 * 请求往下走——dev server 与浏览器都是昂贵资源，越早失败越好。
 */

import { existsSync, statSync } from "node:fs";

import type { PlanStepRequest, RuntimeWorkerRequest, WorkerMode } from "./protocol.js";

const STEP_ACTIONS: readonly PlanStepRequest["action"][] = [
  "click",
  "fill",
  "select",
  "press",
  "wait",
  "screenshot",
  "assert",
  "navigate",
];

/** 需要 targetIndex + selector 的元素操作。 */
const TARGET_ACTIONS: readonly PlanStepRequest["action"][] = ["click", "fill", "select", "press"];

export function parseRequest(raw: string): RuntimeWorkerRequest {
  let parsed: Partial<RuntimeWorkerRequest>;
  try {
    parsed = JSON.parse(raw) as Partial<RuntimeWorkerRequest>;
  } catch (error) {
    throw new Error(`请求不是合法 JSON：${error instanceof Error ? error.message : String(error)}`);
  }
  if (!parsed.projectId || !parsed.rootPath || !parsed.traceId) {
    throw new Error("请求缺少必填字段：projectId / rootPath / traceId");
  }
  if (!parsed.url && !parsed.command) {
    throw new Error("url 与 command 至少给一个：要么直接访问已有服务，要么由 Worker 托管 dev server");
  }
  if (!existsSync(parsed.rootPath) || !statSync(parsed.rootPath).isDirectory()) {
    throw new Error(`rootPath 不存在或不是目录：${parsed.rootPath}`);
  }

  const mode: WorkerMode = parsed.mode ?? "passive";
  if (mode !== "passive" && mode !== "discover" && mode !== "explore") {
    throw new Error(`mode 只支持 passive / discover / explore：${String(mode)}`);
  }

  if (mode === "explore") {
    const steps = parsed.steps;
    if (!Array.isArray(steps) || steps.length === 0) {
      throw new Error("explore 模式必须提供非空的 steps 计划步骤");
    }
    if (steps.length > 50) {
      throw new Error("explore 计划步数超过上限 50");
    }
    steps.forEach((step, position) => {
      validateStep(step, position);
    });
    if (!parsed.artifactsDir) {
      throw new Error("explore 模式必须提供 artifactsDir（截图产物目录）");
    }
  }

  return { ...parsed, mode } as RuntimeWorkerRequest;
}

function validateStep(step: PlanStepRequest, position: number): void {
  const where = `steps[${position}]`;
  if (!step || typeof step !== "object") {
    throw new Error(`${where} 不是对象`);
  }
  if (!Number.isInteger(step.index) || step.index < 0) {
    throw new Error(`${where}.index 必须是非负整数`);
  }
  if (!STEP_ACTIONS.includes(step.action)) {
    throw new Error(`${where}.action 非法：${String(step.action)}`);
  }
  if (TARGET_ACTIONS.includes(step.action)) {
    if (!step.selector || !step.selector.strategy || !step.selector.value) {
      throw new Error(`${where}（${step.action}）必须提供 selector（strategy + value）`);
    }
  }
  if (step.action === "fill" && typeof step.value !== "string") {
    throw new Error(`${where}（fill）必须提供 value`);
  }
  if (step.action === "select" && typeof step.value !== "string") {
    throw new Error(`${where}（select）必须提供 value`);
  }
  if (step.action === "press" && typeof step.value !== "string") {
    throw new Error(`${where}（press）必须提供 value`);
  }
  if (step.action === "navigate" && typeof step.value !== "string") {
    throw new Error(`${where}（navigate）必须提供 value`);
  }
  if (step.action === "assert" && !step.assertion) {
    throw new Error(`${where}（assert）必须提供 assertion`);
  }
}
