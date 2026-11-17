import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RequirementPanel } from "@/components/RequirementPanel";
import type { RequirementTask } from "@/api/types";
import type { RequirementMap } from "@uicodeanalysis/contracts";

const startLocate = vi.fn();
const getRequirementTask = vi.fn();
const getRequirementMap = vi.fn();

vi.mock("@/api/client", () => ({
  api: {
    startRequirementLocate: (...args: unknown[]) => startLocate(...args),
    getRequirementTask: (taskId: string) => getRequirementTask(taskId),
    getRequirementMap: (taskId: string) => getRequirementMap(taskId),
  },
}));

const TASK: RequirementTask = {
  id: "rtask_1",
  projectId: "prj_1",
  requirementId: "req_1",
  status: "succeeded",
  dryRun: false,
  requirement: "用户列表增加状态筛选",
  analysisId: "an_1",
  createdAt: "2026-11-17T00:00:00Z",
  matchCount: 1,
  candidateCount: 2,
  warningCount: 0,
};

const MAP: RequirementMap = {
  schemaVersion: "1.0",
  requirementId: "req_1",
  projectId: "prj_1",
  analysisId: "an_1",
  requirement: "用户列表增加状态筛选",
  createdAt: "2026-11-17T00:00:01Z",
  dryRun: false,
  features: [{ id: "f1", text: "列表筛选", keywords: ["筛选"] }],
  matches: [
    {
      featureId: "f1",
      pageNodeId: "src/pages/UserList.tsx#UserList",
      confidence: 0.6,
      modelConfidence: 0.9,
      quote: "搜索用户",
      reason: "页面文案一致",
      subgraph: {
        nodeIds: ["a", "b"],
        edgeIds: [],
        counts: {
          component: 1,
          hook: 0,
          state: 0,
          api: 1,
          event: 0,
          fn: 0,
          route: 1,
          element: 0,
        },
      },
      source: "ai",
    },
  ],
  candidates: [
    {
      nodeId: "src/pages/UserList.tsx#UserList",
      name: "UserList",
      score: 2.1,
      signals: [{ kind: "uiText", value: "搜索用户", score: 0.9 }],
    },
  ],
  dropped: [
    {
      reason: "quote_not_found",
      featureId: "f1",
      nodeId: "src/pages/Fake.tsx#Fake",
      quote: "编的引文",
    },
  ],
  warnings: [{ code: "llm_unavailable", severity: "error", message: "模型降级提示" }],
};

function renderPanel(onSelectNode = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <RequirementPanel projectId="prj_1" analysisId="an_1" onSelectNode={onSelectNode} />
    </QueryClientProvider>,
  );
  return { ...utils, onSelectNode };
}

describe("RequirementPanel", () => {
  beforeEach(() => {
    startLocate.mockReset();
    getRequirementTask.mockReset();
    getRequirementMap.mockReset();
  });

  it("输入为空时不能发起定位", async () => {
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));

    const submit = screen.getByTestId("submit-locate");
    expect(submit).toBeDisabled();
  });

  it("dry-run 下发起定位并展示确定性召回结果（无 matches）", async () => {
    getRequirementTask.mockResolvedValue({ ...TASK, dryRun: true, matchCount: 0 });
    getRequirementMap.mockResolvedValue({
      ...MAP,
      dryRun: true,
      matches: [],
      dropped: undefined,
      unmatched: [{ featureId: "f1", reason: "候选里没有对应页面" }],
    });
    startLocate.mockResolvedValue({ ...TASK, dryRun: true });

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));
    await user.type(screen.getByTestId("requirement-input"), "用户列表增加状态筛选");
    await user.click(screen.getByTestId("submit-locate"));

    await waitFor(() => {
      expect(startLocate).toHaveBeenCalledWith("prj_1", {
        requirement: "用户列表增加状态筛选",
        dryRun: true,
        analysisId: "an_1",
      });
    });

    await waitFor(() => {
      expect(screen.getByTestId("requirement-result")).toBeInTheDocument();
    });
    expect(screen.getByTestId("map-badge")).toHaveTextContent("确定性召回");
    expect(screen.getByText(/未定位/)).toBeInTheDocument();
    expect(screen.queryByTestId("match-item")).not.toBeInTheDocument();
  });

  it("AI 判定结果展示匹配、置信度封顶依据与落选记录", async () => {
    getRequirementTask.mockResolvedValue(TASK);
    getRequirementMap.mockResolvedValue(MAP);
    startLocate.mockResolvedValue(TASK);

    const user = userEvent.setup();
    const { onSelectNode } = renderPanel();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));
    await user.type(screen.getByTestId("requirement-input"), "用户列表增加状态筛选");
    await user.click(screen.getByTestId("dry-run-toggle"));
    await user.click(screen.getByTestId("submit-locate"));

    await waitFor(() => {
      expect(screen.getAllByTestId("match-item")).toHaveLength(1);
    });
    expect(screen.getByText("UserList")).toBeInTheDocument();
    expect(screen.getByText(/置信度 0\.60/)).toBeInTheDocument();
    expect(screen.getByText(/搜索用户/)).toBeInTheDocument();
    // dropped 必须可见：审核模型是否在幻想，只能靠这份记录
    expect(screen.getByTestId("dropped-list")).toHaveTextContent(/编的引文/);
    expect(screen.getByTestId("map-warnings")).toHaveTextContent(/llm_unavailable/);

    await user.click(screen.getByTestId("match-item"));
    expect(onSelectNode).toHaveBeenCalledWith("src/pages/UserList.tsx#UserList");
  });

  it("前置校验被拒（未配置模型）时透出后端错误信息", async () => {
    startLocate.mockRejectedValue(
      new Error("未配置模型，当前只能使用 dryRun（只跑确定性召回）。"),
    );

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));
    await user.type(screen.getByTestId("requirement-input"), "需求");
    await user.click(screen.getByTestId("dry-run-toggle"));
    await user.click(screen.getByTestId("submit-locate"));

    await waitFor(() => {
      expect(screen.getByTestId("locate-error")).toHaveTextContent(/未配置模型/);
    });
  });
});
