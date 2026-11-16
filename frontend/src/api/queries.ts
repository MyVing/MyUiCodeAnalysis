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
