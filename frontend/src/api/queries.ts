import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";

export const useHealth = () =>
  useQuery({
    queryKey: ["health"],
    queryFn: api.getHealth,
    retry: false,
  });
export const useProjects = () =>
  useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
  });

export const useProject = (projectId: string) =>
  useQuery({
    queryKey: ["projects", projectId],
    queryFn: () => api.getProject(projectId),
    enabled: projectId.length > 0,
  });

export const useProjectGraph = (projectId: string) =>
  useQuery({
    queryKey: ["projects", projectId, "graph"],
    queryFn: () => api.getProjectGraph(projectId),
    enabled: projectId.length > 0,
  });

/** AI 需求助手会话历史列表：保存/删除后通过失效缓存刷新 */
export const useChatSessions = (projectId: string) =>
  useQuery({
    queryKey: ["chat-sessions", projectId],
    queryFn: () => api.listChatSessions(projectId),
    enabled: projectId.length > 0,
  });

/** 轮询分析任务状态：queued/running 时每秒刷新，进入终态后停止 */
export const useAnalysisTask = (taskId: string | null) =>
  useQuery({
    queryKey: ["tasks", taskId],
    queryFn: () => api.getTask(taskId as string),
    enabled: taskId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 1000 : false;
    },
  });

/** 轮询运行时采集任务：与分析任务同一节奏（托管 dev server + 浏览器，采集更慢） */
export const useRuntimeTask = (taskId: string | null) =>
  useQuery({
    queryKey: ["runtime-tasks", taskId],
    queryFn: () => api.getRuntimeTask(taskId as string),
    enabled: taskId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 1000 : false;
    },
  });

/** 轮询需求定位任务：模型判定比确定性召回慢，同样按秒轮询 */
export const useRequirementTask = (taskId: string | null) =>
  useQuery({
    queryKey: ["requirement-tasks", taskId],
    queryFn: () => api.getRequirementTask(taskId as string),
    enabled: taskId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 1000 : false;
    },
  });

/** 轮询 AI 探索任务：discovery + 计划 + 执行三段，节奏同上 */
export const useExploreTask = (taskId: string | null) =>
  useQuery({
    queryKey: ["explore-tasks", taskId],
    queryFn: () => api.getExploreTask(taskId as string),
    enabled: taskId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 1000 : false;
    },
  });

/** 探索产物（轨迹 / 计划 / 场景 / 脚本）：任务成功后才请求 */
export const useExploreArtifact = (taskId: string | null) =>
  useQuery({
    queryKey: ["explore-artifact", taskId],
    queryFn: () => api.getExploreArtifact(taskId as string),
    enabled: taskId !== null,
    staleTime: 60_000,
  });

/** 节点源码片段：只有拿到文件路径才请求，同一文件的不同行分开缓存 */
export const useSourceExcerpt = (projectId: string, file: string | null, line?: number) =>
  useQuery({
    queryKey: ["projects", projectId, "source", file, line ?? 1],
    queryFn: () => api.getSourceExcerpt(projectId, file as string, line),
    enabled: projectId.length > 0 && file !== null,
    staleTime: 60_000,
    retry: false,
  });
