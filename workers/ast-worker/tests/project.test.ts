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

  it("根目录无 tsconfig 时向下发现唯一子目录配置并装载", () => {
    const root = makeProject({
      "website/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx", baseUrl: ".", paths: { "@/*": ["./src/*"] } },
        include: ["src"],
      }),
      "website/src/Hello.tsx": COMPONENT_SOURCE,
    });

    const result = createFullProject(root);
    const codes = result.warnings.map((warning) => warning.code);

    expect(result.fromTsConfig).toBe(true);
    expect(path.relative(root, result.tsConfigPath ?? "").replace(/\\/g, "/")).toBe(
      "website/tsconfig.json",
    );
    expect(codes).toContain("nested_tsconfig_used");
    expect(codes).not.toContain("no_tsconfig");
    expect(result.project.getSourceFiles().map((file) => file.getBaseName())).toContain("Hello.tsx");
  });

  it("多份子目录配置时优先覆盖源码更多者，并在告警里解释候选", () => {
    const root = makeProject({
      "shallow/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "shallow/src/Only.ts": "export const only = 1;\n",
      "app/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "app/src/Hello.tsx": COMPONENT_SOURCE,
      "app/src/Other.ts": "export const other = 1;\n",
    });

    const result = createFullProject(root);
    const selected = result.warnings.find((warning) => warning.code === "nested_tsconfig_selected");

    expect(path.relative(root, result.tsConfigPath ?? "").replace(/\\/g, "/")).toBe(
      "app/tsconfig.json",
    );
    expect(selected).toBeDefined();
    expect(selected?.message).toContain("app/tsconfig.json");
    expect(selected?.message).toContain("shallow/tsconfig.json");
  });

  it("测试夹具目录里的 tsconfig 不参与选择，但其中的源文件仍被扫描", () => {
    const root = makeProject({
      "fixtures/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "fixtures/src/FixtureA.tsx": COMPONENT_SOURCE,
      "fixtures/src/FixtureB.tsx": COMPONENT_SOURCE,
      "app/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "app/src/Hello.tsx": COMPONENT_SOURCE,
    });

    const result = createFullProject(root);
    const codes = result.warnings.map((warning) => warning.code);

    expect(path.relative(root, result.tsConfigPath ?? "").replace(/\\/g, "/")).toBe(
      "app/tsconfig.json",
    );
    expect(codes).toContain("nested_tsconfig_partial_coverage");
    expect(codes).not.toContain("nested_tsconfig_no_coverage");
    expect(result.project.getSourceFiles().map((file) => file.getBaseName())).toEqual(
      expect.arrayContaining(["Hello.tsx", "FixtureA.tsx", "FixtureB.tsx"]),
    );
  });

  it("node_modules 里的 tsconfig 与源码不参与选择", () => {
    const root = makeProject({
      "node_modules/pkg/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "node_modules/pkg/src/External.tsx": COMPONENT_SOURCE,
      "app/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "app/src/Hello.tsx": COMPONENT_SOURCE,
    });

    const result = createFullProject(root);

    expect(path.relative(root, result.tsConfigPath ?? "").replace(/\\/g, "/")).toBe(
      "app/tsconfig.json",
    );
    expect(result.project.getSourceFiles().map((file) => file.getBaseName())).not.toContain(
      "External.tsx",
    );
  });

  it("子目录配置不覆盖任何已扫描文件时退化扫描并留下告警", () => {
    const root = makeProject({
      "legacy/tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["missing-dir"],
      }),
      "src/Hello.tsx": COMPONENT_SOURCE,
    });

    const result = createFullProject(root);
    const codes = result.warnings.map((warning) => warning.code);

    expect(result.fromTsConfig).toBe(false);
    expect(codes).toContain("nested_tsconfig_no_coverage");
    expect(codes).not.toContain("no_tsconfig");
    expect(result.project.getSourceFiles().map((file) => file.getBaseName())).toContain("Hello.tsx");
  });

  it("一个源文件都找不到时必须告警 empty_project，不允许静默成功", () => {
    const root = makeProject({ "README.md": "# 空项目\n" });

    const result = createFullProject(root);

    expect(result.project.getSourceFiles()).toEqual([]);
    expect(result.warnings.map((warning) => warning.code)).toContain("empty_project");
  });

  it("primary scope 不会加载其他引用配置，sourceRoots 只保留指定应用", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        files: [],
        references: [{ path: "./tsconfig.app.json" }, { path: "./tsconfig.admin.json" }],
      }),
      "tsconfig.app.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["apps/main/src"],
      }),
      "tsconfig.admin.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["apps/admin/src"],
      }),
      "apps/main/src/App.tsx": COMPONENT_SOURCE,
      "apps/admin/src/Admin.tsx": COMPONENT_SOURCE,
    });

    const result = createFullProject(root, {
      scopeMode: "primary",
      sourceRoots: ["apps/main"],
    });
    const loaded = result.project.getSourceFiles().map((file) => file.getFilePath());

    expect(loaded.some((file) => file.endsWith("apps/main/src/App.tsx"))).toBe(true);
    expect(loaded.some((file) => file.endsWith("apps/admin/src/Admin.tsx"))).toBe(false);
  });

  it("普通 tsconfig 的 primary scope 不会把 include 之外的源码加入项目", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["apps/main/src"],
      }),
      "apps/main/src/App.tsx": COMPONENT_SOURCE,
      "apps/admin/src/Admin.tsx": COMPONENT_SOURCE,
    });

    const result = createFullProject(root, { scopeMode: "primary" });
    const loaded = result.project.getSourceFiles().map((file) => file.getBaseName());

    expect(loaded).toContain("App.tsx");
    expect(loaded).not.toContain("Admin.tsx");
  });

  it("显式排除测试和 Storybook 文件", () => {
    const root = makeProject({
      "tsconfig.json": JSON.stringify({
        compilerOptions: { jsx: "react-jsx" },
        include: ["src"],
      }),
      "src/App.tsx": COMPONENT_SOURCE,
      "src/App.test.tsx": COMPONENT_SOURCE,
      "src/App.stories.tsx": COMPONENT_SOURCE,
      "src/__tests__/helper.ts": "export const helper = 1;\n",
    });

    const result = createFullProject(root, {
      scopeMode: "primary",
      excludeTests: true,
      excludeStorybook: true,
    });
    const loaded = result.project.getSourceFiles().map((file) => file.getFilePath());

    expect(loaded.some((file) => file.endsWith("src/App.tsx"))).toBe(true);
    expect(loaded.some((file) => file.endsWith("App.test.tsx"))).toBe(false);
    expect(loaded.some((file) => file.endsWith("App.stories.tsx"))).toBe(false);
    expect(loaded.some((file) => file.endsWith("__tests__/helper.ts"))).toBe(false);
  });
});
