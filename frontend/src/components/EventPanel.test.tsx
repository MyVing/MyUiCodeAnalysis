import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EventPanel } from "@/components/EventPanel";
import type { IRNode } from "@uicodeanalysis/contracts";

function elementNode(events: unknown): IRNode {
  return {
    id: "src/pages/UserList.tsx#UserList/jsx:TableCard/handleSelect",
    kind: "element",
    name: "<TableCard>",
    file: "src/pages/UserList.tsx",
    loc: { line: 52, col: 10 },
    attrs: { tag: "TableCard", target: "component", events },
    evidence: [{ source: "static", confidence: 1, file: "src/pages/UserList.tsx" }],
  };
}

const bound = {
  name: "onSelect",
  handler: "handleSelect",
  kind: "reference",
  handlerName: "handleSelect",
  targets: [{ id: "src/pages/UserList.tsx#UserList/handleSelect", kind: "hook" }],
  injected: false,
};

const injected = {
  name: "onClick",
  handler: "onSelect",
  kind: "reference",
  handlerName: "onSelect",
  targets: [],
  injected: true,
};

const unresolved = {
  name: "onClick",
  handler: "makeHandler(\"a\")",
  kind: "other",
  handlerName: null,
  targets: [],
  injected: false,
};

describe("EventPanel", () => {
  it("只服务 element 节点，其他类型不渲染", () => {
    const { container } = render(
      <EventPanel
        node={{ ...elementNode([bound]), kind: "component", id: "a#A", name: "A" }}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("已解析的事件写明落到哪个节点", () => {
    render(<EventPanel node={elementNode([bound])} />);

    expect(screen.getByText("事件（1）")).toBeInTheDocument();
    expect(screen.getByText("onSelect")).toBeInTheDocument();
    expect(screen.getByText("具名引用")).toBeInTheDocument();
    expect(screen.getByText(/hook：src\/pages\/UserList\.tsx#UserList\/handleSelect/)).toBeInTheDocument();
  });

  it("props 注入的回调显示「由父组件注入」，而不是「无法判定」", () => {
    render(<EventPanel node={elementNode([injected])} />);

    // 这条边界是分析结论，不是分析失败
    expect(screen.getByText(/由父组件注入/)).toBeInTheDocument();
    expect(screen.queryByText(/无法静态判定/)).not.toBeInTheDocument();
  });

  it("真正无法判定的情形才说「无法静态判定」", () => {
    render(<EventPanel node={elementNode([unresolved])} />);

    expect(screen.getByText(/无法静态判定/)).toBeInTheDocument();
    expect(screen.getByText("其他表达式")).toBeInTheDocument();
  });

  it("events 缺失或形状不对时整块不渲染", () => {
    const { container } = render(<EventPanel node={elementNode(undefined)} />);
    expect(container).toBeEmptyDOMElement();

    const malformed = render(<EventPanel node={elementNode([{ name: 42 }])} />);
    expect(malformed.container).toBeEmptyDOMElement();
  });
});
