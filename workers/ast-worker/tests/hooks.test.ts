/**
 * hooks / state 分析测试。
 *
 * 重点守住三件事：
 *  1. useState 产出 **state** 节点，而不是 hook 节点（否则前端按"状态"筛选会漏）
 *  2. hook 归属只看组件函数体的**顶层语句**，穿过回调就停（React 规则如此，
 *     `useEffect(() => { useFoo() })` 里的 useFoo 不属于本组件）
 *  3. reads / writes 靠符号引用，不靠位置推断；取不到位置就不产边，不编造
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { IREdge } from "@uicodeanalysis/contracts";

import { discoverComponents } from "../src/components.js";
import { analyzeHooks, type HooksAnalysisResult } from "../src/hooks.js";
import { createFullProject } from "../src/project.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

function analyze(rootPath: string): HooksAnalysisResult {
  const { project } = createFullProject(rootPath);
  const discovery = discoverComponents(rootPath, project);
  return analyzeHooks({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
  });
}

const tempDirs: string[] = [];

function analyzeSynthetic(files: Record<string, string>): HooksAnalysisResult {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-hooks-"));
  tempDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const absolute = path.join(dir, relative);
    mkdirSync(path.dirname(absolute), { recursive: true });
    writeFileSync(absolute, content, "utf8");
  }
  return analyze(dir);
}

afterAll(() => {
  for (const dir of tempDirs) rmSync(dir, { recursive: true, force: true });
});

/** 找出指向某个节点的边；不存在时直接失败，避免断言静默跳过。 */
function edgeTo(result: HooksAnalysisResult, targetId: string, kind: IREdge["kind"]): IREdge {
  const edge = result.edges.find((e) => e.to === targetId && e.kind === kind);
  if (!edge) {
    const actual = result.edges.map((e) => `${e.kind}:${e.to}`).join("\n");
    throw new Error(`缺少 ${kind} 边指向 ${targetId}；实际边：\n${actual}`);
  }
  return edge;
}

function nodeIdsOf(result: HooksAnalysisResult, kind: "hook" | "state"): string[] {
  const nodes = kind === "hook" ? result.hookNodes : result.stateNodes;
  return nodes.map((n) => n.id);
}

/** 取第一个 hook 节点；不存在直接失败，避免断言在 undefined 上静默通过。 */
function firstHook(result: HooksAnalysisResult) {
  const hook = result.hookNodes[0];
  if (!hook) throw new Error("期望至少一个 hook 节点，实际为空");
  return hook;
}

/** 取第一条指定类型的边；不存在直接失败。 */
function firstEdge(result: HooksAnalysisResult, to: string, kind: IREdge["kind"]) {
  const edge = result.edges.find((e) => e.to === to && e.kind === kind);
  if (!edge) throw new Error(`缺少 ${kind} 边指向 ${to}`);
  return edge;
}

describe("hooks / state（真实样本仓）", () => {
  let result: HooksAnalysisResult;

  beforeAll(() => {
    result = analyze(FIXTURE_ROOT);
  });

  it("useState 产出 state 节点，带 setter、类型参数与初始值", () => {
    const rows = result.stateNodes.find(
      (n) => n.id === "src/pages/UserList.tsx#UserList/rows",
    );
    expect(rows).toBeDefined();
    expect(rows?.kind).toBe("state");
    expect(rows?.attrs).toMatchObject({
      hookName: "useState",
      setter: "setRows",
      typeText: "UserRowProps[]",
      initial: "[]",
    });
    // state 节点是语法事实，位置必须能跳回源码
    expect(rows?.file).toBe("src/pages/UserList.tsx");
    expect(rows?.loc).toEqual({ line: 28, col: 9 });
  });

  it("useState 不重复产出 hook 节点", () => {
    // 同一行在 hookNodes 里出现就说明分类错了
    const hookIds = nodeIdsOf(result, "hook");
    expect(hookIds.some((id) => id.includes("/rows"))).toBe(false);
  });

  it("useEffect 产出 hook 节点，空依赖数组记录为空数组而不是 null", () => {
    const effect = result.hookNodes.find((n) => n.id.endsWith("/useEffect"));
    expect(effect).toBeDefined();
    expect(effect?.attrs).toMatchObject({ hookName: "useEffect", custom: false, deps: [] });
    expect(effect?.loc).toEqual({ line: 31, col: 3 });
    // 回调体跨多行：endLine 必须落在调用表达式结束行，而不是起始行
    expect(effect?.attrs.endLine).toBe(36);
  });

  it("useCallback 以绑定变量名命名，并记录依赖", () => {
    const callback = result.hookNodes.find((n) => n.id.endsWith("/handleSelect"));
    expect(callback).toBeDefined();
    expect(callback?.name).toBe("handleSelect");
    expect(callback?.attrs).toMatchObject({
      hookName: "useCallback",
      binding: "handleSelect",
      deps: ["onSelect"],
    });
    expect(callback?.attrs.endLine).toBe(43);
  });

  it("自定义 useXxx 标记为 custom", () => {
    const custom = result.hookNodes.find((n) => n.id.endsWith("/useProTableData"));
    expect(custom).toBeDefined();
    expect(custom?.attrs.custom).toBe(true);
  });

  it("组件的 hook 以 uses 边连到组件；state 走 reads / writes", () => {
    const componentId = "src/pages/UserList.tsx#UserList";

    for (const hookId of nodeIdsOf(result, "hook").filter((id) => id.startsWith(componentId))) {
      expect(edgeTo(result, hookId, "uses").from).toBe(componentId);
    }

    const stateId = "src/pages/UserList.tsx#UserList/rows";
    expect(edgeTo(result, stateId, "reads").from).toBe(componentId);
    expect(edgeTo(result, stateId, "writes").from).toBe(componentId);
  });

  it("reads 证据指向真实的读取位置（JSX 里用了 rows）", () => {
    const edge = edgeTo(result, "src/pages/UserList.tsx#UserList/rows", "reads");
    const loc = edge.evidence[0].loc;

    expect(edge.evidence[0].file).toBe("src/pages/UserList.tsx");
    // rows 在 <TableCard rows={rows}> 处被读
    expect(loc?.line).toBe(52);
  });

  it("setter 既被直接调用、也被当回调传递时，两条位置都进证据", () => {
    const edge = edgeTo(result, "src/pages/UserList.tsx#UserList/loading", "writes");
    const lines = edge.evidence.map((e) => e.loc?.line).sort();

    // setLoading(true) 在 effect 里；setLoading(false) 在 finally 回调里
    expect(lines).toEqual([32, 35]);
  });

  it("hook 调用跨过回调边界时不误归到组件上", () => {
    // UserList 的 effect 体内有 fetchUsers 等调用，但都不是 hook；
    // 关键是 useProTableRequest 只在 ProTable 里，不能出现在 UserList 名下
    const userListHooks = result.hookNodes.filter((n) =>
      n.id.startsWith("src/pages/UserList.tsx#UserList"),
    );
    expect(userListHooks.map((n) => n.name).sort()).toEqual(["handleSelect", "useEffect"]);
  });

  it("每个 hook / state 节点都带静态证据且置信度为 1", () => {
    for (const node of [...result.hookNodes, ...result.stateNodes]) {
      expect(node.evidence[0]).toMatchObject({ source: "static", confidence: 1 });
      expect(node.file).toBeDefined();
      expect(node.loc).toBeDefined();
    }
  });

  it("产出的边端点都能在节点集合里找到", () => {
    const ids = new Set([
      ...result.hookNodes.map((n) => n.id),
      ...result.stateNodes.map((n) => n.id),
    ]);
    // from 是组件 id，由调用方保证存在；这里校验 to 一定是本模块产出的节点
    for (const edge of result.edges) {
      expect(ids.has(edge.to)).toBe(true);
      expect(edge.evidence.length).toBeGreaterThan(0);
    }
  });
});

describe("hooks / state 的边界（合成项目）", () => {
  it("hook 判定看前缀，普通函数调用不算", () => {
    const result = analyzeSynthetic({
      "src/Plain.tsx": `
        declare function formatDate(value: string): string;
        declare function useThing(): number;
        export function Plain() {
          formatDate("x");
          useThing();
          return <div />;
        }
      `,
    });

    expect(nodeIdsOf(result, "hook")).toEqual(["src/Plain.tsx#Plain/useThing"]);
  });

  it("嵌套函数里的调用不归属本组件（React 规则：hook 只能在顶层调用）", () => {
    const result = analyzeSynthetic({
      "src/Nested.tsx": `
        declare function useInner(): void;
        export function Nested() {
          const handler = () => { useInner(); };
          return <div onClick={handler} />;
        }
      `,
    });

    // handler 的体内是独立的函数作用域，useInner 不是 Nested 的 hook
    expect(nodeIdsOf(result, "hook")).toEqual([]);
  });

  it("条件表达式里的 hook 调用仍归属组件（在顶层语句内，未跨函数）", () => {
    const result = analyzeSynthetic({
      "src/Conditional.tsx": `
        declare function useMaybe(on: boolean): number;
        export function Conditional({ on }: { on: boolean }) {
          const value = on ? useMaybe(on) : 0;
          return <div>{value}</div>;
        }
      `,
    });

    expect(nodeIdsOf(result, "hook")).toEqual(["src/Conditional.tsx#Conditional/useMaybe"]);
  });

  it("没有绑定的 hook 用序号保证 id 唯一", () => {
    const result = analyzeSynthetic({
      "src/Effects.tsx": `
        declare function useEffect(fn: () => void, deps: unknown[]): void;
        export function Effects() {
          useEffect(() => {}, []);
          useEffect(() => {}, []);
          return <div />;
        }
      `,
    });

    expect(nodeIdsOf(result, "hook")).toEqual([
      "src/Effects.tsx#Effects/useEffect",
      "src/Effects.tsx#Effects/useEffect#2",
    ]);
  });

  it("hook id 不随上方插入注释而漂移", () => {
    const before = analyzeSynthetic({
      "src/Stable.tsx": `
        declare function useEffect(fn: () => void, deps: unknown[]): void;
        export function Stable() { useEffect(() => {}, []); return <div />; }
      `,
    });
    const after = analyzeSynthetic({
      "src/Stable.tsx": `
        declare function useEffect(fn: () => void, deps: unknown[]): void;
        // 新增一行注释
        // 再来一行
        export function Stable() { useEffect(() => {}, []); return <div />; }
      `,
    });

    expect(nodeIdsOf(after, "hook")).toEqual(nodeIdsOf(before, "hook"));
  });

  it("非解构形式的 useState 不产 state 节点", () => {
    const result = analyzeSynthetic({
      "src/Weird.tsx": `
        declare function useState<T>(initial: T): [T, (next: T) => void];
        export function Weird() {
          const tuple = useState(0);
          return <div>{tuple}</div>;
        }
      `,
    });

    expect(nodeIdsOf(result, "state")).toEqual([]);
    // 它仍然是一次 hook 调用，按 hook 记录，只是不冒充状态（没有 setter 就谈不上读写）
    expect(result.hookNodes).toHaveLength(1);
    expect(firstHook(result).attrs.hookName).toBe("useState");
  });

  it("类组件不产 hooks / state", () => {
    const result = analyzeSynthetic({
      "src/Legacy.tsx": `
        declare const React: any;
        export class Legacy extends React.Component {
          state = { count: 0 };
          render() { return <div>{this.state.count}</div>; }
        }
      `,
    });

    expect(nodeIdsOf(result, "hook")).toEqual([]);
    expect(nodeIdsOf(result, "state")).toEqual([]);
  });

  it("依赖数组缺省时 deps 为 null（表示每次渲染都执行）", () => {
    const result = analyzeSynthetic({
      "src/NoDeps.tsx": `
        declare function useEffect(fn: () => void): void;
        export function NoDeps() { useEffect(() => {}); return <div />; }
      `,
    });

    const effect = firstHook(result);
    expect(effect.loc).toBeDefined();
    expect(effect.attrs.deps).toBeNull();
  });

  it("状态的 reads / writes 边各只有一条，多个引用位置进同一份证据", () => {
    const result = analyzeSynthetic({
      "src/Multi.tsx": `
        declare function useState<T>(initial: T): [T, (next: T) => void];
        export function Multi() {
          const [count, setCount] = useState(0);
          const double = count * 2;
          return <div onClick={() => setCount(count + 1)}>{count}{double}</div>;
        }
      `,
    });

    const stateId = "src/Multi.tsx#Multi/count";
    const reads = result.edges.filter((e) => e.to === stateId && e.kind === "reads");
    const writes = result.edges.filter((e) => e.to === stateId && e.kind === "writes");

    expect(reads).toHaveLength(1);
    expect(writes).toHaveLength(1);
    // count 被读了三次（double、setCount 的参数、JSX 渲染）
    expect(firstEdge(result, stateId, "reads").evidence.length).toBe(3);
  });
});
