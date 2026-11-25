/**
 * 组件发现测试：导出形态与「候选未解析」告警。
 *
 * 重点回归一个真实缺陷：嵌套 tsconfig 的 extends 基础配置缺失时，
 * 解析出的 compilerOptions 会丢掉 allowJs。此时分析器虽然把 `.jsx`
 * 加进了 Project，`getExportedDeclarations()` 却返回空 Map，
 * 具名导出组件全部静默消失，而且没有任何告警。
 *
 * 每个用例都在临时目录里搭最小项目，不污染 tests/fixtures。
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";
import { Project, ts } from "ts-morph";

import { discoverComponents } from "../src/components.js";
import { createFullProject } from "../src/project.js";

const created: string[] = [];

function makeProject(files: Record<string, string>): string {
  const root = mkdtempSync(path.join(tmpdir(), "ast-worker-components-"));
  created.push(root);
  for (const [relative, content] of Object.entries(files)) {
    const target = path.join(root, relative);
    mkdirSync(path.dirname(target), { recursive: true });
    writeFileSync(target, content, "utf8");
  }
  return root;
}

afterEach(() => {
  while (created.length > 0) {
    rmSync(created.pop() as string, { recursive: true, force: true });
  }
});

describe("组件发现的导出形态", () => {
  it("嵌套 tsconfig 缺少 allowJs 时，.jsx 具名导出仍能产出组件并告警", () => {
    const root = makeProject({
      // 模拟 react-native-website：只有子目录 tsconfig，且未显式开启 allowJs
      "website/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx", baseUrl: "." },
        include: ["src"],
      }),
      "website/src/App.tsx": "export function App() {\n  return <div>app</div>;\n}\n",
      "website/src/Card.jsx":
        "export function Card() {\n  return <div>card</div>;\n}\n\nexport function CardFooter() {\n  return <footer>footer</footer>;\n}\n",
    });

    const { project, warnings } = createFullProject(root);
    const discovery = discoverComponents(root, project);
    const ids = discovery.components.map((component) => component.id);

    expect(warnings.map((warning) => warning.code)).toContain("allow_js_enabled");
    expect(ids).toContain("website/src/App.tsx#App");
    expect(ids).toContain("website/src/Card.jsx#Card");
    expect(ids).toContain("website/src/Card.jsx#CardFooter");
    // 解析成功后不应再出现「候选未解析」告警
    expect(discovery.warnings.map((warning) => warning.code)).not.toContain(
      "component_exports_unresolved",
    );
  });

  it(".tsx 的具名导出与默认导出都能产出组件", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx", allowJs: true },
        include: ["src"],
      }),
      "src/Widgets.tsx": [
        "export function NamedCard() {",
        "  return <div>named</div>;",
        "}",
        "export const NamedArrow = () => <span>arrow</span>;",
        "export default function DefaultCard() {",
        "  return <section>default</section>;",
        "}",
      ].join("\n"),
    });

    const { project } = createFullProject(root);
    const discovery = discoverComponents(root, project);
    const byId = new Map(discovery.components.map((component) => [component.id, component]));

    expect(byId.has("src/Widgets.tsx#NamedCard")).toBe(true);
    expect(byId.has("src/Widgets.tsx#NamedArrow")).toBe(true);
    expect(byId.has("src/Widgets.tsx#DefaultCard")).toBe(true);
    expect(byId.get("src/Widgets.tsx#NamedCard")?.form).toBe("function");
    expect(byId.get("src/Widgets.tsx#NamedArrow")?.form).toBe("arrow");
    expect(byId.get("src/Widgets.tsx#DefaultCard")?.exportNames).toContain("default");
    // 行窗口：endLine 取声明结束行，供按组件体取代码窗口
    expect(byId.get("src/Widgets.tsx#NamedCard")?.endLine).toBe(3);
    expect(byId.get("src/Widgets.tsx#NamedArrow")?.endLine).toBe(4);
    expect(byId.get("src/Widgets.tsx#DefaultCard")?.endLine).toBe(7);
    expect(discovery.warnings).toEqual([]);
  });

  it("混合导出只把组件建成 component，不误判常量与工具函数", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "src/Panel.tsx": [
        "export const PANEL_TITLE = 'panel';",
        "export function usePanelState() {",
        "  return { open: true };",
        "}",
        "export function formatTitle(title: string) {",
        "  return title.toUpperCase();",
        "}",
        "export function Panel() {",
        "  return <div>{PANEL_TITLE}</div>;",
        "}",
      ].join("\n"),
    });

    const { project } = createFullProject(root);
    const discovery = discoverComponents(root, project);

    expect(discovery.components.map((component) => component.id)).toEqual(["src/Panel.tsx#Panel"]);
    expect(discovery.warnings).toEqual([]);
  });

  it("只有非组件导出时不产生组件，也不产生候选未解析告警", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "src/constants.ts": [
        "export const VERSION = '1.0.0';",
        "export const COLORS = ['red', 'blue'];",
        "export function useCounter() {",
        "  return 0;",
        "}",
        "export function add(a: number, b: number) {",
        "  return a + b;",
        "}",
        "export type Size = 'sm' | 'lg';",
        "export default { version: VERSION };",
      ].join("\n"),
    });

    const { project } = createFullProject(root);
    const discovery = discoverComponents(root, project);

    expect(discovery.components).toEqual([]);
    expect(discovery.warnings.map((warning) => warning.code)).not.toContain(
      "component_exports_unresolved",
    );
  });

  it("导出在类型层不可见时产出带文件名的 component_exports_unresolved 告警", () => {
    const root = makeProject({
      "src/Card.jsx":
        "export function Card() {\n  return <div>card</div>;\n}\n\nexport function CardFooter() {\n  return <footer>footer</footer>;\n}\n",
    });

    // 刻意不开启 allowJs：模拟缺失 extends 基础配置的真实故障。
    // 此时 .jsx 能被 ts-morph 解析，但 getExportedDeclarations() 返回空。
    const project = new Project({
      skipAddingFilesFromTsConfig: true,
      compilerOptions: { jsx: ts.JsxEmit.ReactJSX },
    });
    project.addSourceFileAtPath(path.join(root, "src/Card.jsx"));

    const discovery = discoverComponents(root, project);
    const warning = discovery.warnings.find((item) => item.code === "component_exports_unresolved");

    expect(discovery.components).toEqual([]);
    expect(warning).toBeDefined();
    expect(warning?.file).toBe("src/Card.jsx");
    expect(warning?.message).toContain("Card");
    expect(warning?.message).toContain("CardFooter");
  });
});
