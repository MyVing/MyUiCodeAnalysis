import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// shared/schemas 使用 Draft 2020-12；Ajv 主包默认只认 draft-07，必须走 2020 构建
// eslint-disable-next-line import/extensions
import Ajv2020 from "ajv/dist/2020.js";
// eslint-disable-next-line import/extensions
import addFormats from "ajv-formats";
import { beforeAll, describe, expect, it } from "vitest";

import type { IRGraph } from "@uicodeanalysis/contracts";

import { analyzeApi } from "../src/api.js";
import { discoverComponents } from "../src/components.js";
import { analyzeEvents } from "../src/events.js";
import { analyzeHooks } from "../src/hooks.js";
import { buildGraph } from "../src/ir-builder.js";
import { buildJsxInventory } from "../src/jsx-inventory.js";
import { analyzeNavigation } from "../src/navigate.js";
import { createFullProject } from "../src/project.js";
import { analyzeRenders } from "../src/renders.js";
import { analyzeProps } from "../src/props.js";
import { createResolutionIndex } from "../src/resolver.js";
import { analyzeRoutes } from "../src/routes.js";
import { analyzeView } from "../src/view.js";
import { buildWrapperAttrs, inferWrappers, loadWrapperMappings } from "../src/wrappers.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const SCHEMA_PATH = path.join(REPO_ROOT, "shared", "schemas", "ir.schema.json");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

const schema = JSON.parse(readFileSync(SCHEMA_PATH, "utf8"));
const ajv = new Ajv2020({ strict: false, allErrors: true });
// 不开 formats 时 date-time 会被静默忽略，等于少校验一层
addFormats(ajv);
const validateFn = ajv.compile(schema);

function checkContract(graph: unknown): void {
  const ok = validateFn(graph);
  if (!ok) {
    throw new Error(`IRGraph 不符合 shared schema：${JSON.stringify(validateFn.errors, null, 2)}`);
  }
}

function emptyGraphRequest(analysisId: string) {
  return {
    projectId: "prj_test",
    rootPath: FIXTURE_ROOT,
    analysisId,
  };
}

/** 跑一遍真实的样本仓分析，而不是构造假数据。 */
function analyzeFixture(analysisId = "an_test"): IRGraph {
  const { project } = createFullProject(FIXTURE_ROOT);
  const resolution = createResolutionIndex(FIXTURE_ROOT, project);
  const discovery = discoverComponents(FIXTURE_ROOT, project);
  const renders = analyzeRenders({
    rootPath: FIXTURE_ROOT,
    project,
    components: discovery.components,
    declarations: discovery.declarations,
    definitionIndex: discovery.definitionIndex,
    resolution: resolution.index,
  });
  const hooks = analyzeHooks({
    rootPath: FIXTURE_ROOT,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  const api = analyzeApi({
    rootPath: FIXTURE_ROOT,
    project,
    definitionIndex: discovery.definitionIndex,
  });
  const props = analyzeProps({
    rootPath: FIXTURE_ROOT,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  const knownNodes = [...hooks.hookNodes, ...hooks.stateNodes, ...api.apiNodes, ...api.fnNodes];
  const events = analyzeEvents({
    inventory: buildJsxInventory(FIXTURE_ROOT, project, discovery.components, discovery.declarations),
    components: discovery.components,
    knownNodes,
    propsFields: new Map(
      [...props.byComponent].map(([id, info]) => [id, info.fields.map((f) => f.name)]),
    ),
    rootPath: FIXTURE_ROOT,
  });
  // 视图层与事件层共用同一份 JSX 清单：元素 id 必须一致，否则同一块 UI 在图上
  // 会变成两个节点，视图树与事件链就断了。事件层先跑，视图层再为它收录的
  // 元素补 contains 父边（与 index.ts 的接线顺序保持一致）。
  const inventory = buildJsxInventory(
    FIXTURE_ROOT,
    project,
    discovery.components,
    discovery.declarations,
  );
  const view = analyzeView({
    inventory,
    extraElementIds: new Set(events.elementNodes.map((node) => node.id)),
  });
  const wrappers = loadWrapperMappings(FIXTURE_ROOT, discovery.components);
  const inferredWrappers = inferWrappers(
    FIXTURE_ROOT,
    project,
    discovery.components,
    discovery.declarations,
  );
  const routes = analyzeRoutes({
    rootPath: FIXTURE_ROOT,
    project,
    components: discovery.components,
    definitionIndex: discovery.definitionIndex,
    resolution: resolution.index,
  });
  const navigation = analyzeNavigation({
    rootPath: FIXTURE_ROOT,
    inventory,
    elementNodeIds: new Set([
      ...view.elementNodes.map((node) => node.id),
      ...events.elementNodes.map((node) => node.id),
    ]),
    routeNodes: routes.routeNodes,
    components: discovery.components,
    declarations: discovery.declarations,
  });

  return buildGraph({
    request: emptyGraphRequest(analysisId),
    components: discovery.components,
    props: props.byComponent,
    wrappers: buildWrapperAttrs(
      wrappers.byComponent,
      new Set(renders.edges.map((edge) => `${edge.from} ${edge.to}`)),
      inferredWrappers.byComponent,
    ),
    edges: [
      ...renders.edges,
      ...hooks.edges,
      ...api.edges,
      ...events.edges,
      ...view.edges,
      ...routes.edges,
      ...navigation.edges,
    ],
    externalNodes: renders.externalNodes,
    extraNodes: [
      ...knownNodes,
      ...view.elementNodes,
      ...events.elementNodes,
      ...routes.routeNodes,
    ],
    warnings: [
      ...resolution.warnings,
      ...discovery.warnings,
      ...props.warnings,
      ...renders.warnings,
      ...hooks.warnings,
      ...api.warnings,
      ...events.warnings,
      ...view.warnings,
      ...wrappers.warnings,
      ...routes.warnings,
      ...navigation.warnings,
    ],
  });
}

describe("ast-worker IR 输出符合契约", () => {
  let graph: IRGraph;

  beforeAll(() => {
    graph = analyzeFixture();
  });

  it("真实分析产物符合 shared/schemas/ir.schema.json", () => {
    checkContract(graph);
  });

  it("空输入也产出合法图，而不是靠特例", () => {
    const empty = buildGraph({ request: emptyGraphRequest("an_empty"), components: [] });
    checkContract(empty);
    expect(empty.nodes).toEqual([]);
    expect(empty.edges).toEqual([]);
  });

  it("版本号与 schema 演进同步", () => {
    expect(graph.version).toBe("1.1");
  });

  it("回传 analysisId，便于编排层定位产物", () => {
    expect(analyzeFixture("an_custom").analysisId).toBe("an_custom");
  });

  it("显式标记 sample=false，与后端示例图区分", () => {
    expect(graph.meta?.sample).toBe(false);
  });

  it("项目内组件节点以语法事实入图：置信度 1.0 且带行列号", () => {
    const components = graph.nodes.filter(
      (node) => node.kind === "component" && node.attrs.external !== true,
    );
    expect(components.length).toBeGreaterThan(0);

    for (const node of components) {
      expect(node.file).toBeDefined();
      expect(node.loc).toBeDefined();
      expect(node.evidence[0]).toMatchObject({ source: "static", confidence: 1 });
      // file / loc 是一等字段，不得塞进 attrs
      expect(node.attrs).not.toHaveProperty("file");
      expect(node.attrs).not.toHaveProperty("loc");
    }
  });

  it("依赖未安装记为 info 级告警，而不是让分析失败或静默丢节点", () => {
    const antd = graph.warnings?.find((w) => w.code === "dependency_not_installed");
    expect(antd?.severity).toBe("info");
  });

  it("renders 边满足契约：端点必须指向图中真实存在的节点", () => {
    const renders = graph.edges.filter((e) => e.kind === "renders");
    expect(renders.length).toBeGreaterThan(0);
    const nodeIds = new Set(graph.nodes.map((n) => n.id));

    for (const edge of renders) {
      expect(nodeIds.has(edge.from)).toBe(true);
      expect(nodeIds.has(edge.to)).toBe(true);
      expect(edge.evidence.length).toBeGreaterThan(0);
    }
  });

  it("route 节点以语法事实上图：kind=route、置信度 1.0、带 file / loc 与 params", () => {
    const routes = graph.nodes.filter((n) => n.kind === "route");
    expect(routes.length).toBeGreaterThan(0);

    const detail = routes.find((n) => n.name === "/users/:userId");
    expect(detail).toBeDefined();
    expect(detail?.file).toBe("src/router.tsx");
    expect(detail?.loc).toBeDefined();
    expect(detail?.attrs.params).toEqual(["userId"]);
    expect(detail?.evidence[0]).toMatchObject({ source: "static", confidence: 1 });

    for (const node of routes) {
      expect(node.file).toBeDefined();
      expect(node.loc).toBeDefined();
      expect(Array.isArray(node.attrs.params)).toBe(true);
    }
  });

  it("route 节点通过 renders 边连到页面组件，端点合法", () => {
    const nodeIds = new Set(graph.nodes.map((n) => n.id));
    const routeEdges = graph.edges.filter((e) => nodeIds.has(e.from) && e.from.includes("#/"));

    expect(routeEdges.length).toBeGreaterThan(0);
    for (const edge of routeEdges) {
      expect(edge.kind).toBe("renders");
      expect(nodeIds.has(edge.to)).toBe(true);
      // 边是推断：置信度低于节点的 1.0
      expect(edge.evidence[0]?.confidence).toBeLessThan(1);
    }

    expect(
      graph.edges.some(
        (e) => e.from === "src/router.tsx#/users/:userId" && e.to === "src/pages/UserDetail.tsx#UserDetail",
      ),
    ).toBe(true);
  });

  it("hook / state 节点以项目内节点身份入图，并带 file / loc", () => {
    const hooks = graph.nodes.filter((n) => n.kind === "hook" || n.kind === "state");
    expect(hooks.length).toBeGreaterThan(0);

    for (const node of hooks) {
      expect(node.file).toBeDefined();
      expect(node.loc).toBeDefined();
      expect(node.evidence[0].source).toBe("static");
      // 节点 id 以所属组件 id 为前缀，便于从组件下钻
      expect(node.id).toMatch(/^src\/.+\.tsx#.+\/.+$/);
    }
  });

  it("API 链路完整入图：组件 → 函数 → 接口，README 的验收点不缺席", () => {
    const apiNode = graph.nodes.find((n) => n.kind === "api");
    expect(apiNode?.name).toBe("GET /api/users");

    const fnNode = graph.nodes.find((n) => n.kind === "fn");
    expect(fnNode?.name).toBe("fetchUsers");

    // 两跳都要在：组件 calls 函数，函数 requests 接口
    expect(graph.edges.some((e) => e.kind === "calls" && e.from === "src/pages/UserList.tsx#UserList")).toBe(true);
    expect(graph.edges.some((e) => e.kind === "requests" && e.to === apiNode?.id)).toBe(true);
  });

  it("api / fn 节点带 file 与 loc：点开节点能跳回源码", () => {
    for (const node of graph.nodes.filter((n) => n.kind === "api" || n.kind === "fn")) {
      expect(node.file).toBeDefined();
      expect(node.loc).toBeDefined();
      expect(node.evidence[0].source).toBe("static");
    }
  });

  it("所有边的端点都存在，且边 id 不重复", () => {
    const nodeIds = new Set(graph.nodes.map((n) => n.id));
    const edgeIds = new Set<string>();

    for (const edge of graph.edges) {
      expect(nodeIds.has(edge.from)).toBe(true);
      expect(nodeIds.has(edge.to)).toBe(true);
      expect(edgeIds.has(edge.id)).toBe(false);
      edgeIds.add(edge.id);
    }
  });

  it("外部组件节点无需 file / loc，但内部组件必须带", () => {
    for (const node of graph.nodes) {
      const isExternal = node.attrs.external === true;
      if (isExternal) {
        expect(node.file).toBeUndefined();
      } else {
        expect(node.file).toBeDefined();
        expect(node.loc).toBeDefined();
      }
    }
  });

  it("事件以 element 节点承载，且落在真实节点上而不是编出来的 handler", () => {
    const elements = graph.nodes.filter((n) => n.kind === "element");
    expect(elements.length).toBeGreaterThan(0);

    const nodeIds = new Set(graph.nodes.map((n) => n.id));
    for (const node of elements) {
      expect(node.file).toBeDefined();
      expect(node.loc).toBeDefined();
      // 元素要么带事件（事件层），要么带视图角色（视图层），两者可同时存在
      const hasEvents = Array.isArray(node.attrs.events);
      const hasRole = typeof node.attrs.viewRole === "string";
      expect(hasEvents || hasRole).toBe(true);
      // 元素不能成为孤立点：要么被组件 uses，要么被父元素 contains
      expect(
        graph.edges.some(
          (e) => (e.kind === "uses" || e.kind === "contains") && e.to === node.id,
        ),
      ).toBe(true);
    }

    // 事件边的端点全部是图上已有节点（不新增 NodeKind 的依据）
    for (const edge of graph.edges) {
      if (!edge.from.includes("/jsx:")) continue;
      expect(nodeIds.has(edge.from)).toBe(true);
      expect(nodeIds.has(edge.to)).toBe(true);
    }
  });

  it("静态分析不使用 event 这种未受控的 NodeKind", () => {
    const kinds = new Set(graph.nodes.map((n) => n.kind));
    expect(kinds.has("event" as never)).toBe(false);
  });

  it("二次封装经配置生效：ProTable 的 attrs.wrapper 注解到 antd#Table", () => {
    const proTable = graph.nodes.find((n) => n.id.endsWith("#ProTable"));
    expect(proTable).toBeDefined();

    const wrapper = proTable?.attrs.wrapper as Record<string, unknown> | undefined;
    expect(wrapper).toMatchObject({ target: "antd#Table", package: "antd", resolved: true });
    // props 映射来自 mapping.json，不是写死在分析器里
    expect(wrapper?.props).toEqual({
      request: "dataSource",
      columns: "columns",
      rowKey: "rowKey",
      onRowClick: "onRow",
    });

    // 映射本身不新增结构：那条边必须来自 renders 分析器
    expect(
      graph.edges.some((e) => e.kind === "renders" && e.from === proTable?.id && e.to === "antd#Table"),
    ).toBe(true);
  });

  it("封装注解写入 attrs.wrapper 且不破坏 IR 契约", () => {
    const wrappers = graph.nodes.filter((node) => node.attrs.wrapper !== undefined);
    expect(wrappers.length).toBeGreaterThan(0);
    checkContract(graph);
  });
  it("同一份源码多次分析产物稳定（节点与边顺序不抖动）", () => {
    const again = analyzeFixture();
    expect(again.nodes.map((n) => n.id)).toEqual(graph.nodes.map((n) => n.id));
    expect(again.edges.map((e) => e.id)).toEqual(graph.edges.map((e) => e.id));
  });
});
