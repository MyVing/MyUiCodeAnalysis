import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PropsPanel } from "@/components/PropsPanel";
import type { IRNode } from "@uicodeanalysis/contracts";

function makeNode(attrs: Record<string, unknown>): IRNode {
  return {
    id: "src/UserList.tsx#UserList",
    kind: "component",
    name: "UserList",
    file: "src/UserList.tsx",
    loc: { line: 5, col: 1 },
    attrs,
    evidence: [{ source: "static", confidence: 1, file: "src/UserList.tsx" }],
  };
}

const propsAttrs = {
  props: {
    typeName: "UserListProps",
    typeFile: "src/types/props.ts",
    generic: false,
    fields: [
      {
        name: "title",
        type: "string",
        optional: false,
        defaultValue: null,
        description: "列表标题",
        file: "src/types/props.ts",
        loc: { line: 9, col: 3 },
      },
      {
        name: "showFilter",
        type: "boolean",
        optional: true,
        defaultValue: "true",
        description: null,
        file: "src/types/props.ts",
        loc: { line: 11, col: 3 },
      },
    ],
  },
};

describe("PropsPanel", () => {
  it("没有 props 信息时整块不渲染，不占位", () => {
    const { container } = render(<PropsPanel node={makeNode({ form: "function" })} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("展示类型名与来源文件", () => {
    render(<PropsPanel node={makeNode(propsAttrs)} />);

    expect(screen.getByText("Props（2）")).toBeInTheDocument();
    expect(screen.getByText(/UserListProps/)).toBeInTheDocument();
    // 类型来源与每个字段的位置都会出现该文件，只要至少出现一次即可
    expect(screen.getAllByText(/src\/types\/props\.ts/).length).toBeGreaterThan(0);
  });

  it("字段展示名称、类型、可选性与说明", () => {
    render(<PropsPanel node={makeNode(propsAttrs)} />);

    expect(screen.getByText("title")).toBeInTheDocument();
    expect(screen.getByText("boolean")).toBeInTheDocument();
    expect(screen.getByText("列表标题")).toBeInTheDocument();
    // 有默认值的字段把默认值也显示出来
    expect(screen.getByText(/=\s*true/)).toBeInTheDocument();
  });

  it("可选字段用问号表达，而不是在类型里写 undefined", () => {
    render(<PropsPanel node={makeNode(propsAttrs)} />);

    expect(screen.getByText("?:")).toBeInTheDocument();
    expect(screen.queryByText(/\| undefined/)).not.toBeInTheDocument();
  });

  it("内联类型（无 typeName）显式说明", () => {
    const node = makeNode({
      props: {
        typeName: null,
        typeFile: null,
        generic: false,
        fields: [
          {
            name: "total",
            type: "number",
            optional: false,
            defaultValue: null,
            description: null,
            file: "src/TableCard.tsx",
            loc: { line: 17, col: 1 },
          },
        ],
      },
    });

    render(<PropsPanel node={node} />);
    expect(screen.getByText("内联类型，无类型名")).toBeInTheDocument();
  });

  it("形状不合法的 attrs.props 被忽略，不渲染半截数据", () => {
    const { container } = render(<PropsPanel node={makeNode({ props: { fields: "nope" } })} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("字段数组为空时说明组件不声明 props", () => {
    render(
      <PropsPanel
        node={makeNode({ props: { typeName: null, typeFile: null, generic: false, fields: [] } })}
      />,
    );

    expect(screen.getByText("该组件不声明 props")).toBeInTheDocument();
  });
});
