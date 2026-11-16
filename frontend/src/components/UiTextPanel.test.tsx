import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UiTextPanel } from "@/components/UiTextPanel";
import type { IRNode } from "@uicodeanalysis/contracts";

function componentNode(uiText: unknown): IRNode {
  return {
    id: "src/pages/UserList.tsx#UserList",
    kind: "component",
    name: "UserList",
    file: "src/pages/UserList.tsx",
    loc: { line: 32, col: 1 },
    attrs: { uiText },
    evidence: [{ source: "static", confidence: 1, file: "src/pages/UserList.tsx" }],
  };
}

const sample = {
  texts: [
    { text: "搜索用户", kind: "attr", via: "placeholder", loc: { line: 48, col: 29 } },
    { text: "姓名", kind: "object", via: "title", loc: { line: 56, col: 13 } },
  ],
  dynamic: [{ expr: "title", loc: { line: 47, col: 12 } }],
};

describe("UiTextPanel", () => {
  it("没有 uiText 时不渲染", () => {
    const { container } = render(<UiTextPanel node={componentNode(undefined)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("空清单也不渲染，避免每个节点都挂一块空面板", () => {
    const { container } = render(<UiTextPanel node={componentNode({ texts: [], dynamic: [] })} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("展示文案原文、来源分类与行号", () => {
    render(<UiTextPanel node={componentNode(sample)} />);

    expect(screen.getByText("可见文案")).toBeInTheDocument();
    expect(screen.getByText("搜索用户")).toBeInTheDocument();
    expect(screen.getByText("姓名")).toBeInTheDocument();
    expect(screen.getByText("属性")).toBeInTheDocument();
    expect(screen.getByText("配置")).toBeInTheDocument();
    expect(screen.getByText("placeholder · L48")).toBeInTheDocument();
  });

  it("动态文案单列，并说明不做跨文件求值", () => {
    render(<UiTextPanel node={componentNode(sample)} />);

    expect(screen.getByText(/来自变量/)).toBeInTheDocument();
    expect(screen.getByText("title")).toBeInTheDocument();
  });

  it("只有动态文案时也渲染（这不是没有文案）", () => {
    render(<UiTextPanel node={componentNode({ texts: [], dynamic: [{ expr: "name" }] })} />);

    expect(screen.getByText(/来自变量/)).toBeInTheDocument();
    expect(screen.getByText("没有字面量文案")).toBeInTheDocument();
  });

  it("截断时如实告知", () => {
    render(<UiTextPanel node={componentNode({ ...sample, truncated: 12 })} />);

    expect(screen.getByText(/另有 12 条超出上限/)).toBeInTheDocument();
  });

  it("不认识的来源分类原样显示，不吞契约外数据", () => {
    render(
      <UiTextPanel
        node={componentNode({
          texts: [{ text: "未来来源", kind: "future-kind", via: null, loc: null }],
          dynamic: [],
        })}
      />,
    );

    expect(screen.getByText("future-kind")).toBeInTheDocument();
  });
});
