export type Framework = "react" | "next";

export type StorageBackend = "memory" | "mysql";

export interface HealthStatus {
  status: "ok";
  name: string;
  version: string;
  storage: StorageBackend;
  serverTime: string;
}

export interface Project {
  id: string;
  name: string;
  rootPath: string;
  framework: Framework;
  createdAt: string;
  storage: StorageBackend;
}

export interface ProjectCreate {
  name: string;
  rootPath: string;
  framework: Framework;
}

export interface PluginDependency {
  pluginId: string;
  versionRange?: string;
}

export interface PluginManifest {
  pluginId: string;
  name: string;
  type: "ui-framework" | "runtime" | "analyzer";
  version: string;
  entrypoint?: string;
  dependencies: PluginDependency[];
  capabilities: string[];
  schemaVersion: string;
  /** 按项目保存的启停状态，不属于插件清单本身 */
  enabled: boolean;
}

export interface PluginList {
  items: PluginManifest[];
  isolation: "subprocess";
  note: string;
}

export type TaskStatus = "queued" | "running" | "succeeded" | "failed";

/** 分析任务的可选输入；需求与 Commit 字段为模式 A/C 预留，本期后端只接收存储 */
export interface AnalysisStart {
  requirement?: string;
  baseCommit?: string;
  targetCommit?: string;
}

export interface AnalysisTask {
  id: string;
  projectId: string;
  analysisId: string;
  status: TaskStatus;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  nodeCount?: number | null;
  edgeCount?: number | null;
  warningCount?: number | null;
  requirement?: string | null;
  baseCommit?: string | null;
  targetCommit?: string | null;
}

export interface AnalysisTaskList {
  items: AnalysisTask[];
}

/** 运行时采集的输入：command（托管启动 dev server）与 url（已运行页面）二选一 */
export interface RuntimeStart {
  command?: string;
  url?: string;
  analysisId?: string;
  settleMs?: number;
}

export interface RuntimeTask {
  id: string;
  projectId: string;
  traceId: string;
  status: TaskStatus;
  command?: string | null;
  url?: string | null;
  analysisId?: string | null;
  settleMs?: number | null;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  componentCount?: number | null;
  networkCount?: number | null;
  consoleCount?: number | null;
  interactionCount?: number | null;
}

export interface RuntimeTaskList {
  items: RuntimeTask[];
}
