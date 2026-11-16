import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HookPanel } from "@/components/HookPanel";
import type { IRNode } from "@uicodeanalysis/contracts";

function makeNode(kind: IRNode["kind"], attrs: Record<string, unknown>): IRNode {
  return {
    id: "src/pages/UserList.tsx#UserList/handleSelect",
    kind,
    name: "handleSelect",
    file: "src/pages/UserList.tsx",
    loc: { line: 38, col: 24 },
    attrs,
    evidence: [{ source: "static", confidence: 1, file: "src/pages/UserList.tsx" }],
  };
}

describe("HookPanel", () => {
  it("组件节点不渲染，面板只服务 hook / state", () => {
    const { container } = render(
      <HookPanel node={{ ...makeNode("component", {}), id: "a#A", name: "A" }} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("hook 节点展示 hook 来源、绑定变量与依赖", () => {
    render(
      <HookPanel
        node={makeNode("hook", {
          hookName: "useCallback",
          custom: false,
          binding: "handleSelect",
          deps: ["onSelect"],
        })}
      />,
    );

    expect(screen.getByText("Hook")).toBeInTheDocument();
    expect(screen.getByText("useCallback")).toBeInTheDocument();
    expect(screen.getByText("[onSelect]")).toBeInTheDocument();
  });

  it("空依赖数组与缺省依赖要能区分开", () => {
    const { unmount } = render(
      <HookPanel node={makeNode("hook", { hookName: "useEffect", deps: [] })} />,
    );
    expect(screen.getByText("[] （仅挂载/卸载）")).toBeInTheDocument();
    unmount();

    // deps 为 null = 每次渲染都执行，不能显示成空数组
    render(<HookPanel node={makeNode("hook", { hookName: "useEffect", deps: null })} />);
    expect(screen.queryByText("[] （仅挂载/卸载）")).not.toBeInTheDocument();
  });

  it("state 节点展示 setter、类型与初始值", () => {
    render(
      <HookPanel
        node={makeNode("state", {
          hookName: "useState",
          setter: "setRows",
          typeText: "UserRowProps[]",
          initial: "[]",
        })}
      />,
    );

    expect(screen.getByText("State")).toBeInTheDocument();
    expect(screen.getByText("setRows")).toBeInTheDocument();
    expect(screen.getByText("UserRowProps[]")).toBeInTheDocument();
    // 初始值那一行是独立的 "[]"（类型文本里也含 []，所以不能用模糊匹配）
    expect(screen.getByText("初始值")).toBeInTheDocument();
    expect(screen.getAllByText("[]").length).toBeGreaterThan(0);
  });

  it("自定义 hook 明确标注不是 react 内置", () => {
    render(
      <HookPanel
        node={makeNode("hook", { hookName: "useProTableData", custom: true, deps: null })}
      />,
    );

    expect(screen.getByText("自定义 Hook（非 react 内置）")).toBeInTheDocument();
  });

  it("attrs 形状异常时只显示有的部分，不整块消失", () => {
    render(<HookPanel node={makeNode("hook", { deps: "nope" })} />);

    // hookName 缺失 → 「来源」行不出现；deps 不是数组 → 不显示依赖行
    expect(screen.queryByText("来源")).not.toBeInTheDocument();
    expect(screen.queryByText("依赖")).not.toBeInTheDocument();
    expect(screen.getByText("无附加信息")).toBeInTheDocument();
  });
});
