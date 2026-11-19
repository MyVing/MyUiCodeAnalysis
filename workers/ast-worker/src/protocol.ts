/**
 * 静态分析 Worker 的协议层。
 *
 * 与编排层通过 stdin/stdout 交换 JSON，不共享进程内存：
 *   stdin  <- WorkerRequest
 *   stdout -> WorkerResponse（内含 IRGraph 或 error）
 *
 * 协议字段刻意不依赖 IRGraph，保证 Worker 崩溃时编排层仍能解析出失败原因。
 */

/** 编排层下发的分析请求。 */
export interface WorkerRequest {
  projectId: string;
  /** 被分析项目根目录（绝对路径） */
  rootPath: string;
  /** 本次分析产物标识，写入 IRGraph.analysisId */
  analysisId: string;
  /**
   * 增量分析的文件集合（相对 rootPath）。
   * 缺省表示全量分析。
   */
  changedFiles?: string[];
  /** 项目类型，决定路由与 RSC 语义 */
  framework?: "react" | "next";
  /** 可选源码目录与生产代码排除策略。 */
  sourceRoots?: string[];
  excludeTests?: boolean;
  excludeStorybook?: boolean;
}

/** Worker 成功响应。 */
export interface WorkerSuccess {
  ok: true;
  graph: unknown;
}

/** Worker 失败响应；失败也走正常退出码，避免编排层只能靠 kill 判断。 */
export interface WorkerFailure {
  ok: false;
  error: {
    code: string;
    message: string;
  };
}

export type WorkerResponse = WorkerSuccess | WorkerFailure;
