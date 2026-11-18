/**
 * tsconfig 装载策略的验收测试。
 *
 * 背景：create-vite 等项目模板默认生成 solution-style 根配置
 * （`files: []` + `references`）。若直接交给 ts-morph，会装载到 0 个源文件，
 * 分析器全部空跑，最终产出一张「成功但为空」的图 —— 界面上表现为点击
 * 「开始分析」没有任何反应。这类静默失败必须被测试挡住。
 *
 * 每个用例在临时目录里搭一个最小项目，不污染 tests/fixtures。
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { createFullProject } from "../src/project.js";

const created: string[] = [];

function makeProject(files: Record<string, string>): string {
  const root = mkdtempSync(path.join(tmpdir(), "ast-worker-tsconfig-"));
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

const COMPONENT_SOURCE = `export function Hello() {
  return <div>hello</div>;
}
`;

describe("全量分析的 tsconfig 装载", () => {
  it("solution-style 根配置顺着 references 找到真正装载源文件的子配置", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        files: [],
        references: [{ path: "./tsconfig.app.json" }],
        compilerOptions: { baseUrl: "src", paths: { "@/*": ["./*"] } },
      }),
      "tsconfig.app.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx", paths: { "@/*": ["./*"] }, baseUrl: "src" },
        include: ["src"],
      }),
      "src/Hello.tsx": COMPONENT_SOURCE,
    });

    const result = createFullProject(root);

    expect(result.fromTsConfig).toBe(true);
    expect(path.basename(result.tsConfigPath ?? "")).toBe("tsconfig.app.json");
    expect(result.project.getSourceFiles().length).toBeGreaterThan(0);
    // 正确处理不应产告警：否则每张真实图都会挂一条常驻提示
    expect(result.warnings).toEqual([]);
  });

  it("多个引用配置时主配置取 JSX 源文件最多者，其余源文件一并加载", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        files: [],
        references: [{ path: "./tsconfig.node.json" }, { path: "./tsconfig.app.json" }],
      }),
      "tsconfig.node.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["vite.config.ts"],
      }),
      "vite.config.ts": "export const config = { plugins: [] };\n",
      "tsconfig.app.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "src/Hello.tsx": COMPONENT_SOURCE,
      "src/Other.tsx": "export function Other() {\n  return <span>o</span>;\n}\n",
    });

    const result = createFullProject(root);
    const loaded = result.project.getSourceFiles().map((file) => file.getBaseName());

    expect(path.basename(result.tsConfigPath ?? "")).toBe("tsconfig.app.json");
    expect(loaded).toEqual(expect.arrayContaining(["Hello.tsx", "Other.tsx", "vite.config.ts"]));
  });

  it("普通（非 solution）tsconfig 行为不变", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "src/Hello.tsx": COMPONENT_SOURCE,
    });

    const result = createFullProject(root);

    expect(result.fromTsConfig).toBe(true);
    expect(path.basename(result.tsConfigPath ?? "")).toBe("tsconfig.json");
    expect(result.warnings).toEqual([]);
  });

  it("solution-style 的子配置也没装载到文件时退化扫描，并留下可见告警", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        files: [],
        references: [{ path: "./tsconfig.app.json" }],
      }),
      "tsconfig.app.json": JSON.stringify({ include: ["missing-dir"] }),
      "src/Hello.tsx": COMPONENT_SOURCE,
    });

    const result = createFullProject(root);

    expect(result.fromTsConfig).toBe(false);
    expect(result.warnings.map((warning) => warning.code)).toContain("tsconfig_no_files");
    // 退化路径也必须真的扫到源码，否则又是静默空图
    expect(result.project.getSourceFiles().length).toBeGreaterThan(0);
  });

  it("完全没有 tsconfig 时走 glob 并告警 no_tsconfig", () => {
    const root = makeProject({ "src/Hello.tsx": COMPONENT_SOURCE });

    const result = createFullProject(root);

    expect(result.fromTsConfig).toBe(false);
    expect(result.warnings.map((warning) => warning.code)).toContain("no_tsconfig");
    expect(result.project.getSourceFiles().length).toBeGreaterThan(0);
  });

  it("一个源文件都找不到时必须告警 empty_project，不允许静默成功", () => {
    const root = makeProject({ "README.md": "# 空项目\n" });

    const result = createFullProject(root);

    expect(result.project.getSourceFiles()).toEqual([]);
    expect(result.warnings.map((warning) => warning.code)).toContain("empty_project");
  });
});
