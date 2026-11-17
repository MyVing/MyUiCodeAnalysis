import type { IRGraph, RequirementMap, RuntimeTrace } from "@uicodeanalysis/contracts";
import type {
  AnalysisStart,
  AnalysisTask,
  AnalysisTaskList,
  HealthStatus,
  PluginList,
  Project,
  ProjectCreate,
  RequirementLocateStart,
  RequirementTask,
  RequirementTaskList,
  RuntimeStart,
  RuntimeTask,
  RuntimeTaskList,
} from "@/api/types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
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
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // 非 JSON 错误体，保留默认提示
    }
    throw new ApiError(response.status, detail);
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

  getProject: (projectId: string) => request<Project>(`/projects/${projectId}`),

  getProjectGraph: (projectId: string) =>
    request<IRGraph>(`/projects/${projectId}/graph`),

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
