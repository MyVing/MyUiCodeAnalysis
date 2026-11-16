/**
 * 二次封装映射测试。
 *
 * 守住四件事：
 *  1. 规则只来自配置 —— 测试里凡是断言"能识别 ProTable"，都必须有 mapping.json，
 *     不存在任何内联的业务名分支
 *  2. 配置错了要说出来（mapping_invalid / wrapper_not_found），不能当成"没配映射"
 *  3. 映射不新增节点与边，只往组件 attrs 里写注解
 *  4. `resolved` 反映渲染边是否真的连到目标，没连上不能假装成功
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, describe, expect, it } from "vitest";

import { analyzeRenders } from "../src/renders.js";
import { discoverComponents } from "../src/components.js";
import { createFullProject } from "../src/project.js";
import { createResolutionIndex } from "../src/resolver.js";
import {
  buildWrapperAttrs,
  externalNodeId,
  loadWrapperMappings,
  parseWrapperMapping,
  resolveWrappers,
} from "../src/wrappers.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

const tempDirs: string[] = [];

/** 建一个最小项目：只放需要验证的文件。 */
function makeProject(files: Record<string, string>): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-wrappers-"));
  tempDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const absolute = path.join(dir, relative);
    mkdirSync(path.dirname(absolute), { recursive: true });
    writeFileSync(absolute, content, "utf8");
  }
  return dir;
}

afterAll(() => {
  for (const dir of tempDirs) rmSync(dir, { recursive: true, force: true });
});

function validMapping(): Record<string, unknown> {
  return {
    schemaVersion: "1.0",
    pluginId: "ui.antd",
    package: "antd",
    wrappers: [{ name: "ProTable", target: "Table", props: { request: "dataSource" } }],
  };
}

describe("映射文件解析", () => {
  it("合法的 mapping.json 被收窄为强类型结构", () => {
    const mapping = parseWrapperMapping(validMapping());
    expect(mapping).not.toBeNull();
    expect(mapping?.package).toBe("antd");
    expect(mapping?.wrappers[0]).toMatchObject({
      name: "ProTable",
      target: "Table",
      props: { request: "dataSource" },
    });
  });

  it("schemaVersion 不认识就拒绝，避免按旧格式读新文件", () => {
    const raw = { ...validMapping(), schemaVersion: "2.0" };
    expect(parseWrapperMapping(raw)).toBeNull();
  });

  it("缺字段 / 类型不对一律拒绝", () => {
    expect(parseWrapperMapping(null)).toBeNull();
    expect(parseWrapperMapping([])).toBeNull();
    expect(parseWrapperMapping("{}")).toBeNull();

    // package 缺失会导致外部节点 id 拼不出来
    const noPackage = validMapping();
    delete noPackage.package;
    expect(parseWrapperMapping(noPackage)).toBeNull();

    // wrappers 空数组 = 没有规则，和"文件不存在"没有区别，不该假装读成功了
    expect(parseWrapperMapping({ ...validMapping(), wrappers: [] })).toBeNull();

    // props 是嵌套对象时无法映射到 props 名
    const nested = validMapping();
    nested.wrappers = [{ name: "ProTable", target: "Table", props: { request: { a: "b" } } }];
    expect(parseWrapperMapping(nested)).toBeNull();
  });
});

describe("规则匹配", () => {
  it("按组件名匹配，与文件路径无关", () => {
    const dir = makeProject({
      "src/a/ProTable.tsx": "export function ProTable() { return <div />; }",
      "src/Other.tsx": "export function Other() { return <div />; }",
    });
    const { project } = createFullProject(dir);
    const discovery = discoverComponents(dir, project);

    const result = resolveWrappers(
      [parseWrapperMapping(validMapping())!],
      discovery.components,
      dir,
    );

    const matched = [...result.byComponent.values()];
    expect(matched).toHaveLength(1);
    expect(matched[0]).toMatchObject({ target: "Table", package: "antd" });
    expect(result.warnings).toEqual([]);
  });

  it("同名组件出现在多处时全部匹配 —— 封装常被复制到不同目录", () => {
    const dir = makeProject({
      "src/a/ProTable.tsx": "export function ProTable() { return <div />; }",
      "src/b/ProTable.tsx": "export function ProTable() { return <div />; }",
    });
    const { project } = createFullProject(dir);
    const discovery = discoverComponents(dir, project);

    const result = resolveWrappers(
      [parseWrapperMapping(validMapping())!],
      discovery.components,
      dir,
    );

    expect(result.byComponent.size).toBe(2);
  });

  it("规则声明的组件不存在时记 info 告警：用户以为配了、其实没生效", () => {
    const dir = makeProject({
      "src/Other.tsx": "export function Other() { return <div />; }",
    });
    const { project } = createFullProject(dir);
    const discovery = discoverComponents(dir, project);

    const result = resolveWrappers(
      [parseWrapperMapping(validMapping())!],
      discovery.components,
      dir,
    );

    expect(result.byComponent.size).toBe(0);
    const [warning] = result.warnings;
    expect(warning).toMatchObject({ code: "wrapper_not_found", severity: "info" });
    expect(warning?.message).toContain("ProTable");
  });
});

describe("映射不发明结构", () => {
  it("解析结果里没有任何节点或边 —— 结构由 renders 分析器负责", () => {
    const dir = makeProject({
      "src/ProTable.tsx": "export function ProTable() { return <div />; }",
    });
    const { project } = createFullProject(dir);
    const discovery = discoverComponents(dir, project);

    const result = resolveWrappers(
      [parseWrapperMapping(validMapping())!],
      discovery.components,
      dir,
    );

    expect(result).not.toHaveProperty("edges");
    expect(result).not.toHaveProperty("nodes");
  });

  it("attrs 的 resolved 只反映真实渲染边，不因为配置声明就为 true", () => {
    const dir = makeProject({
      "src/ProTable.tsx": "export function ProTable() { return <div />; }",
    });
    const { project } = createFullProject(dir);
    const discovery = discoverComponents(dir, project);
    const result = resolveWrappers(
      [parseWrapperMapping(validMapping())!],
      discovery.components,
      dir,
    );

    // 该组件函数体里没有 <Table />，renders 边集合为空
    const attrs = buildWrapperAttrs(result.byComponent, new Set());
    const entry = [...attrs.entries()][0];
    expect(entry?.[0]).toBe(discovery.components[0]?.id);
    expect(entry?.[1]).toMatchObject({
      target: externalNodeId("antd", "Table"),
      package: "antd",
      resolved: false,
    });
  });
});

describe("从项目根目录加载", () => {
  it("真实样本仓的 mapping.json 生效，且渲染边连得上 → resolved: true", () => {
    const { project } = createFullProject(FIXTURE_ROOT);
    const discovery = discoverComponents(FIXTURE_ROOT, project);
    const resolution = createResolutionIndex(FIXTURE_ROOT, project);
    const renders = analyzeRenders({
      rootPath: FIXTURE_ROOT,
      project,
      components: discovery.components,
      declarations: discovery.declarations,
      definitionIndex: discovery.definitionIndex,
      resolution: resolution.index,
    });

    const loaded = loadWrapperMappings(FIXTURE_ROOT, discovery.components);
    expect(loaded.warnings).toEqual([]);

    const attrs = buildWrapperAttrs(
      loaded.byComponent,
      new Set(renders.edges.map((edge) => `${edge.from} ${edge.to}`)),
    );

    const proTable = [...attrs.entries()].find(([id]) => id.endsWith("#ProTable"));
    expect(proTable?.[1]).toMatchObject({
      target: "antd#Table",
      display: "Table",
      package: "antd",
      // 渲染分析器确实在这个封装里找到了 <Table />
      resolved: true,
      props: { request: "dataSource" },
    });
  });

  it("没有 mapping.json 不告警 —— 没配映射是常态", () => {
    const dir = makeProject({
      "src/Other.tsx": "export function Other() { return <div />; }",
    });
    const { project } = createFullProject(dir);
    const discovery = discoverComponents(dir, project);

    const loaded = loadWrapperMappings(dir, discovery.components);
    expect(loaded.warnings).toEqual([]);
    expect(loaded.byComponent.size).toBe(0);
  });

  it("文件存在但格式不对时告警，而不是静默当成没配", () => {
    const dir = makeProject({
      "mapping.json": "{ not json",
      "src/Other.tsx": "export function Other() { return <div />; }",
    });
    const { project } = createFullProject(dir);
    const discovery = discoverComponents(dir, project);

    const loaded = loadWrapperMappings(dir, discovery.components);
    expect(loaded.warnings).toHaveLength(1);
    const [warning] = loaded.warnings;
    expect(warning).toMatchObject({ code: "mapping_invalid", severity: "warning" });
    expect(warning?.message).toContain("mapping.json");
  });

  it("mappings/ 目录下的分文件映射一并加载", () => {
    const dir = makeProject({
      "mappings/antd.json": JSON.stringify(validMapping()),
      "src/ProTable.tsx": "export function ProTable() { return <div />; }",
    });
    const { project } = createFullProject(dir);
    const discovery = discoverComponents(dir, project);

    const loaded = loadWrapperMappings(dir, discovery.components);
    expect(loaded.warnings).toEqual([]);
    expect(loaded.byComponent.size).toBe(1);
  });
});
