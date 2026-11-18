import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ViewPanel } from "@/components/ViewPanel";
import type { IRNode } from "@uicodeanalysis/contracts";

function elementNode(attrs: Record<string, unknown>, kind: IRNode["kind"] = "element"): IRNode {
  return {
    id: "src/pages/UserList.tsx#UserList/jsx:button/handleExport",
    kind,
    name: "<button> 导出",
    file: "src/pages/UserList.tsx",
    loc: { line: 40, col: 8 },
    attrs,
    evidence: [{ source: "static", confidence: 1, file: "src/pages/UserList.tsx" }],
  };
}

const button = {
  tag: "button",
  target: "host",
  viewRole: "control",
  viewText: "导出",
};

describe("ViewPanel", () => {
  it("只服务带 viewRole 的 element 节点", () => {
    const { container } = render(<ViewPanel node={elementNode({ tag: "button" })} />);
    expect(container).toBeEmptyDOMElement();

    // 只有事件、没有视图信息：EventPanel 的范围，本面板不重复
    const eventsOnly = render(<ViewPanel node={elementNode({ tag: "button", events: [] })} />);
    expect(eventsOnly.container).toBeEmptyDOMElement();

    const component = render(<ViewPanel node={elementNode(button, "component")} />);
    expect(component.container).toBeEmptyDOMElement();
  });

  it("控件的标签、角色与文案都显示", () => {
    render(<ViewPanel node={elementNode(button)} />);

    expect(screen.getByTestId("view-panel")).toBeInTheDocument();
    expect(screen.getByText("<button>")).toBeInTheDocument();
    expect(screen.getByText(/HTML 标签 · 控件/)).toBeInTheDocument();
    expect(screen.getByText("「导出」")).toBeInTheDocument();
  });

  it("组件实例标注为「组件」，与 HTML 标签区分开", () => {
    render(
      <ViewPanel
        node={elementNode({ ...button, tag: "Button", target: "component", viewRole: "control" })}
      />,
    );

    expect(screen.getByText(/组件 · 控件/)).toBeInTheDocument();
  });

  it("文案是动态的：说「由变量决定」，不留空也不假装有文案", () => {
    render(
      <ViewPanel
        node={elementNode({
          tag: "h1",
          target: "host",
          viewRole: "text",
          viewTextDynamic: true,
        })}
      />,
    );

    expect(screen.getByText(/文案由变量决定/)).toBeInTheDocument();
    expect(screen.queryByText(/「/)).not.toBeInTheDocument();
  });

  it("表格列按顺序列出，列名与数据键都显示", () => {
    render(
      <ViewPanel
        node={elementNode({
          tag: "ProTable",
          target: "component",
          viewRole: "container",
          columns: [
            { title: "姓名", dataIndex: "name" },
            { title: "状态", dataIndex: null },
          ],
        })}
      />,
    );

    expect(screen.getByText(/表格列 · 2/)).toBeInTheDocument();
    expect(screen.getByText("姓名")).toBeInTheDocument();
    expect(screen.getByText("name")).toBeInTheDocument();
    expect(screen.getByText("状态")).toBeInTheDocument();
  });

  it("未知角色或畸形 columns 不崩，按信息缺失处理", () => {
    render(<ViewPanel node={elementNode({ tag: "div", viewRole: "mystery" })} />);
    expect(screen.queryByTestId("view-panel")).not.toBeInTheDocument();

    render(
      <ViewPanel
        node={elementNode({ tag: "Table", viewRole: "container", columns: [{ title: 42 }] })}
      />,
    );
    expect(screen.queryByText(/表格列/)).not.toBeInTheDocument();
  });
});
