import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useLocation, MemoryRouter, Route, Routes } from "react-router-dom";

import { ApiError } from "@/api/client";
import ChatPage from "@/pages/ChatPage";
import type { RequirementChatResult } from "@/api/types";

const chatRequirement = vi.fn();

// vi.mock 会被提升到文件顶部：类定义必须放进 factory 内，
// 测试里通过 import { ApiError } 拿到的就是 factory 里的实现
vi.mock("@/api/client", () => ({
  ApiError: class ApiError extends Error {
    readonly status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  api: {
    chatRequirement: (...args: unknown[]) => chatRequirement(...args),
  },
}));

/** 工作台探针：展示路由 state，用来断言草稿真的被带回来了 */
function WorkbenchProbe() {
  return (
    <div data-testid="workbench-probe">
      {(useLocation().state as { draftRequirement?: string } | null)?.draftRequirement ??
        "no-draft"}
    </div>
  );
}

function renderChat() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/projects/prj_1/chat"]}>
        <Routes>
          <Route path="/projects/:projectId/chat" element={<ChatPage />} />
          <Route path="/projects/:projectId" element={<WorkbenchProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function sendMessage(text: string) {
  const input = screen.getByTestId("chat-input");
  await userEvent.type(input, text);
  await userEvent.click(screen.getByTestId("chat-send"));
}

describe("ChatPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    chatRequirement.mockReset();
  });

  it("发送消息后展示用户与 AI 消息，历史全量回传", async () => {
    chatRequirement.mockResolvedValueOnce({
      reply: "筛选什么字段？",
      draftRequirement: null,
    } satisfies RequirementChatResult);
    renderChat();

    await sendMessage("我想加个筛选");

    await waitFor(() => {
      expect(screen.getAllByTestId("chat-message")).toHaveLength(2);
    });
    expect(chatRequirement).toHaveBeenCalledWith("prj_1", {
      messages: [{ role: "user", content: "我想加个筛选" }],
    });
    expect(screen.queryByTestId("chat-pending")).not.toBeInTheDocument();
  });

  it("出现草稿 banner，点击后草稿经路由 state 带回工作台", async () => {
    chatRequirement.mockResolvedValueOnce({
      reply: "草稿已生成",
      draftRequirement: "在用户列表页增加按状态筛选",
    } satisfies RequirementChatResult);
    renderChat();

    await sendMessage("用户列表页加状态筛选");
    await waitFor(() => {
      expect(screen.getByTestId("chat-draft-banner")).toBeInTheDocument();
    });
    expect(screen.getByTestId("chat-draft-banner")).toHaveTextContent(
      "在用户列表页增加按状态筛选",
    );

    await userEvent.click(screen.getByTestId("chat-fill-draft"));
    expect(screen.getByTestId("workbench-probe")).toHaveTextContent(
      "在用户列表页增加按状态筛选",
    );
  });

  it("模型未配置（503）时给出可操作的提示", async () => {
    chatRequirement.mockRejectedValueOnce(
      new ApiError(503, "模型未启用") as unknown as Error,
    );
    renderChat();

    await sendMessage("加个筛选");
    await waitFor(() => {
      expect(screen.getByTestId("chat-error")).toHaveTextContent(/llm_enabled/);
    });
    // 错误不应吞掉用户消息：可重试
    expect(screen.getByTestId("chat-retry")).toBeInTheDocument();
  });

  it("查文档消息渲染规格文档折叠块，下载按钮触发 .md 下载", async () => {
    const markdown = "## 业务操作步骤\n\n1. 选择付款账户";
    // jsdom 未实现 createObjectURL/revokeObjectURL，直接以 vi.fn 覆盖
    const createSpy = vi.fn(() => "blob:mock");
    const revokeSpy = vi.fn();
    const originalCreate = URL.createObjectURL;
    const originalRevoke = URL.revokeObjectURL;
    URL.createObjectURL = createSpy as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = revokeSpy as unknown as typeof URL.revokeObjectURL;
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click");

    chatRequirement.mockResolvedValueOnce({
      reply: "已定位到页面《转账》",
      draftRequirement: null,
      docMarkdown: markdown,
      docPage: "转账",
    } satisfies RequirementChatResult);
    renderChat();

    await sendMessage("转账的前端交互视图");
    const docBlock = await screen.findByTestId("chat-doc-block");
    expect(docBlock).toHaveTextContent("规格文档 · 转账");
    expect(screen.getByTestId("chat-doc-block")).toHaveTextContent(
      "业务操作步骤",
    );

    await userEvent.click(screen.getByTestId("chat-doc-download"));
    expect(clickSpy).toHaveBeenCalled();
    expect(createSpy).toHaveBeenCalledWith(expect.any(Blob));
    expect(revokeSpy).toHaveBeenCalledWith("blob:mock");

    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
  });
});
