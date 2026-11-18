import type { IRGraph, RequirementMap, RuntimeTrace } from "@uicodeanalysis/contracts";
import type {
  AnalysisStart,
  AnalysisTask,
  AnalysisTaskList,
  HealthStatus,
  PluginList,
  Project,
  ProjectCreate,
  ProjectGitCreate,
  RequirementLocateStart,
  RequirementTask,
  RequirementTaskList,
  RuntimeStart,
  RuntimeTask,
  RuntimeTaskList,
  SourceExcerpt,
} from "@/api/types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  /** 后端结构化错误的机器码，例如 git_project_exists */
  readonly code?: string;
  /** 冲突时指向已存在的项目，前端可直接跳转 */
  readonly projectId?: string;

  constructor(
    status: number,
    message: string,
    extra: { code?: string; projectId?: string } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = extra.code;
    this.projectId = extra.projectId;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });

  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`;
    let code: string | undefined;
    let projectId: string | undefined;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (body.detail && typeof body.detail === "object") {
        const structured = body.detail as {
          message?: unknown;
          code?: unknown;
          projectId?: unknown;
        };
        if (typeof structured.message === "string") detail = structured.message;
        if (typeof structured.code === "string") code = structured.code;
        if (typeof structured.projectId === "string") projectId = structured.projectId;
      }
    } catch {
      // 非 JSON 错误体，保留默认提示
    }
    throw new ApiError(response.status, detail, { code, projectId });
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  getHealth: () => request<HealthStatus>("/health"),

  listProjects: () => request<Project[]>("/projects"),

  createProject: (payload: ProjectCreate) =>
    request<Project>("/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  createGitProject: (payload: ProjectGitCreate) =>
    request<Project>("/projects/git", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  syncProject: (projectId: string) =>
    request<Project>(`/projects/${projectId}/sync`, { method: "POST" }),

  getProject: (projectId: string) => request<Project>(`/projects/${projectId}`),

  getProjectGraph: (projectId: string) =>
    request<IRGraph>(`/projects/${projectId}/graph`),

  /** 读取项目内某文件的一段源码；line 为 1 起始的行号 */
  getSourceExcerpt: (projectId: string, file: string, line?: number, radius = 20) => {
    const params = new URLSearchParams({ file });
    if (line !== undefined) params.set("line", String(line));
    params.set("radius", String(radius));
    return request<SourceExcerpt>(`/projects/${projectId}/source?${params.toString()}`);
  },

  startAnalysis: (projectId: string, payload?: AnalysisStart) =>
    request<AnalysisTask>(`/projects/${projectId}/analyses`, {
      method: "POST",
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    }),

  getTask: (taskId: string) => request<AnalysisTask>(`/tasks/${taskId}`),

  listAnalyses: (projectId: string) =>
    request<AnalysisTaskList>(`/projects/${projectId}/analyses`),

  startRuntimeCollect: (projectId: string, payload: RuntimeStart) =>
    request<RuntimeTask>(`/projects/${projectId}/runtime-traces`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listRuntimeTasks: (projectId: string) =>
    request<RuntimeTaskList>(`/projects/${projectId}/runtime-traces`),

  getRuntimeTask: (taskId: string) => request<RuntimeTask>(`/runtime-tasks/${taskId}`),

  getRuntimeTrace: (taskId: string) =>
    request<RuntimeTrace>(`/runtime-tasks/${taskId}/trace`),

  listPlugins: (projectId: string) =>
    request<PluginList>(`/projects/${projectId}/plugins`),

  startRequirementLocate: (projectId: string, payload: RequirementLocateStart) =>
    request<RequirementTask>(`/projects/${projectId}/requirements`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listRequirementTasks: (projectId: string) =>
    request<RequirementTaskList>(`/projects/${projectId}/requirements`),

  getRequirementTask: (taskId: string) => request<RequirementTask>(`/requirements/${taskId}`),

  getRequirementMap: (taskId: string) =>
    request<RequirementMap>(`/requirements/${taskId}/map`),
};
