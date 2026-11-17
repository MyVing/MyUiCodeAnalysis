/**
 * 请求解析与前置校验：从 stdin 原始文本到 RuntimeWorkerRequest。
 *
 * 校验失败抛错（index 捕获后回传 invalid_request），不让 Worker 带着残缺
 * 请求往下走——dev server 与浏览器都是昂贵资源，越早失败越好。
 */

import { existsSync, statSync } from "node:fs";

import type { RuntimeWorkerRequest } from "./protocol.js";

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
  return parsed as RuntimeWorkerRequest;
}
