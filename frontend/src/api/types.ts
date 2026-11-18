import type { ExploreArtifact, RequirementMap } from "@uicodeanalysis/contracts";

export type Framework = "react" | "next";

export type StorageBackend = "memory" | "mysql";

/** local = 用户登记的本机目录；git = 平台克隆的受管仓库 */
export type ProjectSource = "local" | "git";

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
  source: ProjectSource;
  remoteUrl?: string | null;
  gitBranch?: string | null;
  commitSha?: string | null;
  lastSyncedAt?: string | null;
}

export interface ProjectCreate {
  name: string;
  rootPath: string;
  framework: Framework;
}

/** 按 Git 地址接入；username/password 或 token 仅用于一次克隆，不回显 */
export interface ProjectGitCreate {
  gitUrl: string;
  name?: string;
  branch?: string;
  username?: string;
  password?: string;
  token?: string;
  framework?: Framework;
}

/** 源码片段：点开「文件:行」时由后端读取 */
export interface SourceExcerpt {
  file: string;
  startLine: number;
  endLine: number;
  content: string;
  truncated: boolean;
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

/** 按需求采集输入：requirement 文本与 requirementTaskId 二选一；baseUrl 必填（页面需已在运行） */
export interface RequirementCollectStart {
  requirement?: string;
  requirementTaskId?: string;
  baseUrl: string;
  dryRun?: boolean;
  settleMs?: number;
}

/** 未能转成采集任务的页面，带机器可读原因（无路由 / 路由歧义 / 动态参数） */
export interface SkippedRoute {
  pageNodeId: string;
  routePath?: string | null;
  reason: string;
  message: string;
}

export interface RequirementCollectResult {
  requirementTaskId: string;
  items: RuntimeTask[];
  skipped: SkippedRoute[];
}

/** 需求定位输入：dryRun=true 只跑确定性召回（不调模型） */
export interface RequirementLocateStart {
  requirement: string;
  dryRun: boolean;
  analysisId?: string;
}

export interface RequirementTask {
  id: string;
  projectId: string;
  requirementId: string;
  status: TaskStatus;
  dryRun: boolean;
  requirement: string;
  analysisId?: string | null;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  candidateCount?: number | null;
  matchCount?: number | null;
  warningCount?: number | null;
}

export interface RequirementTaskList {
  items: RequirementTask[];
}

export type RequirementMapArtifact = RequirementMap;

/** AI 自动探索输入：url 与 command 二选一；dryRun=true 只产出计划不执行 */
export interface ExploreStart {
  goal: string;
  command?: string;
  url?: string;
  analysisId?: string;
  dryRun: boolean;
  maxSteps?: number;
}

export interface ExploreTask {
  id: string;
  projectId: string;
  goal: string;
  status: TaskStatus;
  command?: string | null;
  url?: string | null;
  analysisId?: string | null;
  dryRun: boolean;
  maxSteps?: number | null;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  elementCount?: number | null;
  stepCount?: number | null;
  successCount?: number | null;
  failedCount?: number | null;
  screenshotCount?: number | null;
}

export interface ExploreTaskList {
  items: ExploreTask[];
}

export type ExploreArtifactData = ExploreArtifact;
