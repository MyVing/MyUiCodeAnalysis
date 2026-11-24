/**
 * resolver 与组件发现的验收测试，基准是 tests/fixtures/enterprise-react-app。
 *
 * 这里的每一条断言都对应样本 README 里的一条验收点，不是「跑通就好」的快照测试。
 */

import path from "node:path";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import { fileURLToPath } from "node:url";

import { Project } from "ts-morph";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { assertUniqueIds, discoverComponents, type DiscoveredComponent } from "../src/components.js";
import { createResolutionIndex, formatResolution } from "../src/resolver.js";
import type { ResolutionIndex } from "../src/resolver.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

let project: Project;
let index: ResolutionIndex;
let components: DiscoveredComponent[];

beforeAll(() => {
  project = new Project({ tsConfigFilePath: path.join(FIXTURE_ROOT, "tsconfig.json") });
  index = createResolutionIndex(FIXTURE_ROOT, project).index;
  components = discoverComponents(FIXTURE_ROOT, project).components;
});

/** 取某个样本文件，路径相对 FIXTURE_ROOT。 */
function fixtureFile(relative: string) {
  const file = project.getSourceFile(path.join(FIXTURE_ROOT, relative));
  if (!file) throw new Error(`样本文件未加载：${relative}`);
  return file;
}

describe("module resolver", () => {
  it("路径别名按 tsconfig paths 精确解析，不退化为按名称匹配", () => {
    const userList = fixtureFile("src/pages/UserList.tsx");

    const resolved = index.resolve(userList, "@/components/TableCard");
    expect(resolved.status).toBe("internal");
    if (resolved.status !== "internal") return;
    expect(formatResolution(resolved, FIXTURE_ROOT)).toBe(
      "internal:src/components/TableCard.tsx",
    );

    // 同名文件不在其他目录：若退化为按名字全局匹配，会落到别的组件
    const rowItem = index.resolve(userList, "@/components/ProTable");
    expect(rowItem.status).toBe("internal");
    if (rowItem.status !== "internal") return;
    expect(formatResolution(rowItem, FIXTURE_ROOT)).toBe("internal:src/components/ProTable.tsx");
  });

  it("相对路径 import 解析到同目录文件", () => {
    const tableCard = fixtureFile("src/components/TableCard.tsx");
    const resolved = index.resolve(tableCard, "./RowItem");
    expect(resolved.status).toBe("internal");
    if (resolved.status !== "internal") return;
    expect(formatResolution(resolved, FIXTURE_ROOT)).toBe("internal:src/components/RowItem.tsx");
  });

  it("已安装的外部依赖归类为 external 并带上类型声明路径", () => {
    const userList = fixtureFile("src/pages/UserList.tsx");
    const resolved = index.resolve(userList, "react");
    expect(resolved.status).toBe("external");
    if (resolved.status !== "external") return;
    expect(resolved.packageName).toBe("react");
    expect(resolved.declarationFile).toBeDefined();
    expect(resolved.declarationFile?.replace(/\\/g, "/")).toMatch(/node_modules\/@types\/react\//);
  });

  it("未安装的外部依赖归为 unresolved(external)，而不是外链错误", () => {
    const proTable = fixtureFile("src/components/ProTable.tsx");
    const resolved = index.resolve(proTable, "antd");
    expect(resolved.status).toBe("unresolved");
    if (resolved.status !== "unresolved") return;
    // 裸包名未解析属于「依赖未安装」，不是本地断链
    expect(resolved.isLocal).toBe(false);
  });

  it("barrel 的 export * 全部解析到最终定义文件", () => {
    const barrel = fixtureFile("src/index.ts");
    const resolutions = index.forFile(barrel);

    expect(resolutions.get("./components/TableCard")?.status).toBe("internal");
    expect(formatResolution(resolutions.get("./components/TableCard")!, FIXTURE_ROOT)).toBe(
      "internal:src/components/TableCard.tsx",
    );
    expect(formatResolution(resolutions.get("./services/request")!, FIXTURE_ROOT)).toBe(
      "internal:src/services/request.ts",
    );
    // export * 不应该被当成「解析不到的本地模块」
    for (const [, resolution] of resolutions) {
      expect(resolution.status).toBe("internal");
    }
  });

  it("本地说明符解析失败报 error，外部依赖缺失只报 info", () => {
    const brokenProject = new Project({ skipAddingFilesFromTsConfig: true, compilerOptions: {} });
    const brokenDir = path.join(REPO_ROOT, "tests", "fixtures");
    const brokenFile = brokenProject.createSourceFile(
      path.join(brokenDir, "__synthetic_broken__.ts"),
      `import a from "./does-not-exist";\nimport b from "not-installed-pkg";\nexport const x = [a, b];\n`,
    );
    void brokenFile;

    const { warnings } = createResolutionIndex(brokenDir, brokenProject);
    const unresolved = warnings.find((w) => w.code === "unresolved_import");
    const missingDep = warnings.find((w) => w.code === "dependency_not_installed");

    expect(unresolved?.severity).toBe("error");
    expect(missingDep?.severity).toBe("info");
  });
});

describe("组件发现", () => {
  it("单文件多组件不合并：TableCard.tsx 产出 TableCard 与 CardFooter 两个节点", () => {
    const inFile = components.filter((c) => c.file === "src/components/TableCard.tsx");
    const names = inFile.map((c) => c.name).sort();

    expect(names).toEqual(["CardFooter", "TableCard"]);
    // id 必须不同，否则会被图下游按 id 合并
    expect(new Set(inFile.map((c) => c.id)).size).toBe(2);
  });

  it("node id 由「文件路径 + 组件名」组成", () => {
    const tableCard = components.find((c) => c.name === "TableCard");
    expect(tableCard?.id).toBe("src/components/TableCard.tsx#TableCard");
  });

  it("export default 与具名导出指向同一组件时只算一个，且记录两个导出名", () => {
    const tableCard = components.find((c) => c.name === "TableCard");
    expect(tableCard?.exportNames.sort()).toEqual(["TableCard", "default"]);
    expect(components.filter((c) => c.id.endsWith("#TableCard"))).toHaveLength(1);
  });

  it("barrel 不重复计数：每个组件只在其定义文件产出一个节点", () => {
    const rowItem = components.filter((c) => c.name === "RowItem");
    expect(rowItem).toHaveLength(1);
    // 定义在 RowItem.tsx，而不是 src/index.ts
    expect(rowItem[0]?.file).toBe("src/components/RowItem.tsx");
    expect(components.some((c) => c.file === "src/index.ts")).toBe(false);
  });

  it("匿名默认导出用文件名兜底，仍得到稳定 id", () => {
    const userList = components.find((c) => c.file === "src/pages/UserList.tsx");
    // `export default function UserList(...)` 有名字，用真名
    expect(userList?.name).toBe("UserList");
    expect(userList?.id).toBe("src/pages/UserList.tsx#UserList");
  });

  it("类型声明被当作类型而非组件", () => {
    // props.ts 只有 interface，不应产出任何组件
    expect(components.some((c) => c.file === "src/types/props.ts")).toBe(false);
    // 服务层 request 返回 Promise 且体内无 JSX，不是组件
    expect(components.some((c) => c.name === "request")).toBe(false);
  });

  it("未导出的内部 Hook 不计为组件", () => {
    // ProTable.tsx 中的 useProTableData / useProTableRequest 均未导出
    expect(components.some((c) => c.name === "useProTableData")).toBe(false);
    expect(components.some((c) => c.name === "useProTableRequest")).toBe(false);
  });

  it("props 类型名从具名类型引用中提取，覆盖跨文件类型", () => {
    const userList = components.find((c) => c.name === "UserList");
    expect(userList?.propsTypeName).toBe("UserListProps");

    const proTable = components.find((c) => c.name === "ProTable");
    expect(proTable?.propsTypeName).toBe("ProTableProps");

    // 内联对象类型不强行编造名字
    const cardFooter = components.find((c) => c.name === "CardFooter");
    expect(cardFooter?.propsTypeName).toBeNull();
  });

  it("每个组件都带可回溯的文件与行列号", () => {
    for (const component of components) {
      expect(component.file).toMatch(/^src\//);
      expect(component.loc.line).toBeGreaterThan(0);
      expect(component.loc.col).toBeGreaterThan(0);
    }
    const tableCard = components.find((c) => c.name === "TableCard");
    expect(tableCard?.loc).toEqual({ line: 21, col: 1 });
  });

  it("id 唯一性可显式断言", () => {
    expect(() => assertUniqueIds(components)).not.toThrow();
  });

  it("样本仓的组件集合完整", () => {
    const names = components.map((c) => c.name).sort();
    expect(names).toEqual([
      "CardFooter",
      "ProTable",
      "RowItem",
      "TableCard",
      "UserDetail",
      "UserList",
    ]);
  });
});

describe("allowJs 下静态 import 的解析兜底", () => {
  let root: string;
  let synthetic: Project;
  let result: ReturnType<typeof createResolutionIndex>;

  beforeAll(() => {
    // 用磁盘临时目录而不是内存文件系统：ts.resolveModuleName 走的是 ts.sys
    root = mkdtempSync(path.join(os.tmpdir(), "ast-resolver-allowjs-"));
    mkdirSync(path.join(root, "src"), { recursive: true });
    writeFileSync(
      path.join(root, "tsconfig.json"),
      JSON.stringify({
        compilerOptions: {
          target: "ES2020",
          module: "ESNext",
          moduleResolution: "node",
          allowJs: true,
          jsx: "react-jsx",
          baseUrl: ".",
          paths: { "@/*": ["src/*"] },
          strict: false,
        },
        include: ["src"],
      }),
      "utf8",
    );
    writeFileSync(
      path.join(root, "src/public-path.js"),
      "export const publicPath = '/base/';\n",
      "utf8",
    );
    writeFileSync(
      path.join(root, "src/Widget.jsx"),
      "export default function Widget() { return null; }\n",
      "utf8",
    );
    writeFileSync(
      path.join(root, "src/main.ts"),
      [
        'import { publicPath } from "@/public-path";',
        'import Widget from "./Widget";',
        'import "./missing";',
        "export const base = publicPath;",
        "export const view = Widget;",
      ].join("\n"),
      "utf8",
    );

    synthetic = new Project({ tsConfigFilePath: path.join(root, "tsconfig.json") });
    result = createResolutionIndex(root, synthetic);
  }, 30_000);

  afterAll(() => {
    rmSync(root, { recursive: true, force: true });
  });

  function mainFile() {
    const file = synthetic.getSourceFile(path.join(root, "src/main.ts"));
    if (!file) throw new Error("synthetic main.ts 未加载");
    return file;
  }

  it("别名指向纯 .js 文件时归类为 internal", () => {
    const resolved = result.index.resolve(mainFile(), "@/public-path");
    expect(resolved.status).toBe("internal");
    if (resolved.status !== "internal") return;
    expect(formatResolution(resolved, root)).toBe("internal:src/public-path.js");
  });

  it("相对路径指向 .jsx 文件时归类为 internal", () => {
    const resolved = result.index.resolve(mainFile(), "./Widget");
    expect(resolved.status).toBe("internal");
    if (resolved.status !== "internal") return;
    expect(formatResolution(resolved, root)).toBe("internal:src/Widget.jsx");
  });

  it("确实不存在的本地模块仍报 unresolved_import error", () => {
    const resolved = result.index.resolve(mainFile(), "./missing");
    expect(resolved.status).toBe("unresolved");

    const errors = result.warnings.filter((w) => w.code === "unresolved_import");
    expect(errors.some((w) => w.message.includes('"./missing"'))).toBe(true);
    // .js / .jsx 两个目标不应再产生任何 unresolved_import
    expect(errors.some((w) => w.message.includes("public-path"))).toBe(false);
    expect(errors.some((w) => w.message.includes("Widget"))).toBe(false);
  });
});
