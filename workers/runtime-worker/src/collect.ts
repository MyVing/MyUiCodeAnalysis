/**
 * 被动采集：加载页面 → 记录网络/控制台/页面错误 → 提取组件树 → 组装 RuntimeTrace。
 *
 * 被动原则：不点击、不输入、不遍历路由。观察窗口只等待页面自身发起的请求落定。
 */

import { chromium, type Browser, type BrowserContext, type Page } from "playwright-core";

import { extractComponentTree } from "./extract.js";
import { installCollector } from "./install-collector.js";
import { normalizeSourceFile, toUnixSeconds } from "./normalize.js";
import type { RuntimeWorkerRequest } from "./protocol.js";

/** 与 runtime.schema.json 的 ComponentNode 对齐（rawFile 由本模块归一化前使用）。 */
interface RawComponentNode {
  name: string;
  source: string;
  children: RawComponentNode[];
  rawFile?: string;
  line?: number;
  col?: number;
}

export interface CollectedTrace {
  trace: Record<string, unknown>;
}

export class CollectError extends Error {
  constructor(
    readonly code: "browser_unavailable" | "page_load_failed" | "collect_failed",
    message: string,
  ) {
    super(message);
  }
}

/** Edge → Chrome → bundled chromium 的解析顺序；都不可用时给出可操作的错误。 */
export async function launchBrowserWithFallback(): Promise<{ browser: Browser; channel: string | null }> {
  for (const channel of ["msedge", "chrome"]) {
    try {
      const browser = await chromium.launch({ channel, headless: true });
      return { browser, channel };
    } catch {
      // 换下一个候选
    }
  }
  try {
    const browser = await chromium.launch({ headless: true });
    return { browser, channel: null };
  } catch {
    throw new CollectError(
      "browser_unavailable",
      "未找到可用的浏览器（依次尝试 Edge、Chrome、bundled Chromium）。" +
        "请安装 Microsoft Edge 或 Chrome，或执行 npx playwright install chromium。",
    );
  }
}

/** rawFile → 契约字段：归一化失败则去掉 file（source 保留，证据如实降级）。 */
function mapRawTree(nodes: RawComponentNode[], rootPath: string): Record<string, unknown>[] {
  return nodes.map((node) => {
    const mapped: Record<string, unknown> = {
      name: node.name,
      source: node.source,
      children: mapRawTree(node.children ?? [], rootPath),
    };
    const file = node.rawFile !== undefined ? normalizeSourceFile(node.rawFile, rootPath) : undefined;
    if (file !== undefined) {
      mapped.file = file;
    }
    if (typeof node.line === "number" && node.line >= 1) {
      mapped.line = node.line;
    }
    if (typeof node.col === "number" && node.col >= 1) {
      mapped.col = node.col;
    }
    return mapped;
  });
}

function countNodes(nodes: Record<string, unknown>[]): number {
  return nodes.reduce((sum, node) => sum + 1 + countNodes((node.children as Record<string, unknown>[]) ?? []), 0);
}

/**
 * 执行一次被动采集。context 必须是未访问过目标页面的新 context——
 * addInitScript 只对之后创建的页面生效。
 */
export async function collectTrace(
  browser: Browser,
  request: RuntimeWorkerRequest,
  baseUrl: string,
  devServer: { port: number; command?: string } | null,
  log: (message: string) => void,
): Promise<CollectedTrace> {
  const settleMs = request.settleMs ?? 3000;
  const viewport = request.viewport ?? { width: 1280, height: 800 };

  const context: BrowserContext = await browser.newContext({
    viewport,
  });
  let page: Page | null = null;
  try {
    await context.addInitScript(installCollector);

    const networkEvents: Record<string, unknown>[] = [];
    const consoleEvents: Record<string, unknown>[] = [];
    const pageErrors: Record<string, unknown>[] = [];
    let eventIdSeq = 0;

    page = await context.newPage();
    page.on("response", (response) => {
      const request0 = response.request();
      eventIdSeq += 1;
      const event: Record<string, unknown> = {
        eventId: `net_${eventIdSeq}`,
        method: request0.method(),
        url: response.url(),
        status: response.status(),
        resourceType: request0.resourceType(),
      };
      const contentType = response.headers()["content-type"];
      if (contentType !== undefined) {
        event.contentType = contentType;
      }
      event.timestamp = toUnixSeconds(Date.now());
      networkEvents.push(event);
    });
    page.on("console", (message) => {
      // playwright 的 'warning' 对齐契约里的 'warn'
      const level = message.type() === "warning" ? "warn" : message.type();
      const event: Record<string, unknown> = { level, message: message.text() };
      const location = message.location();
      if (location.url) {
        event.location = `${location.url}:${location.lineNumber ?? 0}`;
      }
      event.timestamp = toUnixSeconds(Date.now());
      consoleEvents.push(event);
    });
    page.on("pageerror", (error) => {
      const event: Record<string, unknown> = { message: error.message };
      if (error.name) {
        event.name = error.name;
      }
      if (error.stack) {
        event.stack = error.stack;
      }
      event.timestamp = toUnixSeconds(Date.now());
      pageErrors.push(event);
    });

    log(`访问 ${baseUrl}，静默观察 ${settleMs}ms`);
    try {
      await page.goto(baseUrl, { waitUntil: "load", timeout: 30_000 });
    } catch (error) {
      throw new CollectError("page_load_failed", `页面加载失败（${baseUrl}）：${String(error)}`);
    }

    // 静默观察窗口：不操作页面，只等被动请求落定
    await page.waitForLoadState("networkidle", { timeout: Math.max(settleMs, 1000) }).catch(() => {
      log("networkidle 未在观察窗口内达成，按已捕获内容继续（被动采集不重试、不点击）");
    });
    await page.waitForTimeout(settleMs / 2);

    const extract = (await page.evaluate(extractComponentTree, 2000)) as unknown as {
      ok: boolean;
      tree?: RawComponentNode[];
      error?: string;
    } | null;
    if (extract === null || typeof extract !== "object") {
      throw new CollectError("collect_failed", "组件树提取未返回结果（注入脚本未生效）");
    }
    if (!extract.ok) {
      throw new CollectError("collect_failed", `组件树提取失败：${extract.error ?? "未知错误"}`);
    }
    const tree = mapRawTree(extract.tree ?? [], request.rootPath);
    log(`组件树：${countNodes(tree)} 个组件实例，网络事件 ${networkEvents.length} 条`);

    const trace: Record<string, unknown> = {
      schemaVersion: "1.0",
      traceId: request.traceId,
      projectId: request.projectId,
      mode: "passive",
      page: {
        url: baseUrl,
        title: await page.title().catch(() => undefined),
        timestamp: toUnixSeconds(Date.now()),
        viewport,
      },
      componentTree: tree,
    };
    if (request.analysisId !== undefined) {
      trace.analysisId = request.analysisId;
    }
    if (devServer !== null) {
      const devServerInfo: Record<string, unknown> = { port: devServer.port };
      if (devServer.command !== undefined) {
        devServerInfo.command = devServer.command;
      }
      trace.devServer = devServerInfo;
    }
    if (networkEvents.length > 0) {
      trace.networkEvents = networkEvents;
    }
    if (consoleEvents.length > 0) {
      trace.consoleEvents = consoleEvents;
    }
    if (pageErrors.length > 0) {
      trace.pageErrors = pageErrors;
    }
    return { trace };
  } finally {
    await context.close().catch(() => undefined);
  }
}
