import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiPanel } from "@/components/ApiPanel";
import type { IRNode } from "@uicodeanalysis/contracts";

function makeNode(kind: IRNode["kind"], attrs: Record<string, unknown>): IRNode {
  return {
    id: "api:GET:/api/users",
    kind,
    name: "GET /api/users",
    file: "src/pages/UserList.tsx",
    loc: { line: 21, col: 10 },
    attrs,
    evidence: [{ source: "static", confidence: 0.9, file: "src/pages/UserList.tsx" }],
  };
}

describe("ApiPanel", () => {
  it("组件节点不渲染，面板只服务 api / fn", () => {
    const { container } = render(
      <ApiPanel node={{ ...makeNode("component", {}), id: "a#A", name: "A" }} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("api 节点展示方法、URL 与首次发现位置", () => {
    render(<ApiPanel node={makeNode("api", { method: "GET", url: "/api/users" })} />);

    expect(screen.getByText("接口")).toBeInTheDocument();
    expect(screen.getByText("GET")).toBeInTheDocument();
    expect(screen.getByText("/api/users")).toBeInTheDocument();
    // 接口没有定义处，位置是「首次发现于某调用点」，必须说清楚
    expect(screen.getByText(/首次发现于/)).toBeInTheDocument();
    expect(screen.getByText(/src\/pages\/UserList\.tsx:21:10/)).toBeInTheDocument();
  });

  it("fn 节点展示它在请求链路里的角色，且不说成首次发现处", () => {
    render(<ApiPanel node={makeNode("fn", { role: "请求链路上的函数" })} />);

    expect(screen.getByText("请求链路")).toBeInTheDocument();
    expect(screen.getByText("请求链路上的函数")).toBeInTheDocument();
    expect(screen.queryByText(/首次发现于/)).not.toBeInTheDocument();
  });

  it("attrs 形状异常时只显示有的部分，不整块消失", () => {
    render(<ApiPanel node={makeNode("api", { method: 42 })} />);

    // method 不是字符串 → 不显示「方法」行
    expect(screen.queryByText("方法")).not.toBeInTheDocument();
    expect(screen.getByText("无附加信息")).toBeInTheDocument();
  });
});
