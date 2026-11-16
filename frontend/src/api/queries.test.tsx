import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import { useAnalysisTask } from "@/api/queries";
import type { AnalysisTask } from "@/api/types";

vi.mock("@/api/client", () => ({
  api: {
    getTask: vi.fn(),
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
