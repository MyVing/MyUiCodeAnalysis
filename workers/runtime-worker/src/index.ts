/**
 * 运行时采集 Worker 入口。
 *
 * 使用方式（由编排层调用）：
 *   echo '{"projectId":"prj_1","rootPath":"/abs/path","traceId":"trace_1","command":"npm run dev"}' \
 *     | node dist/index.js
 *
 * 设计约束（与 ast-worker 一致）：
 *  - 只通过 stdin/stdout 通信，不写库、不读业务数据。
 *  - 任何失败都以 { ok: false, error } 形式回传，并退出码 0。
 *  - stdout 只输出一行业务 JSON；日志一律走 stderr。
 *  - 无论成功失败，托管启动的 dev server 都会被杀掉，浏览器都会关闭。
 *
 * 三种模式：passive（被动采集 RuntimeTrace）| discover（枚举可交互元素）|
 * explore（确定性执行探索计划），由 request.mode 分发，缺省 passive。
 */

import { collectTrace, launchBrowserWithFallback } from "./collect.js";
import { discoverElements } from "./discover.js";
import { executeExplorePlan } from "./explore.js";
import { parseRequest } from "./parse-request.js";
import type {
  RuntimeWorkerRequest,
  RuntimeWorkerResponse,
  RuntimeWorkerSuccess,
} from "./protocol.js";
import { startDevServer, type DevServerHandle } from "./vite-server.js";

function log(message: string): void {
  process.stderr.write(`[runtime-worker] ${message}\n`);
}

function readStdin(): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    process.stdin.on("data", (chunk: Buffer) => chunks.push(chunk));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  });
}

type BrowserHandle = Awaited<ReturnType<typeof launchBrowserWithFallback>>["browser"];

interface ModeOutcome {
  ok: boolean;
  payload?: Partial<RuntimeWorkerSuccess>;
  error?: { code: string; message: string };
}

/** 打开页面（含 dev server 托管与浏览器启动），执行完回调后由调用方负责清理。 */
async function withPage(
  request: RuntimeWorkerRequest,
  log0: (message: string) => void,
  run: (browser: BrowserHandle, baseUrl: string, server: DevServerHandle | null) => Promise<Partial<RuntimeWorkerSuccess>>,
): Promise<ModeOutcome> {
  let server: DevServerHandle | null = null;
  let browser: BrowserHandle | null = null;
  try {
    let baseUrl: string;
    if (request.url !== undefined) {
      baseUrl = request.url;
    } else {
      server = await startDevServer(
        request.command as string,
        request.rootPath,
        request.serverTimeoutMs ?? 60_000,
        log0,
      );
      baseUrl = server.baseUrl;
    }

    const launched = await launchBrowserWithFallback();
    browser = launched.browser;
    log0(`浏览器：${launched.channel ?? "bundled chromium"}`);
    const payload = await run(browser, baseUrl, server);
    return { ok: true, payload };
  } catch (error) {
    const code =
      typeof (error as { code?: unknown })?.code === "string"
        ? (error as { code: string }).code
        : "collect_failed";
    return {
      ok: false,
      error: { code, message: error instanceof Error ? error.message : String(error) },
    };
  } finally {
    if (browser !== null) {
      await browser.close().catch(() => undefined);
    }
    if (server !== null) {
      await server.close().catch(() => undefined);
    }
  }
}

async function runPassive(request: RuntimeWorkerRequest): Promise<ModeOutcome> {
  return withPage(request, log, async (browser, baseUrl, server) => {
    const devServerInfo: { port: number; command?: string } | null =
      server !== null
        ? request.command !== undefined
          ? { port: server.port, command: request.command }
          : { port: server.port }
        : null;
    const collected = await collectTrace(browser, request, baseUrl, devServerInfo, log);
    return { trace: collected.trace };
  });
}

async function runDiscover(request: RuntimeWorkerRequest): Promise<ModeOutcome> {
  return withPage(request, log, async (browser, baseUrl) => {
    const context = await browser.newContext({ viewport: request.viewport ?? { width: 1280, height: 800 } });
    try {
      const page = await context.newPage();
      try {
        await page.goto(baseUrl, { waitUntil: "load", timeout: 30_000 });
      } catch (error) {
        throw Object.assign(
          new Error(`页面加载失败（${baseUrl}）：${String(error)}`),
          { code: "page_load_failed" },
        );
      }
      const settleMs = request.settleMs ?? 3000;
      await page
        .waitForLoadState("networkidle", { timeout: Math.max(settleMs, 1000) })
        .catch(() => log("networkidle 未在观察窗口内达成，按已加载内容继续枚举"));
      await page.waitForTimeout(settleMs / 2);

      const discovered = await discoverElements(page, request.maxElements ?? 200);
      log(`发现 ${discovered.elements.length} 个可交互元素`);
      return {
        discovery: {
          pageUrl: discovered.pageUrl,
          ...(discovered.title !== undefined ? { title: discovered.title } : {}),
          elements: discovered.elements,
        },
      };
    } finally {
      await context.close().catch(() => undefined);
    }
  });
}

async function runExplore(request: RuntimeWorkerRequest): Promise<ModeOutcome> {
  const steps = request.steps ?? [];
  return withPage(request, log, async (browser, baseUrl) => {
    const context = await browser.newContext({ viewport: request.viewport ?? { width: 1280, height: 800 } });
    try {
      const page = await context.newPage();
      try {
        await page.goto(baseUrl, { waitUntil: "load", timeout: 30_000 });
      } catch (error) {
        throw Object.assign(
          new Error(`页面加载失败（${baseUrl}）：${String(error)}`),
          { code: "page_load_failed" },
        );
      }
      await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => undefined);
      const outcome = await executeExplorePlan(
        page,
        steps,
        { artifactsDir: request.artifactsDir as string },
        log,
      );
      return { explore: outcome };
    } finally {
      await context.close().catch(() => undefined);
    }
  });
}

async function run(request: RuntimeWorkerRequest): Promise<ModeOutcome> {
  switch (request.mode ?? "passive") {
    case "discover":
      return runDiscover(request);
    case "explore":
      return runExplore(request);
    default:
      return runPassive(request);
  }
}

async function main(): Promise<void> {
  let request: RuntimeWorkerRequest;
  try {
    request = parseRequest(await readStdin());
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    log(`请求解析失败：${message}`);
    process.stdout.write(
      JSON.stringify({
        ok: false,
        error: { code: "invalid_request", message },
      } satisfies RuntimeWorkerResponse),
    );
    return;
  }

  const response = await run(request);
  const payload: RuntimeWorkerResponse =
    response.ok && response.payload !== undefined
      ? { ok: true, ...response.payload }
      : {
          ok: false,
          error:
            response.error ?? ({ code: "collect_failed", message: "采集未产出结果" } as const),
        };
  process.stdout.write(JSON.stringify(payload));
  process.stdout.write("\n");
}

void main();
