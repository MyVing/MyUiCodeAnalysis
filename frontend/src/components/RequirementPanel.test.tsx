import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RequirementPanel } from "@/components/RequirementPanel";
import type { RequirementTask } from "@/api/types";
import type { ActionChain, RequirementMap } from "@uicodeanalysis/contracts";

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
  schemaVersion: "1.1",
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
      verifiedEvidence: [
        {
          nodeId: "src/pages/UserList.tsx#UserList",
          quote: "搜索用户",
          via: "uiText",
          file: "src/pages/UserList.tsx",
          loc: { line: 12, col: 7 },
          source: "static",
          confidence: 1,
        },
      ],
    },
  ],
  candidates: [
    {
      nodeId: "src/pages/UserList.tsx#UserList",
      name: "UserList",
      score: 2.1,
      file: "src/pages/UserList.tsx",
      routeId: "src/router.tsx#/users",
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

// 外链链路夹具：用户在报告里反馈的那一条（GitHub 链接，跳站外）
const CHAIN: ActionChain = {
  id: "chain_github",
  pageId: "src/core/Header.tsx#Header",
  element: {
    nodeId: "src/core/Header.tsx#Header/jsx:a",
    role: "element",
    name: "<a> GitHub",
    file: "src/core/Header.tsx",
    loc: { line: 20, col: 7 },
  },
  steps: [
    {
      nodeId: "src/core/Header.tsx#Header/jsx:a",
      role: "element",
      name: "<a> GitHub",
      file: "src/core/Header.tsx",
      loc: { line: 20, col: 7 },
    },
  ],
  path: { nodeIds: ["src/core/Header.tsx#Header/jsx:a"], edgeIds: [] },
  text: "GitHub",
  viewRole: "link",
  href: "https://github.com/naserrasoulii/feature-based-react",
  terminatedAt: "外部链接",
};

function renderPanel(onSelectNode = vi.fn()) {  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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
    // 候选列表里也有同名页面，匹配项断言必须限定在 match-list 内
    const matchList = screen.getByTestId("match-list");
    expect(within(matchList).getByText("UserList")).toBeInTheDocument();
    expect(within(matchList).getByText(/置信度 0\.60/)).toBeInTheDocument();
    expect(within(matchList).getByText(/依据：「搜索用户」/)).toBeInTheDocument();
    // dropped 必须可见：审核模型是否在幻想，只能靠这份记录
    expect(screen.getByTestId("dropped-list")).toHaveTextContent(/编的引文/);
    expect(screen.getByTestId("map-warnings")).toHaveTextContent(/llm_unavailable/);

    await user.click(screen.getByTestId("match-item"));
    // 点击匹配要把后端算好的子图一起交给工作台，用于精确聚焦
    expect(onSelectNode).toHaveBeenCalledWith("src/pages/UserList.tsx#UserList", ["a", "b"]);
  });

  it("候选列表展示命中信号及其得分", async () => {
    getRequirementTask.mockResolvedValue({ ...TASK, dryRun: true, matchCount: 0 });
    getRequirementMap.mockResolvedValue({
      ...MAP,
      dryRun: true,
      matches: [],
      dropped: undefined,
      warnings: [],
    });
    startLocate.mockResolvedValue({ ...TASK, dryRun: true });

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));
    await user.type(screen.getByTestId("requirement-input"), "用户列表增加状态筛选");
    await user.click(screen.getByTestId("submit-locate"));

    const list = await screen.findByTestId("candidate-list");
    expect(list).toHaveTextContent("UserList");
    expect(list).toHaveTextContent("src/pages/UserList.tsx");
    expect(list).toHaveTextContent("/users");
    expect(list).toHaveTextContent("总分 2.10");
    // 类别用中文展示，机器值不必出现在界面上
    expect(list).toHaveTextContent("界面文案");
    expect(list).toHaveTextContent("搜索用户");
    expect(list).toHaveTextContent("+0.90");
  });

  it("匹配项展示已核验证据的来源与坐标", async () => {
    getRequirementTask.mockResolvedValue(TASK);
    getRequirementMap.mockResolvedValue(MAP);
    startLocate.mockResolvedValue(TASK);

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));
    await user.type(screen.getByTestId("requirement-input"), "用户列表增加状态筛选");
    await user.click(screen.getByTestId("dry-run-toggle"));
    await user.click(screen.getByTestId("submit-locate"));

    const evidence = await screen.findByTestId("verified-evidence");
    expect(evidence).toHaveTextContent("静态分析");
    expect(evidence).toHaveTextContent("界面文案");
    expect(evidence).toHaveTextContent("src/pages/UserList.tsx:12:7");
    expect(evidence).toHaveTextContent("搜索用户");
    expect(screen.queryByTestId("no-verified-evidence")).not.toBeInTheDocument();
  });

  it("没有可定位证据时明确降级，不冒充完整证据", async () => {
    getRequirementTask.mockResolvedValue(TASK);
    getRequirementMap.mockResolvedValue({
      ...MAP,
      matches: [{ ...MAP.matches[0]!, verifiedEvidence: [] }],
    });
    startLocate.mockResolvedValue(TASK);

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));
    await user.type(screen.getByTestId("requirement-input"), "用户列表增加状态筛选");
    await user.click(screen.getByTestId("dry-run-toggle"));
    await user.click(screen.getByTestId("submit-locate"));

    expect(await screen.findByTestId("no-verified-evidence")).toHaveTextContent(
      "暂无可定位证据",
    );
    expect(screen.queryByTestId("verified-evidence")).not.toBeInTheDocument();
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

  it("候选标明来源：AI 与词法一致时可信度最高", async () => {
    getRequirementTask.mockResolvedValue(TASK);
    getRequirementMap.mockResolvedValue({
      ...MAP,
      candidates: [
        {
          ...MAP.candidates![0]!,
          signals: [
            { kind: "uiText", value: "搜索用户", score: 0.9 },
            // 后端只落名次不落分数（semantic_signals），所以 score 恒为 0
            { kind: "semanticRank", value: "rank=1", score: 0 },
          ],
        },
      ],
    });
    startLocate.mockResolvedValue(TASK);

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));
    await user.type(screen.getByTestId("requirement-input"), "用户列表增加状态筛选");
    await user.click(screen.getByTestId("dry-run-toggle"));
    await user.click(screen.getByTestId("submit-locate"));

    const list = await screen.findByTestId("candidate-list");
    expect(within(list).getByTestId("candidate-origin")).toHaveTextContent("AI + 词法一致");
    expect(list).toHaveTextContent("AI 名次");
  });

  it("命中链路时展示逐段路径、外链标注与断点，聚焦喂链路节点集", async () => {
    // 复现用户最初的需求：点击 github 页面跳转
    const pageNodeId = CHAIN.pageId;
    getRequirementTask.mockResolvedValue(TASK);
    getRequirementMap.mockResolvedValue({
      ...MAP,
      matches: [
        {
          ...MAP.matches[0]!,
          pageNodeId,
          chainId: CHAIN.id,
          targets: [CHAIN.element.nodeId],
          quote: "GitHub",
          subgraph: { ...MAP.matches[0]!.subgraph, nodeIds: ["header-page"] },
        },
      ],
      interactionChains: [CHAIN],
    });
    startLocate.mockResolvedValue(TASK);

    const user = userEvent.setup();
    const { onSelectNode } = renderPanel();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));
    await user.type(screen.getByTestId("requirement-input"), "点击github页面跳转");
    await user.click(screen.getByTestId("dry-run-toggle"));
    await user.click(screen.getByTestId("submit-locate"));

    const matchChain = await screen.findByTestId("match-chain");
    expect(matchChain).toHaveTextContent("GitHub");
    expect(matchChain).toHaveTextContent(CHAIN.href!);
    expect(matchChain).toHaveTextContent("中断：外部链接");

    const view = screen.getByTestId("chain-view");
    expect(view).toHaveTextContent("链接");
    expect(view).toHaveTextContent("外链");
    expect(screen.getByTestId("chain-terminated")).toHaveTextContent("链路在此中断：外部链接");

    const step = screen.getByTestId("chain-step");
    expect(step).toHaveTextContent("元素");
    expect(step).toHaveTextContent("src/core/Header.tsx:20:7");

    // 有链路就喂链路节点集：比整页子图小得多，聚焦才能直接落在这次动作上
    await user.click(screen.getByTestId("match-item"));
    expect(onSelectNode).toHaveBeenCalledWith(pageNodeId, CHAIN.path.nodeIds);

    await user.click(step);
    expect(onSelectNode).toHaveBeenCalledWith(CHAIN.element.nodeId, CHAIN.path.nodeIds);
  });

  it("没有链路时链路视图整段不出现，不渲染空壳", async () => {
    getRequirementTask.mockResolvedValue(TASK);
    getRequirementMap.mockResolvedValue(MAP);
    startLocate.mockResolvedValue(TASK);

    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /需求定位/ }));
    await user.type(screen.getByTestId("requirement-input"), "用户列表增加状态筛选");
    await user.click(screen.getByTestId("dry-run-toggle"));
    await user.click(screen.getByTestId("submit-locate"));

    await screen.findByTestId("match-item");
    expect(screen.queryByTestId("chain-list")).not.toBeInTheDocument();
    expect(screen.queryByTestId("match-chain")).not.toBeInTheDocument();
  });
});
