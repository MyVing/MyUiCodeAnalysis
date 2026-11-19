import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import { useAnalysisTask, useChatSessions, useRuntimeTask } from "@/api/queries";
import type { AnalysisTask, ChatSessionSummaryList, RuntimeTask } from "@/api/types";

vi.mock("@/api/client", () => ({
  api: {
    getTask: vi.fn(),
    getRuntimeTask: vi.fn(),
    listChatSessions: vi.fn(),
  },
}));

function makeTask(status: AnalysisTask["status"]): AnalysisTask {
  return {
    id: "task_1",
    projectId: "prj_1",
    analysisId: "an_1",
    status,
    createdAt: new Date().toISOString(),
  };
}

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

describe("useAnalysisTask", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("polls while the task is active and stops once it succeeds", async () => {
    vi.mocked(api.getTask)
      .mockResolvedValueOnce(makeTask("queued"))
      .mockResolvedValueOnce(makeTask("running"))
      .mockResolvedValue(makeTask("succeeded"));

    const { result } = renderHook(() => useAnalysisTask("task_1"), { wrapper: makeWrapper() });

    // 首次拉取
    await waitFor(() => expect(result.current.data?.status).toBe("queued"));
    expect(api.getTask).toHaveBeenCalledTimes(1);

    // 轮询至 running
    await waitFor(
      () => expect(api.getTask).toHaveBeenCalledTimes(2),
      { timeout: 3000 },
    );
    expect(result.current.data?.status).toBe("running");

    // 轮询至终态
    await waitFor(
      () => expect(result.current.data?.status).toBe("succeeded"),
      { timeout: 3000 },
    );
    const callsAfterFinal = vi.mocked(api.getTask).mock.calls.length;

    // 终态后不再轮询
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(api.getTask).toHaveBeenCalledTimes(callsAfterFinal);
  }, 15000);

  it("does not fetch when taskId is null", () => {
    renderHook(() => useAnalysisTask(null), { wrapper: makeWrapper() });

    expect(api.getTask).not.toHaveBeenCalled();
  });
});

function makeRuntimeTask(status: RuntimeTask["status"]): RuntimeTask {
  return {
    id: "collect_1",
    projectId: "prj_1",
    traceId: "trace_1",
    status,
    createdAt: new Date().toISOString(),
  };
}

describe("useRuntimeTask", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("polls while collecting and stops at a terminal state", async () => {
    vi.mocked(api.getRuntimeTask)
      .mockResolvedValueOnce(makeRuntimeTask("queued"))
      .mockResolvedValue(makeRuntimeTask("succeeded"));

    const { result } = renderHook(() => useRuntimeTask("collect_1"), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.data?.status).toBe("queued"));
    expect(api.getRuntimeTask).toHaveBeenCalledTimes(1);

    await waitFor(
      () => expect(result.current.data?.status).toBe("succeeded"),
      { timeout: 3000 },
    );
    const callsAfterFinal = vi.mocked(api.getRuntimeTask).mock.calls.length;

    // 终态后不再轮询
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(api.getRuntimeTask).toHaveBeenCalledTimes(callsAfterFinal);
  }, 15000);

  it("does not fetch when taskId is null", () => {
    renderHook(() => useRuntimeTask(null), { wrapper: makeWrapper() });

    expect(api.getRuntimeTask).not.toHaveBeenCalled();
  });
});

describe("useChatSessions", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("fetches session summaries for the project", async () => {
    vi.mocked(api.listChatSessions).mockResolvedValue({
      items: [
        {
          id: "chat_1",
          projectId: "prj_1",
          title: "登录页加记住我",
          messageCount: 2,
          createdAt: "2026-11-19T08:00:00Z",
          updatedAt: "2026-11-19T08:00:00Z",
        },
      ],
    } satisfies ChatSessionSummaryList);

    const { result } = renderHook(() => useChatSessions("prj_1"), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.data?.items).toHaveLength(1));
    expect(api.listChatSessions).toHaveBeenCalledWith("prj_1");
  });

  it("does not fetch when projectId is empty", () => {
    renderHook(() => useChatSessions(""), { wrapper: makeWrapper() });

    expect(api.listChatSessions).not.toHaveBeenCalled();
  });
});
