import { afterEach, describe, expect, it } from "vitest";

import { extractComponentTree } from "../src/extract.js";
import type { ExtractResult } from "../src/extract.js";

/** 在 Node 里模拟浏览器 window，让 extractComponentTree 可直接测试。 */
function withWindow<T>(roots: unknown[], run: () => T): T {
  const g = globalThis as unknown as { window?: unknown };
  const original = g.window;
  g.window = { __UICA_FIBER_ROOTS__: roots };
  try {
    return run();
  } finally {
    if (original === undefined) {
      delete g.window;
    } else {
      g.window = original;
    }
  }
}

interface MockFiber {
  type: unknown;
  child: MockFiber | null;
  sibling: MockFiber | null;
  return?: MockFiber | null;
  _debugSource?: { fileName: string; lineNumber: number; columnNumber: number };
  _debugStack?: Error;
  _debugTask?: unknown;
}

type RawNode = Record<string, unknown>;

function findNode(nodes: RawNode[], name: string): RawNode | undefined {
  for (const node of nodes) {
    if (node.name === name) return node;
    const hit = findNode((node.children as RawNode[]) ?? [], name);
    if (hit !== undefined) return hit;
  }
  return undefined;
}

afterEach(() => {
  const g = globalThis as unknown as { window?: unknown };
  delete g.window;
});

describe("extractComponentTree：React ≤18（_debugSource）", () => {
  it("产出组件挂载树，包含 file/line 与 debug-source 来源", () => {
    const UserList: MockFiber = {
      type: Object.assign(function UserList() {}, { displayName: "UserList" }),
      child: null,
      sibling: null,
      _debugSource: { fileName: "/src/components/UserList.tsx", lineNumber: 5, columnNumber: 12 },
    };
    const section: MockFiber = { type: "section", child: UserList, sibling: null };
    const userListEl: MockFiber = { type: "div", child: null, sibling: section };
    const App: MockFiber = {
      type: Object.assign(function App() {}, { displayName: "App" }),
      child: userListEl,
      sibling: null,
      _debugSource: { fileName: "/src/App.tsx", lineNumber: 10, columnNumber: 3 },
    };
    const hostRoot: MockFiber = { type: null, child: App, sibling: null };
    const result = withWindow<ExtractResult>([{ current: hostRoot }], () =>
      extractComponentTree(2000),
    );

    expect(result.ok).toBe(true);
    const app = findNode(result.tree, "App");
    expect(app).toBeDefined();
    expect(app?.source).toBe("debug-source");
    expect(app?.rawFile).toBe("/src/App.tsx");
    expect(app?.line).toBe(10);

    // host 元素不产出节点，但其子树中的组件要被发现
    const userList = findNode(result.tree, "UserList");
    expect(userList).toBeDefined();
    expect(userList?.source).toBe("debug-source");
    expect((app?.children as RawNode[]).map((c) => c.name)).toContain("UserList");
  });

  it("匿名组件标注 Anonymous / name-only", () => {
    // esbuild/V8 都会对匿名函数做属性名推断，这里强制清空 name 模拟真匿名组件
    const anon = function () {
      return null;
    };
    Object.defineProperty(anon, "name", { value: "" });
    const anonymous: MockFiber = {
      type: anon as unknown as () => null,
      child: null,
      sibling: null,
    };
    const hostRoot: MockFiber = { type: null, child: anonymous, sibling: null };
    const result = withWindow<ExtractResult>([{ current: hostRoot }], () =>
      extractComponentTree(2000),
    );
    expect(result.ok).toBe(true);
    expect(result.tree).toHaveLength(1);
    const node = result.tree[0] as RawNode;
    expect(node.name).toBe("Anonymous");
    expect(node.source).toBe("name-only");
    expect(node.file).toBeUndefined();
  });
});

describe("extractComponentTree：React ≥19（_debugStack）", () => {
  it("从 _debugStack 解析 JSX 创建位点（跳过 node_modules 内部帧）", () => {
    const stack = new Error("react-stack-top-frame");
    stack.stack = [
      "Error: react-stack-top-frame",
      "    at exports.jsxDEV (http://localhost:5199/node_modules/.vite/deps/react_jsx-dev-runtime.js?v=x:244:31)",
      "    at App (http://localhost:5199/src/App.tsx:23:21)",
      "    at Object.react_stack_bottom_frame (http://localhost:5199/node_modules/.vite/deps/react-dom_client.js?v=x:20257:20)",
    ].join("\n");
    const UserList: MockFiber = {
      type: Object.assign(function UserList() {}, { displayName: "UserList" }),
      child: null,
      sibling: null,
      _debugStack: stack,
    };
    const App: MockFiber = {
      type: Object.assign(function App() {}, { displayName: "App" }),
      child: UserList,
      sibling: null,
      _debugTask: { run: () => undefined },
    };
    const hostRoot: MockFiber = { type: null, child: App, sibling: null };
    const result = withWindow<ExtractResult>([{ current: hostRoot }], () =>
      extractComponentTree(2000),
    );

    expect(result.ok).toBe(true);
    const userList = findNode(result.tree, "UserList");
    expect(userList?.source).toBe("debug-stack");
    expect(userList?.rawFile).toBe("http://localhost:5199/src/App.tsx");
    expect(userList?.line).toBe(23);

    const app = findNode(result.tree, "App");
    expect(app?.source).toBe("name-only"); // 只有 _debugTask（无名字可取）
  });
});

describe("extractComponentTree：防御", () => {
  it("预算上限截断大树", () => {
    const makeSibling = (n: number): MockFiber | null => {
      if (n === 0) return null;
      return {
        type: Object.assign(function C() {}, { displayName: `C${n}` }),
        child: null,
        sibling: makeSibling(n - 1),
      };
    };
    const hostRoot: MockFiber = { type: null, child: makeSibling(10), sibling: null };
    const result = withWindow<ExtractResult>([{ current: hostRoot }], () =>
      extractComponentTree(3),
    );
    expect(result.ok).toBe(true);
    expect(result.tree).toHaveLength(3);
  });

  it("无 window 时返回 ok:false 而不是抛错", () => {
    const result = extractComponentTree(2000);
    expect(result.ok).toBe(false);
    expect(result.tree).toHaveLength(0);
  });

  it("forwardRef/memo 包装组件被解包命名", () => {
    const inner = Object.assign(function UserCard() {}, { displayName: "UserCard" });
    const forwardRef = { $$typeof: Symbol.for("react.forward_ref"), render: inner };
    const fiber: MockFiber = { type: forwardRef, child: null, sibling: null };
    const hostRoot: MockFiber = { type: null, child: fiber, sibling: null };
    const result = withWindow<ExtractResult>([{ current: hostRoot }], () =>
      extractComponentTree(2000),
    );
    expect(result.ok).toBe(true);
    const node = result.tree[0] as RawNode;
    expect(node.name).toBe("UserCard");
  });
});
