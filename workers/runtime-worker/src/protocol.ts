/**
 * 运行时采集 Worker 的协议层。
 *
 * 与 ast-worker 同构：stdin/stdout 交换 JSON，不共享进程内存。
 *   stdin  <- RuntimeWorkerRequest
 *   stdout -> RuntimeWorkerResponse（内含 RuntimeTrace 或 error）
 *
 * 协议字段刻意不依赖 RuntimeTrace 契约类型，保证 Worker 崩溃时编排层仍能
 * 解析出失败原因；trace 的形状校验由后端按 runtime.schema.json 完成。
 */

/** 编排层下发的采集请求。 */
export interface RuntimeWorkerRequest {
  projectId: string;
  /** 被采集项目根目录（绝对路径）；file 归一化与 dev server 工作目录都基于它 */
  rootPath: string;
  /** 本次采集记录的唯一标识，写入 RuntimeTrace.traceId */
  traceId: string;
  /** 关联的静态分析产物标识 */
  analysisId?: string;
  /**
   * 页面 URL。给定后直接访问，不托管 dev server（适合「服务已在跑」的场景）。
   */
  url?: string;
  /**
   * dev server 启动命令（如 "npm run dev"）。给定后 Worker 在 rootPath 下托管
   * 启动、解析端口、采集完成后杀掉进程树。url 与 command 至少给一个。
   */
  command?: string;
  /**
   * dev server 启动超时（毫秒），缺省 60_000。
   */
  serverTimeoutMs?: number;
  /**
   * 页面加载后的静默观察窗口（毫秒）：等待被动网络请求落定，缺省 3_000。
   */
  settleMs?: number;
  /** 视口，缺省 1280x800 */
  viewport?: { width: number; height: number };
}

/** Worker 成功响应。trace 形状由 shared/schemas/runtime.schema.json 约定。 */
export interface RuntimeWorkerSuccess {
  ok: true;
  trace: unknown;
}

/** Worker 失败响应；失败也走正常退出码，避免编排层只能靠 kill 判断。 */
export interface RuntimeWorkerFailure {
  ok: false;
  error: {
    code: string;
    message: string;
  };
}

export type RuntimeWorkerResponse = RuntimeWorkerSuccess | RuntimeWorkerFailure;

/** Worker 内部已知的失败码，编排层可直接透传。 */
export type RuntimeWorkerErrorCode =
  | "invalid_request"
  | "dev_server_failed"
  | "browser_unavailable"
  | "page_load_failed"
  | "collect_failed";
