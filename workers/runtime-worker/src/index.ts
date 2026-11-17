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
 */

import { collectTrace, launchBrowserWithFallback } from "./collect.js";
import { parseRequest } from "./parse-request.js";
import type { RuntimeWorkerRequest, RuntimeWorkerResponse } from "./protocol.js";
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

interface CollectOutcome {
  ok: boolean;
  trace?: unknown;
  error?: { code: string; message: string };
}

async function run(request: RuntimeWorkerRequest): Promise<CollectOutcome> {
  let server: DevServerHandle | null = null;
  let browser: Awaited<ReturnType<typeof launchBrowserWithFallback>>["browser"] | null = null;
  try {
    // 1. 页面来源：url 直连，或托管 dev server（close 保证 finally 必杀）
    let baseUrl: string;
    if (request.url !== undefined) {
      baseUrl = request.url;
    } else {
      server = await startDevServer(
        request.command as string,
        request.rootPath,
        request.serverTimeoutMs ?? 60_000,
        log,
      );
      baseUrl = server.baseUrl;
    }

    // 2. 浏览器与采集
    const launched = await launchBrowserWithFallback();
    browser = launched.browser;
    log(`浏览器：${launched.channel ?? "bundled chromium"}`);

    const devServerInfo: { port: number; command?: string } | null =
      server !== null
        ? request.command !== undefined
          ? { port: server.port, command: request.command }
          : { port: server.port }
        : null;
    const collected = await collectTrace(browser, request, baseUrl, devServerInfo, log);
    return { ok: true, trace: collected.trace };
  } catch (error) {
    const code = typeof (error as { code?: unknown })?.code === "string" ? (error as { code: string }).code : "collect_failed";
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
    response.ok && response.trace !== undefined
      ? { ok: true, trace: response.trace }
      : {
          ok: false,
          error:
            response.error ??
            ({ code: "collect_failed", message: "采集未产出结果" } as const),
        };
  process.stdout.write(JSON.stringify(payload));
  process.stdout.write("\n");
}

void main();
