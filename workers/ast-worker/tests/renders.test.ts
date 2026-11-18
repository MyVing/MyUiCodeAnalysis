/**
 * renders 边分析测试。
 *
 * 分两部分：
 *  - 真实样本仓（tests/fixtures/enterprise-react-app）：验证 README 里写明的期望图结构
 *  - 合成项目：验证真实样本没覆盖到的边界（自环、重复渲染、组件外 JSX、未导出局部组件）
 *
 * 合成项目刻意写进临时目录而不是 createSourceFile 注入，因为 resolver 依赖真实
 * 文件路径做归属判定；用内存节点会绕开它本该走的那条路径。
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import { discoverComponents } from "../src/components.js";
import { createFullProject } from "../src/project.js";
import { analyzeRenders } from "../src/renders.js";
import { createResolutionIndex } from "../src/resolver.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

interface AnalysisResult {
  edges: IREdge[];
  externalNodes: IRNode[];
  warnings: Warning[];
  componentIds: string[];
}

function analyze(rootPath: string): AnalysisResult {
  const { project } = createFullProject(rootPath);
  const resolution = createResolutionIndex(rootPath, project);
  const discovery = discoverComponents(rootPath, project);
  const renders = analyzeRenders({
    rootPath,
    project,
    components: discovery.components,
    declarations: discovery.declarations,
    definitionIndex: discovery.definitionIndex,
    resolution: resolution.index,
  });

  return {
    edges: renders.edges,
    externalNodes: renders.externalNodes,
    warnings: [...resolution.warnings, ...discovery.warnings, ...renders.warnings],
    componentIds: discovery.components.map((c) => c.id),
  };
}

/** 在临时目录里搭一个合成项目并分析。 */
const tempDirs: string[] = [];

function analyzeSynthetic(files: Record<string, string>): AnalysisResult {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-renders-"));
  tempDirs.push(dir);

  for (const [relative, content] of Object.entries(files)) {
    const absolute = path.join(dir, relative);
    mkdirSync(path.dirname(absolute), { recursive: true });
    writeFileSync(absolute, content, "utf8");
  }

  return analyze(dir);
}

afterAll(() => {
  for (const dir of tempDirs) {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** 取某条边，便于断言；不存在直接失败而不是静默返回 undefined。 */
function getEdge(result: AnalysisResult, from: string, to: string): IREdge {
  const edge = result.edges.find((e) => e.from === from && e.to === to);
  if (!edge) {
    throw new Error(
      `缺少边 ${from} -> ${to}；实际边：\n${result.edges.map((e) => `  ${e.from} -> ${e.to}`).join("\n")}`,
    );
  }
  return edge;
}

describe("renders 边（真实样本仓）", () => {
  let result: AnalysisResult;

  beforeAll(() => {
    result = analyze(FIXTURE_ROOT);
  });

  it("还原样本 README 声明的图结构", () => {
    // UserList -> TableCard -> RowItem
    getEdge(result, "src/pages/UserList.tsx#UserList", "src/components/TableCard.tsx#TableCard");
    getEdge(result, "src/components/TableCard.tsx#TableCard", "src/components/RowItem.tsx#RowItem");

    // UserList -> ProTable -> antd.Table
    getEdge(result, "src/pages/UserList.tsx#UserList", "src/components/ProTable.tsx#ProTable");
    getEdge(result, "src/components/ProTable.tsx#ProTable", "antd#Table");
  });

  it("同文件内的组件互相渲染也能识别（不因同文件而丢失）", () => {
    // TableCard 渲染同文件内定义的 CardFooter
    getEdge(
      result,
      "src/components/TableCard.tsx#TableCard",
      "src/components/TableCard.tsx#CardFooter",
    );
  });

  it("边数精确：不重复、不漏", () => {
    expect(result.edges).toHaveLength(5);
  });

  it("宿主元素（div / section / input）不产生边", () => {
    const toHostElement = result.edges.filter(
      (e) => !e.to.includes("#") || e.to.endsWith("#div") || e.to.endsWith("#input"),
    );
    expect(toHostElement).toEqual([]);
  });

  it("项目内组件边的置信度为 0.9，外部组件边为 0.6", () => {
    const internal = getEdge(
      result,
      "src/pages/UserList.tsx#UserList",
      "src/components/TableCard.tsx#TableCard",
    );
    expect(internal.evidence[0]?.confidence).toBe(0.9);
    expect(internal.evidence[0]?.source).toBe("static");

    const external = getEdge(result, "src/components/ProTable.tsx#ProTable", "antd#Table");
    expect(external.evidence[0]?.confidence).toBe(0.6);
  });

  it("边能回溯到标签名所在行列，而不是元素开头", () => {
    const edge = getEdge(
      result,
      "src/pages/UserList.tsx#UserList",
      "src/components/TableCard.tsx#TableCard",
    );
    const evidence = edge.evidence[0];
    expect(evidence?.file).toBe("src/pages/UserList.tsx");
    // 源码第 52 行是 `<TableCard rows={rows} onSelect={handleSelect} />`，标签名在第 10 列
    expect(evidence?.loc).toEqual({ line: 52, col: 10 });
  });

  it("外部组件以薄节点入图，显式标注不可见", () => {
    expect(result.externalNodes).toHaveLength(1);
    const node = result.externalNodes[0];
    expect(node?.id).toBe("antd#Table");
    expect(node?.kind).toBe("component");
    // 外部节点没有源码位置，file / loc 必须缺省而不是编造
    expect(node?.file).toBeUndefined();
    expect(node?.loc).toBeUndefined();
    expect(node?.attrs).toMatchObject({ external: true, packageName: "antd" });
  });

  it("没有 unresolved_jsx_target：样本里的每个组件标签都能追踪到", () => {
    const unresolved = result.warnings.filter((w) => w.code === "unresolved_jsx_target");
    expect(unresolved).toEqual([]);
  });
});

describe("renders 边的边界（合成项目）", () => {
  it("递归组件渲染自己时不产生自环", () => {
    const result = analyzeSynthetic({
      "src/Tree.tsx": `
        export function Tree() {
          return <div><Tree /></div>;
        }
      `,
    });

    expect(result.edges).toEqual([]);
  });

  it("同一组件渲染两次只产生一条边", () => {
    const result = analyzeSynthetic({
      "src/Leaf.tsx": `export function Leaf() { return <span />; }`,
      "src/Pair.tsx": `
        import { Leaf } from "./Leaf";
        export function Pair() {
          return <div><Leaf /><Leaf /></div>;
        }
      `,
    });

    expect(result.edges).toHaveLength(1);
    expect(result.edges[0]?.from).toBe("src/Pair.tsx#Pair");
    expect(result.edges[0]?.to).toBe("src/Leaf.tsx#Leaf");
  });

  it("箭头函数形式的组件也能作为边的起点", () => {
    const result = analyzeSynthetic({
      "src/Leaf.tsx": `export function Leaf() { return <span />; }`,
      "src/Wrapper.tsx": `
        import { Leaf } from "./Leaf";
        export const Wrapper = () => <section><Leaf /></section>;
      `,
    });

    expect(getEdge(result, "src/Wrapper.tsx#Wrapper", "src/Leaf.tsx#Leaf")).toBeDefined();
  });

  it("组件外（模块顶层常量）的 JSX 不归任何组件，不产边", () => {
    const result = analyzeSynthetic({
      "src/Leaf.tsx": `export function Leaf() { return <span />; }`,
      "src/constants.tsx": `
        import { Leaf } from "./Leaf";
        // 顶层常量里的 JSX 不属于任何组件
        export const prebuilt = <Leaf />;
        export function Real() { return <div />; }
      `,
    });

    expect(result.edges).toEqual([]);
  });

  it("渲染项目内未导出的局部组件时记录告警，不编造边", () => {
    const result = analyzeSynthetic({
      "src/Page.tsx": `
        function Internal() { return <span />; }
        export function Page() { return <div><Internal /></div>; }
      `,
    });

    // Internal 未导出，因此不在组件列表里；它确实被渲染了，属于分析能力缺口
    expect(result.componentIds).toEqual(["src/Page.tsx#Page"]);
    expect(result.edges).toEqual([]);

    const warning = result.warnings.find((w) => w.code === "unresolved_jsx_target");
    expect(warning?.severity).toBe("warning");
    expect(warning?.file).toBe("src/Page.tsx");
    expect(warning?.message).toContain("Internal");
  });

  it("已安装但与渲染无关的模块不会被误连成边", () => {
    const result = analyzeSynthetic({
      "src/Leaf.tsx": `export function Leaf() { return <span />; }`,
      "src/Holder.tsx": `
        import { Leaf } from "./Leaf";
        import type { LeafProps } from "./Leaf";
        export function Holder(): JSX.Element {
          return <Leaf />;
        }
        export type { LeafProps };
      `,
    });

    expect(result.edges).toHaveLength(1);
    expect(result.edges[0]?.to).toBe("src/Leaf.tsx#Leaf");
  });

  it("没有 JSX 的组件之间不产生 edge", () => {
    const result = analyzeSynthetic({
      "src/a.tsx": `export function A() { return <div />; }`,
      "src/b.tsx": `export function B() { return <span />; }`,
    });

    expect(result.componentIds.sort()).toEqual(["src/a.tsx#A", "src/b.tsx#B"]);
    expect(result.edges).toEqual([]);
  });

  it("slot 组件（const Comp = asChild ? Slot : \"button\"）按分支连边，不报未解析", () => {
    const result = analyzeSynthetic({
      "src/Slot.tsx": `
        import { Slot } from "@radix-ui/react-slot";
        import * as React from "react";
        export function Button({ asChild = false, ...props }: React.ComponentProps<"button"> & { asChild?: boolean }) {
          const Comp = asChild ? Slot : "button";
          return <Comp {...props} />;
        }
      `,
    });

    // 字符串分支 "button" 是宿主元素，不是组件；Slot 是外部组件，产薄节点
    expect(result.edges).toHaveLength(1);
    expect(result.edges[0]?.from).toBe("src/Slot.tsx#Button");
    expect(result.edges[0]?.to).toBe("@radix-ui/react-slot#Slot");
    // 分支级确定、整体取决于运行时 prop，置信度低于直接解析
    expect(result.edges[0]?.evidence[0]?.confidence).toBeLessThan(0.9);
    expect(result.warnings.some((w) => w.code === "unresolved_jsx_target")).toBe(false);
  });

  it("两个项目内组件作为分支时，两条边都产出", () => {
    const result = analyzeSynthetic({
      "src/Parts.tsx": `
        export function Primary() { return <div />; }
        export function Secondary() { return <span />; }
      `,
      "src/Maybe.tsx": `
        import { Primary, Secondary } from "./Parts";
        export function Maybe({ primary }: { primary: boolean }) {
          const Comp = primary ? Primary : Secondary;
          return <Comp />;
        }
      `,
    });

    const targets = result.edges.map((e) => e.to).sort();
    expect(targets).toEqual(["src/Parts.tsx#Primary", "src/Parts.tsx#Secondary"]);
    expect(result.warnings.some((w) => w.code === "unresolved_jsx_target")).toBe(false);
  });
});
