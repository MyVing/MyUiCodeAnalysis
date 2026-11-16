import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WrapperPanel } from "@/components/WrapperPanel";
import type { IRNode } from "@uicodeanalysis/contracts";

function componentNode(wrapper: unknown): IRNode {
  return {
    id: "src/components/ProTable.tsx#ProTable",
    kind: "component",
    name: "ProTable",
    file: "src/components/ProTable.tsx",
    loc: { line: 8, col: 1 },
    attrs: { props: { typeName: null, typeFile: null, generic: false, fields: [] }, wrapper },
    evidence: [{ source: "static", confidence: 1, file: "src/components/ProTable.tsx" }],
  };
}

const resolvedWrapper = {
  target: "antd#Table",
  display: "Table",
  package: "antd",
  props: { request: "dataSource", onRowClick: "onRow" },
  resolved: true,
  description: "企业内二次封装",
};

describe("WrapperPanel", () => {
  it("没有 wrapper 注解时不渲染", () => {
    const { container } = render(<WrapperPanel node={componentNode(undefined)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("展示被封装的库组件与 props 映射", () => {
    render(<WrapperPanel node={componentNode(resolvedWrapper)} />);

    expect(screen.getByText("二次封装")).toBeInTheDocument();
    expect(screen.getByText("antd")).toBeInTheDocument();
    expect(screen.getByText("Table")).toBeInTheDocument();
    expect(screen.getByText("antd#Table")).toBeInTheDocument();
    expect(screen.getByText("request")).toBeInTheDocument();
    expect(screen.getByText("→ dataSource")).toBeInTheDocument();
    expect(screen.getByText("企业内二次封装")).toBeInTheDocument();
  });

  it("渲染边没连上时明确告警，而不是当成成功", () => {
    render(<WrapperPanel node={componentNode({ ...resolvedWrapper, resolved: false })} />);

    expect(screen.getByText(/渲染边没有连到这个组件/)).toBeInTheDocument();
  });

  it("resolved 为 true 时不出告警文案", () => {
    render(<WrapperPanel node={componentNode(resolvedWrapper)} />);

    expect(screen.queryByText(/渲染边没有连到这个组件/)).not.toBeInTheDocument();
  });

  it("target 缺失时整块不渲染（无法确认封装了什么）", () => {
    const { container } = render(
      <WrapperPanel node={componentNode({ display: "Table", package: "antd" })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
