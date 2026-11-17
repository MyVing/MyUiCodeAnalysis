/**
 * dev server 生命周期托管：启动 → 解析端口 → TCP 就绪 → 采集 → 杀进程树。
 *
 * Windows 上 npm run dev 会衍生 node 子进程，proc.kill() 只杀 shell，
 * 必须用 `taskkill /T /F` 杀整棵树；其余平台按 SIGTERM → SIGKILL 处理。
 */

import { spawn, type ChildProcess } from "node:child_process";
import net from "node:net";

import { parseServerPorts, toBaseUrl } from "./ports.js";

export interface DevServerHandle {
  baseUrl: string;
  port: number;
  close(): Promise<void>;
}

export class DevServerError extends Error {
  constructor(
    readonly code: "spawn_failed" | "port_not_found" | "start_timeout" | "stop_failed",
    message: string,
  ) {
    super(message);
  }
}

type Logger = (message: string) => void;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function tcpPoll(host: string, port: number, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port });
    const timer = setTimeout(() => {
      socket.destroy();
      resolve(false);
    }, timeoutMs);
    socket.once("connect", () => {
      clearTimeout(timer);
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => {
      clearTimeout(timer);
      resolve(false);
    });
  });
}

async function waitForPort(
  proc: ChildProcess,
  stdout: () => string,
  host: string,
  serverTimeoutMs: number,
): Promise<number> {
  const started = Date.now();
  let lastPorts: number[] = [];
  while (Date.now() - started < serverTimeoutMs) {
    const ports = parseServerPorts(stdout());
    if (ports.length > 0) {
      const port = ports[0] as number;
      if (await tcpPoll(host, port, 1000)) {
        return port;
      }
      lastPorts = ports;
    }
    if (proc.exitCode !== null || proc.signalCode !== null) {
      throw new DevServerError("spawn_failed", `dev server 进程提前退出（exit=${proc.exitCode}）`);
    }
    await sleep(200);
  }
  throw new DevServerError(
    "start_timeout",
    `dev server ${serverTimeoutMs}ms 内未就绪（日志中${lastPorts.length > 0 ? `解析到端口 ${lastPorts.join(", ")} 但 TCP 未通` : "未出现 localhost 端口"}）`,
  );
}

async function killProcessTree(proc: ChildProcess, log: Logger): Promise<void> {
  const pid = proc.pid;
  if (pid === undefined || proc.exitCode !== null) {
    return;
  }
  if (process.platform === "win32") {
    await new Promise<void>((resolve) => {
      // shell:true 在 Windows 下是 cmd.exe → node 的进程树，proc.kill 只杀 cmd
      const killer = spawn(`taskkill /pid ${pid} /T /F`, { shell: true, stdio: "ignore" });
      killer.on("close", () => resolve());
      killer.on("error", () => resolve());
    });
    return;
  }
  proc.kill("SIGTERM");
  const exited = await Promise.race([
    new Promise<boolean>((resolve) => proc.once("exit", () => resolve(true))),
    sleep(3000).then(() => false),
  ]);
  if (!exited && proc.exitCode === null) {
    log("dev server 未响应 SIGTERM，升级为 SIGKILL");
    proc.kill("SIGKILL");
  }
}

/** 托管启动 dev server 并等待端口就绪。日志重定向到 stderr，避免污染 stdout 协议。 */
export async function startDevServer(
  command: string,
  cwd: string,
  serverTimeoutMs: number,
  log: Logger,
): Promise<DevServerHandle> {
  let stdoutText = "";
  let proc: ChildProcess;
  try {
    proc = spawn(command, {
      cwd,
      shell: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    throw new DevServerError("spawn_failed", `无法启动 dev server（${command}）：${String(error)}`);
  }

  proc.stdout?.on("data", (chunk: Buffer) => {
    stdoutText += chunk.toString();
  });
  proc.stderr?.on("data", (chunk: Buffer) => {
    log(`[dev-server] ${chunk.toString().trimEnd()}`);
  });

  try {
    const port = await waitForPort(proc, () => stdoutText, "localhost", serverTimeoutMs);
    log(`dev server 就绪：localhost:${port}`);
    return {
      baseUrl: toBaseUrl("localhost", port),
      port,
      close: async () => {
        await killProcessTree(proc, log);
      },
    };
  } catch (error) {
    // 启动失败也不能留下孤儿进程树：waitForPort 抛错时进程可能已经绑定端口
    await killProcessTree(proc, log);
    throw error;
  }
}
