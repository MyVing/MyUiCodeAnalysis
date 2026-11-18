import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ExplorePanel } from "@/components/ExplorePanel";
import type {
  ExploreArtifactData as ExploreArtifact,
  ExploreTask,
} from "@/api/types";

const startExplore = vi.fn();
const getExploreTask = vi.fn();
const getExploreArtifact = vi.fn();

vi.mock("@/api/client", () => ({
  api: {
    startExplore: (...args: unknown[]) => startExplore(...args),
    getExploreTask: (taskId: string) => getExploreTask(taskId),
    getExploreArtifact: (taskId: string) => getExploreArtifact(taskId),
    exploreScreenshotUrl: (taskId: string, fileName: string) =>
      `/api/v1/explore-tasks/${taskId}/screenshots/${fileName}`,
    exploreScriptUrl: (taskId: string) => `/api/v1/explore-tasks/${taskId}/script`,
  },
}));

const TASK: ExploreTask = {
  id: "exp_1",
  projectId: "prj_1",
  goal: "验证搜索交互",
  status: "succeeded",
  command: "npm run dev",
  dryRun: true,
  createdAt: "2026-11-18T00:00:00Z",
  elementCount: 2,
  stepCount: 2,
  successCount: null,
  failedCount: null,
  screenshotCount: 0,
};

const ARTIFACT: ExploreArtifact = {
  schemaVersion: "1.0",
  taskId: "exp_1",
  projectId: "prj_1",
  goal: "验证搜索交互",
  pageUrl: "http://localhost:5199/",
  title: "订单平台",
  dryRun: true,
  discovery: [
    {
      index: 0,
      tag: "input",
      role: "textbox",
      name: "搜索关键词",
      type: "text",
      visible: true,
      selector: { strategy: "role", value: "textbox", name: "搜索关键词" },
    },
    {
      index: 1,
      tag: "button",
      role: "button",
      name: "查询",
      visible: true,
      selector: { strategy: "role", value: "button", name: "查询" },
    },
  ],
  plan: {
    goal: "验证搜索交互",
    summary: "填写搜索框后点击查询",
    maxSteps: 12,
    steps: [
      {
        index: 0,
        action: "fill",
        targetIndex: 0,
        value: "订单",
        reasoning: "先填搜索框",
      },
      { index: 1, action: "click", targetIndex: 1, reasoning: "触发查询" },
    ],
    scenario: { title: "搜索订单", description: "关键词搜索订单列表" },
    testCases: [
      {
        title: "关键词搜索订单",
        priority: "p1",
        steps: ["在搜索框输入订单", "点击查询"],
        expected: ["页面出现查询结果"],
      },
    ],
  },
  results: [],
  scenario: { title: "搜索订单", description: "关键词搜索订单列表" },
  testCases: [
    {
      title: "关键词搜索订单",
      priority: "p1",
      steps: ["在搜索框输入订单", "点击查询"],
      expected: ["页面出现查询结果"],
    },
  ],
  script: {
    fileName: "explore-plan.spec.ts",
    content: "import { test, expect } from '@playwright/test';\n",
    testCount: 1,
    stepCount: 2,
  },
  screenshots: [],
  warnings: [
    { code: "dry_run", severity: "info", message: "dry-run：仅生成计划" },
    { code: "llm_not_configured", severity: "info", message: "未配置模型" },
  ],
};

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ExplorePanel projectId="prj_1" />
    </QueryClientProvider>,
  );
}

describe("ExplorePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("未展开时不渲染表单", () => {
    renderPanel();
    expect(screen.queryByTestId("explore-goal-input")).not.toBeInTheDocument();
  });

  it("点击标题后出现目标输入与 dry-run 默认开启", async () => {
    renderPanel();
    await userEvent.click(screen.getByTestId("explore-panel-toggle"));
    expect(screen.getByTestId("explore-goal-input")).toBeInTheDocument();
    const toggle = screen.getByTestId(
      "explore-dry-run-toggle",
    ) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
  });

  it("提交 dry-run 请求并展示计划、场景与脚本下载", async () => {
    startExplore.mockResolvedValueOnce(TASK);
    getExploreTask.mockResolvedValueOnce(TASK);
    getExploreArtifact.mockResolvedValueOnce(ARTIFACT);
    renderPanel();
    await userEvent.click(screen.getByTestId("explore-panel-toggle"));

    await userEvent.type(screen.getByTestId("explore-goal-input"), "验证搜索交互");
    await userEvent.click(screen.getByTestId("explore-submit"));

    await waitFor(() => expect(startExplore).toHaveBeenCalledTimes(1));
    expect(startExplore).toHaveBeenCalledWith("prj_1", {
      goal: "验证搜索交互",
      dryRun: true,
      command: "npm run dev",
    });

    await waitFor(() =>
      expect(screen.getByTestId("explore-scenario-title")).toHaveTextContent("搜索订单"),
    );
    expect(screen.getAllByTestId("explore-plan-step")).toHaveLength(2);
    expect(screen.getAllByTestId("explore-test-case")).toHaveLength(1);
    expect(screen.getAllByTestId("explore-element")).toHaveLength(2);
    // dry-run：无执行轨迹
    expect(screen.queryByTestId("explore-step-result")).not.toBeInTheDocument();
    // 脚本下载链接
    expect(screen.getByTestId("explore-script-download")).toHaveAttribute(
      "href",
      "/api/v1/explore-tasks/exp_1/script",
    );
    // dry-run 告警可见
    expect(screen.getByText(/dry-run：仅生成计划/)).toBeInTheDocument();
  });

  it("执行失败时展示失败信息", async () => {
    const failedTask: ExploreTask = {
      ...TASK,
      status: "failed",
      errorCode: "browser_unavailable",
      errorMessage: "未找到可用的浏览器",
    };
    startExplore.mockResolvedValueOnce(failedTask);
    getExploreTask.mockResolvedValueOnce(failedTask);
    renderPanel();
    await userEvent.click(screen.getByTestId("explore-panel-toggle"));
    await userEvent.type(screen.getByTestId("explore-goal-input"), "g");
    await userEvent.click(screen.getByTestId("explore-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("explore-failed")).toHaveTextContent(/browser_unavailable/),
    );
    expect(screen.queryByTestId("explore-result")).not.toBeInTheDocument();
  });
});
