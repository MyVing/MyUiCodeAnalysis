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
  /**
   * 采集模式：passive（缺省，被动采集 RuntimeTrace）| discover（枚举可交互
   * 元素）| explore（确定性执行探索计划）。discover 与 explore 是 P5 的
   * AI 自动探索链路，产物形状由 shared/schemas/explore.schema.json 约定。
   */
  mode?: WorkerMode;
  /** discover：可交互元素枚举上限，缺省 200 */
  maxElements?: number;
  /** explore：要执行的确定性计划步骤（selector 已由后端从 discovery 解析） */
  steps?: PlanStepRequest[];
  /** explore：截图产物目录（绝对路径）；Worker 写 step-NNN.png 并回传文件名 */
  artifactsDir?: string;
}

export type WorkerMode = "passive" | "discover" | "explore";

/** explore 计划步骤的执行形态（契约 PlanStep 的裁剪版，只留执行所需字段）。 */
export interface PlanStepRequest {
  index: number;
  action: "click" | "fill" | "select" | "press" | "wait" | "screenshot" | "assert" | "navigate";
  /** 与契约 Selector 对齐 */
  selector?: { strategy: "role" | "testId" | "css" | "text"; value: string; name?: string };
  value?: string;
  assertion?: string;
  optional?: boolean;
}

/** discover 模式的响应产物：页面上真实存在的可交互元素清单。 */
export interface DiscoverOutcome {
  pageUrl: string;
  title?: string;
  elements: DiscoverElement[];
}

export interface DiscoverElement {
  index: number;
  tag: string;
  role: string;
  name: string;
  type?: string;
  testId?: string;
  href?: string;
  visible: boolean;
  selector: { strategy: "role" | "testId" | "css" | "text"; value: string; name?: string };
}

/** explore 模式的响应产物：逐步执行结果与截图文件名。 */
export interface ExploreOutcome {
  pageUrl: string;
  title?: string;
  results: ExploreStepOutcome[];
  screenshots: { stepIndex: number; fileName: string }[];
}

export interface ExploreStepOutcome {
  index: number;
  action: PlanStepRequest["action"];
  target: string;
  selector?: PlanStepRequest["selector"];
  valueSummary?: string;
  result: "success" | "failed" | "skipped" | "no_change";
  error?: string;
  screenshotRef?: string;
  triggeredNetworkEvents?: string[];
  pageTransitioned?: boolean;
  url?: string;
}

/** Worker 成功响应。trace / discovery / explore 的形状分别由
 * runtime.schema.json 与 explore.schema.json 约定。 */
export interface RuntimeWorkerSuccess {
  ok: true;
  trace?: unknown;
  discovery?: unknown;
  explore?: unknown;
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
  | "collect_failed"
  | "plan_invalid"
  | "explore_failed";
