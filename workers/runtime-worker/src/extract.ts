/**
 * fiber 树遍历与组件树提取：在浏览器内执行（page.evaluate 序列化）。
 *
 * **必须自包含**：Playwright 只序列化传入函数自身的源码，模块级辅助函数
 * 不会跟着过去，因此全部 helper 嵌在函数体内。定位来源降级链（与
 * runtime.schema.json 的 ComponentNode.source 对齐）：
 *   _debugSource（file+line）> _debugTask（React 19，仅名）> 仅 name。
 * 抓不到就标注 name-only，不猜路径——错误的 file 会污染合并证据。
 */

/** 浏览器内由 installCollector 收集的 fiber root。 */
declare global {
  interface Window {
    __UICA_FIBER_ROOTS__?: unknown[];
  }
}

export interface ExtractResult {
  ok: boolean;
  tree: Array<Record<string, unknown>>;
  error?: string;
}

export function extractComponentTree(maxNodes: number): ExtractResult {
  type FiberLike = {
    type: unknown;
    child: FiberLike | null;
    sibling: FiberLike | null;
    return?: FiberLike | null;
    _debugSource?: { fileName?: string; lineNumber?: number; columnNumber?: number };
    _debugStack?: Error;
  };
  type ComponentNode = {
    name: string;
    source: "debug-source" | "debug-stack" | "name-only";
    children: ComponentNode[];
    rawFile?: string;
    line?: number;
    col?: number;
  };

  // 前端内部组件（Suspense/StrictMode 等包装）按名字过滤
  const INTERNAL_NAMES = new Set([
    "StrictMode",
    "Profiler",
    "Suspense",
    "ContextProvider",
    "ContextConsumer",
    "Boundary",
  ]);

  function componentName(type: unknown): string {
    if (typeof type === "function") {
      const fn = type as { displayName?: string; name?: string };
      return fn.displayName || fn.name || "";
    }
    if (type !== null && typeof type === "object") {
      // forwardRef: { render: fn }; memo/lazy: { type: fn }
      const obj = type as { displayName?: string; render?: unknown; type?: unknown };
      const inner = obj.render ?? obj.type;
      if (typeof inner === "function") {
        const fn = inner as { displayName?: string; name?: string };
        return obj.displayName || fn.displayName || fn.name || "";
      }
    }
    return "";
  }

  function isCollectable(type: unknown): boolean {
    if (typeof type === "function") return true;
    if (type !== null && typeof type === "object") {
      const obj = type as { render?: unknown; type?: unknown };
      return typeof obj.render === "function" || typeof obj.type === "function";
    }
    return false;
  }

  function parseDebugStack(stack: unknown): { rawFile: string; line: number; col: number } | null {
    if (!stack || typeof (stack as Error).stack !== "string") {
      return null;
    }
    // _debugStack 的帧链：jsxDEV 内部帧 → 应用帧（组件被创建的 JSX 位点）
    // → react_stack_bottom_frame 等内部帧。取第一个非 node_modules 的应用帧。
    for (const line of ((stack as Error).stack as string).split("\n")) {
      if (line.includes("react_stack_top_frame") || line.includes("react_stack_bottom_frame")) {
        continue;
      }
      const atFrame = /^\s*at\s+.*?\((.+)\)\s*$/.exec(line);
      let loc = atFrame !== null ? (atFrame[1] ?? "") : line.trim().replace(/^at\s+/, "");
      if (!loc || loc.includes("node_modules")) {
        continue;
      }
      // loc 形如 http://host/src/App.tsx:23:21 或 file:///D:/x/y.tsx:1:2
      const lastColon2 = loc.lastIndexOf(":");
      const lastColon1 = loc.lastIndexOf(":", lastColon2 - 1);
      if (lastColon1 <= 0 || lastColon2 <= lastColon1) {
        continue;
      }
      const rawFile = loc.slice(0, lastColon1);
      const lineNo = Number.parseInt(loc.slice(lastColon1 + 1, lastColon2), 10);
      const colNo = Number.parseInt(loc.slice(lastColon2 + 1), 10);
      if (!rawFile || !Number.isInteger(lineNo) || !Number.isInteger(colNo)) {
        continue;
      }
      return { rawFile, line: lineNo, col: colNo };
    }
    return null;
  }

  function makeNode(fiber: FiberLike): ComponentNode {
    const name0 = componentName(fiber.type);
    const node: ComponentNode = { name: name0 || "Anonymous", source: "name-only", children: [] };

    // React ≤18：_debugSource { fileName, lineNumber, columnNumber }（定义位点）
    const dbg = fiber._debugSource;
    if (dbg && typeof dbg.fileName === "string" && dbg.fileName) {
      node.rawFile = dbg.fileName;
      if (typeof dbg.lineNumber === "number" && dbg.lineNumber >= 1) node.line = dbg.lineNumber;
      if (typeof dbg.columnNumber === "number" && dbg.columnNumber >= 1) node.col = dbg.columnNumber;
      node.source = "debug-source";
      return node;
    }

    // React ≥19：_debugStack（JSX 创建位点）。_debugTask 只有 run() 没有 name，
    // 对命名与定位均无帮助，跳过。
    const stackLoc = parseDebugStack(fiber._debugStack);
    if (stackLoc !== null) {
      node.rawFile = stackLoc.rawFile;
      node.line = stackLoc.line;
      node.col = stackLoc.col;
      node.source = "debug-stack";
      return node;
    }
    return node;
  }

  // 收集同一层级：可收集组件变成节点并递归其子树；不可收集的（host 元素、
  // fragment、portal、context）不产出节点但下探——组件藏在 DOM 结构之下
  // 依然要能被发现。
  function collectLevel(
    fiber: FiberLike | null,
    out: ComponentNode[],
    visited: Set<FiberLike>,
    budget: { count: number },
  ): ComponentNode[] {
    for (let f: FiberLike | null = fiber; f !== null && budget.count > 0; f = f.sibling) {
      if (visited.has(f)) continue;
      visited.add(f);

      if (isCollectable(f.type) && !INTERNAL_NAMES.has(componentName(f.type))) {
        const node = makeNode(f);
        out.push(node);
        budget.count -= 1;
        node.children = collectLevel(f.child, [], visited, budget);
      } else {
        const nested = collectLevel(f.child, [], visited, budget);
        out.push(...nested);
      }
    }
    return out;
  }

  // DOM 兜底：hook 未收集到 root 时从容器元素的 React 内部 key 上溯
  function findRootsFromDom(): FiberLike[] {
    const roots: FiberLike[] = [];
    const all = document.querySelectorAll("*");
    for (let i = 0; i < all.length && roots.length === 0; i++) {
      const el = all[i] as HTMLElement & Record<string, unknown>;
      for (const key of Object.keys(el)) {
        if (key.startsWith("__reactContainer$")) {
          const container = el[key] as { current?: FiberLike } | undefined;
          if (container && container.current) {
            roots.push(container.current);
          }
          break;
        }
        if (key.startsWith("__reactFiber$")) {
          let f = el[key] as FiberLike | undefined;
          while (f && f.return) {
            f = f.return;
          }
          if (f) {
            roots.push(f);
          }
          break;
        }
      }
    }
    return roots;
  }

  try {
    const budget = { count: maxNodes > 0 ? maxNodes : 2000 };
    const visited = new Set<FiberLike>();
    const w = window as unknown as { __UICA_FIBER_ROOTS__?: unknown[] };
    const rawRoots =
      w.__UICA_FIBER_ROOTS__ && w.__UICA_FIBER_ROOTS__.length > 0
        ? w.__UICA_FIBER_ROOTS__
        : findRootsFromDom();
    const tree: ComponentNode[] = [];
    for (const root of rawRoots) {
      const fiberRoot = root as { current?: FiberLike };
      const current = fiberRoot.current ?? (root as FiberLike);
      collectLevel(current, tree, visited, budget);
    }
    return { ok: true, tree: tree as unknown as Array<Record<string, unknown>> };
  } catch (error) {
    return { ok: false, tree: [], error: error instanceof Error ? error.message : String(error) };
  }
}
