/**
 * 事件分析测试。
 *
 * 重点守住四件事：
 *  1. 事件挂在元素上，不是浮空实体 —— 用已有的 `element` 节点承载，
 *     不新增 NodeKind（那要升 schema 版本）
 *  2. `on[A-Z]` 是 React 的语言级约定，不是业务名硬编码；它同时覆盖宿主元素
 *     事件与组件回调 prop
 *  3. 「落到谁」要能分辨三种情形：落到本组件节点、由 props 注入、无法判定。
 *     中间那种不是分析缺漏，标错会让人误以为链路断了
 *  4. 内联处理函数要下探嵌套层级（事件在运行期触发，不受 hook 的顶层规则约束）
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { IREdge, IRNode } from "@uicodeanalysis/contracts";

import { analyzeApi } from "../src/api.js";
import { discoverComponents } from "../src/components.js";
import { analyzeEvents, type EventBinding, type EventsAnalysisResult } from "../src/events.js";
import { analyzeHooks } from "../src/hooks.js";
import { buildJsxInventory } from "../src/jsx-inventory.js";
import { analyzeProps } from "../src/props.js";
import { createFullProject } from "../src/project.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

function analyze(rootPath: string): EventsAnalysisResult {
  const { project } = createFullProject(rootPath);
  const discovery = discoverComponents(rootPath, project);
  const props = analyzeProps({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  const hooks = analyzeHooks({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  const api = analyzeApi({
    rootPath,
    project,
    definitionIndex: discovery.definitionIndex,
  });

  return analyzeEvents({
    inventory: buildJsxInventory(
      rootPath,
      project,
      discovery.components,
      discovery.declarations,
    ),
    components: discovery.components,
    knownNodes: [...hooks.hookNodes, ...hooks.stateNodes, ...api.apiNodes, ...api.fnNodes],
    propsFields: new Map(
      [...props.byComponent].map(([id, info]) => [id, info.fields.map((f) => f.name)]),
    ),
    rootPath,
  });
}

const tempDirs: string[] = [];

function analyzeSynthetic(files: Record<string, string>): EventsAnalysisResult {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-events-"));
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

/** 找元素节点；不存在直接失败。 */
function elementOf(result: EventsAnalysisResult, id: string): IRNode {
  const node = result.elementNodes.find((n) => n.id === id);
  if (!node) {
    const actual = result.elementNodes.map((n) => n.id).join("\n");
    throw new Error(`缺少元素节点 ${id}；实际节点：\n${actual}`);
  }
  return node;
}

/** 取元素节点上的某个事件。 */
function bindingOf(node: IRNode, name: string): EventBinding {
  const events = node.attrs.events as EventBinding[];
  const event = events.find((e) => e.name === name);
  if (!event) throw new Error(`元素 ${node.id} 上没有 ${name}；实际：${events.map((e) => e.name).join(", ")}`);
  return event;
}

function edgeOf(
  result: EventsAnalysisResult,
  kind: IREdge["kind"],
  from: string,
  to: string,
): IREdge {
  const edge = result.edges.find((e) => e.kind === kind && e.from === from && e.to === to);
  if (!edge) {
    throw new Error(
      `缺少 ${kind} 边 ${from} → ${to}；实际边：\n${result.edges.map((e) => `${e.kind} ${e.from} → ${e.to}`).join("\n")}`,
    );
  }
  return edge;
}

describe("事件分析（真实样本仓）", () => {
  let result: EventsAnalysisResult;

  beforeAll(() => {
    result = analyze(FIXTURE_ROOT);
  });

  it("具名回调引用解析到 useCallback 产生的 hook 节点", () => {
    const element = elementOf(result, "src/pages/UserList.tsx#UserList/jsx:TableCard/handleSelect");
    const event = bindingOf(element, "onSelect");

    expect(event.kind).toBe("reference");
    expect(event.handlerName).toBe("handleSelect");
    expect(event.targets).toEqual([
      { id: "src/pages/UserList.tsx#UserList/handleSelect", kind: "hook" },
    ]);
    expect(event.injected).toBe(false);
  });

  it("元素 → 处理节点的边连到真实节点，而不是编一个新节点", () => {
    const edge = edgeOf(
      result,
      "calls",
      "src/pages/UserList.tsx#UserList/jsx:TableCard/handleSelect",
      "src/pages/UserList.tsx#UserList/handleSelect",
    );

    expect(edge.evidence[0]).toMatchObject({ source: "static", confidence: 0.9 });
    // 位置指向那个带 onSelect 的元素
    expect(edge.evidence[0].loc?.line).toBe(52);
  });

  it("内联处理函数里调用的节点同样连边（onRowClick 落到 handleSelect）", () => {
    const element = elementOf(result, "src/pages/UserList.tsx#UserList/jsx:ProTable");
    const event = bindingOf(element, "onRowClick");

    expect(event.kind).toBe("inline-function");
    expect(event.handlerName).toBeNull();
    // 内联形态没有名字，但 targets 照样说清楚它落到谁
    expect(event.targets).toEqual([
      { id: "src/pages/UserList.tsx#UserList/handleSelect", kind: "hook" },
    ]);
  });

  it("props 注入的回调标 injected，而不是显示成链路缺失", () => {
    const element = elementOf(result, "src/components/TableCard.tsx#TableCard/jsx:RowItem/onSelect");
    const event = bindingOf(element, "onClick");

    // RowItem 的 onClick 来自 UserRowProps，逻辑在父组件
    expect(event.kind).toBe("reference");
    expect(event.handlerName).toBe("onSelect");
    expect(event.targets).toEqual([]);
    expect(event.injected).toBe(true);

    // 元素没有任何出边是正确结果：本组件里确实没有这个函数可连，
    // 编一条边出去会让"外部注入"伪装成"已解析"
    const outgoing = result.edges.filter((e) => e.from === element.id);
    expect(outgoing).toEqual([]);
  });

  it("内联体里转手调用 props 回调时也标 injected：逻辑仍在父组件", () => {
    // <div onClick={() => onClick(userId)}>：内联只是转一手，onClick 来自 props
    const element = elementOf(result, "src/components/RowItem.tsx#RowItem/jsx:div");
    const event = bindingOf(element, "onClick");

    expect(event.kind).toBe("inline-function");
    expect(event.targets).toEqual([]);
    expect(event.injected).toBe(true);
  });

  it("宿主元素事件与组件回调 prop 同等对待", () => {
    // <div ... onClick={...}> 在 RowItem 里
    const element = elementOf(result, "src/components/RowItem.tsx#RowItem/jsx:div");
    expect(element.attrs.target).toBe("host");
    expect(bindingOf(element, "onClick").kind).toBe("inline-function");

    // <Table onRow={...}> 是组件标签，target 记成 component
    const table = elementOf(result, "src/components/ProTable.tsx#ProTable/jsx:Table");
    expect(table.attrs.target).toBe("component");
  });

  it("每个元素节点都带 file / loc，且以所属组件 id 为前缀", () => {
    expect(result.elementNodes.length).toBeGreaterThan(0);
    for (const node of result.elementNodes) {
      expect(node.kind).toBe("element");
      expect(node.file).toBeDefined();
      expect(node.loc).toBeDefined();
      expect(node.evidence[0]).toMatchObject({ source: "static", confidence: 1 });
      expect(node.id).toMatch(/^(src\/.+)#[^/]+\/jsx:/);
    }
  });

  it("组件 → 元素的 uses 边存在，元素不会成为孤立点", () => {
    for (const node of result.elementNodes) {
      const ownerId = node.id.slice(0, node.id.indexOf("/jsx:"));
      const edge = result.edges.find((e) => e.kind === "uses" && e.to === node.id);
      expect(edge?.from).toBe(ownerId);
    }
  });

  it("事件边端点都指向真实存在的节点", () => {
    const elementIds = new Set(result.elementNodes.map((n) => n.id));
    for (const edge of result.edges) {
      // from 要么是组件 id、要么是元素 id；这里校验 from 若形如元素则必须存在
      if (edge.from.includes("/jsx:")) expect(elementIds.has(edge.from)).toBe(true);
      expect(edge.evidence.length).toBeGreaterThan(0);
    }
  });
});

describe("事件分析的边界（合成项目）", () => {
  it("非 on[A-Z] 的属性不算事件", () => {
    const result = analyzeSynthetic({
      "src/Attrs.tsx": `
        export function Attrs() {
          return <div className="x" id="y" data-testid="z" onClick={() => {}} />;
        }
      `,
    });

    const element = elementOf(result, "src/Attrs.tsx#Attrs/jsx:div");
    const events = element.attrs.events as EventBinding[];
    expect(events.map((e) => e.name)).toEqual(["onClick"]);
  });

  it("小写开头的 onxxx 属性不是事件（onchange 是拼写错误，不是事件）", () => {
    const result = analyzeSynthetic({
      "src/Lower.tsx": `
        export function Lower() {
          return <div onchange={() => {}} onclick={() => {}} />;
        }
      `,
    });

    // 两个都不是合法事件属性 → 整个元素不产节点
    expect(result.elementNodes).toEqual([]);
  });

  it("内联处理函数下探嵌套层级：arr.map 里的 setter 仍是写入", () => {
    const result = analyzeSynthetic({
      "src/Nested.tsx": `
        declare function useState<T>(initial: T): [T, (next: T) => void];
        export function Nested() {
          const [ids, setIds] = useState<string[]>([]);
          return (
            <button onClick={() => ids.forEach((id) => setIds([id]))}>go</button>
          );
        }
      `,
    });

    const element = elementOf(result, "src/Nested.tsx#Nested/jsx:button");
    const event = bindingOf(element, "onClick");

    // 与 hook 归属相反：事件在运行期触发，嵌套回调里的 setter 照样会执行
    expect(event.targets.map((t) => t.id)).toEqual(["src/Nested.tsx#Nested/ids"]);
    edgeOf(result, "writes", element.id, "src/Nested.tsx#Nested/ids");
  });

  it("调用结果形态的事件不推断指向（onClick={makeHandler(x)}）", () => {
    const result = analyzeSynthetic({
      "src/CallResult.tsx": `
        declare function makeHandler(id: string): () => void;
        export function CallResult() {
          return <div onClick={makeHandler("a")} />;
        }
      `,
    });

    const element = elementOf(result, "src/CallResult.tsx#CallResult/jsx:div");
    const event = bindingOf(element, "onClick");
    expect(event.kind).toBe("other");
    expect(event.handlerName).toBeNull();
    expect(event.targets).toEqual([]);
  });

  it("同名元素在同一组件里多次出现时 id 唯一，不覆盖事件", () => {
    const result = analyzeSynthetic({
      "src/Dup.tsx": `
        export function Dup() {
          return (
            <div>
              <button onClick={() => {}}>a</button>
              <button onClick={() => {}}>b</button>
            </div>
          );
        }
      `,
    });

    // 只有带事件的元素才成节点，所以外层的 <div> 不出现
    const ids = result.elementNodes.map((n) => n.id).sort();
    expect(ids).toEqual(["src/Dup.tsx#Dup/jsx:button", "src/Dup.tsx#Dup/jsx:button#2"]);
  });

  it("模块顶层 JSX 不归任何组件，不产元素节点", () => {
    const result = analyzeSynthetic({
      "src/Top.tsx": `
        export const metadata = <div onClick={() => {}} />;
      `,
    });

    expect(result.elementNodes).toEqual([]);
  });

  it("元素 id 不随上方插入注释而漂移", () => {
    const before = analyzeSynthetic({
      "src/Stable.tsx": `
        export function Stable() {
          return <div onClick={() => {}} />;
        }
      `,
    });
    const after = analyzeSynthetic({
      "src/Stable.tsx": `
        // 新增注释
        // 再来一行
        export function Stable() {
          return <div onClick={() => {}} />;
        }
      `,
    });

    expect(after.elementNodes.map((n) => n.id)).toEqual(before.elementNodes.map((n) => n.id));
  });
});

/**
 * 跨文件处理函数。
 *
 * 这一组守住的是「图上不能把请求归属到错误的调用者」：只按同文件名字表解析时，
 * import 进来的 `saveOrder` 命中不了任何节点，targets 为空，而 `api.ts` 的
 * findOwner 会把请求记到组件名下——页面上就变成"加载时请求"。
 */
describe("跨文件处理函数（符号解析）", () => {
  const SERVICES = `
    export async function saveOrder(id: string) {
      return fetch("/api/orders", { method: "POST" });
    }
  `;

  it("具名引用直接 import 时，calls 边连到跨文件函数节点", () => {
    const result = analyzeSynthetic({
      "src/services/order.ts": SERVICES,
      "src/Save.tsx": `
        import { saveOrder } from "./services/order";

        export function Save({ id }: { id: string }) {
          return <button onClick={saveOrder}>保存</button>;
        }
      `,
    });

    const element = elementOf(result, "src/Save.tsx#Save/jsx:button/saveOrder");
    const event = bindingOf(element, "onClick");

    expect(event.kind).toBe("reference");
    expect(event.handlerName).toBe("saveOrder");
    expect(event.targets).toEqual([{ id: "src/services/order.ts#saveOrder", kind: "fn" }]);
    // 命中真实函数，不是父组件注入
    expect(event.injected).toBe(false);

    const edge = edgeOf(
      result,
      "calls",
      element.id,
      "src/services/order.ts#saveOrder",
    );
    expect(edge.evidence[0]).toMatchObject({ source: "static", confidence: 0.9 });
  });

  it("被事件引用但图上没有的跨文件函数补产 fn 节点", () => {
    // validateForm 不含请求，api.ts 不会注册它；只有"被事件引用"才让它进图
    const result = analyzeSynthetic({
      "src/services/form.ts": `
        export function validateForm(values: Record<string, unknown>) {
          return Object.keys(values).length > 0;
        }
      `,
      "src/Save.tsx": `
        import { validateForm } from "./services/form";

        export function Save() {
          return <button onClick={validateForm}>保存</button>;
        }
      `,
    });

    const node = result.fnNodes.find((n) => n.id === "src/services/form.ts#validateForm");
    expect(node).toBeDefined();
    expect(node?.kind).toBe("fn");
    expect(node?.file).toBe("src/services/form.ts");
    expect(node?.attrs.role).toBe("事件处理函数");

    // 边指向的节点必须在 fnNodes 里存在，否则前端点开目标会找不到
    const element = elementOf(result, "src/Save.tsx#Save/jsx:button/validateForm");
    const event = bindingOf(element, "onClick");
    expect(event.targets).toEqual([
      { id: "src/services/form.ts#validateForm", kind: "fn" },
    ]);
  });

  it("请求链函数已由接口分析产出时，不重复补节点", () => {
    const result = analyzeSynthetic({
      "src/services/order.ts": SERVICES,
      "src/Save.tsx": `
        import { saveOrder } from "./services/order";

        export function Save() {
          return <button onClick={saveOrder}>保存</button>;
        }
      `,
    });

    // saveOrder 含 fetch，analyzeApi 已经注册了它（测试的 analyze 会把
    // api.fnNodes 一并传给事件层）
    expect(
      result.fnNodes.filter((n) => n.id === "src/services/order.ts#saveOrder"),
    ).toEqual([]);
  });

  it("import 别名照样解析到真实声明", () => {
    const result = analyzeSynthetic({
      "src/services/order.ts": SERVICES,
      "src/Save.tsx": `
        import { saveOrder as submit } from "./services/order";

        export function Save({ id }: { id: string }) {
          return <button onClick={submit}>保存</button>;
        }
      `,
    });

    const element = elementOf(result, "src/Save.tsx#Save/jsx:button/submit");
    const event = bindingOf(element, "onClick");

    // 边连到定义处的 id，而不是别名字符串
    expect(event.targets).toEqual([{ id: "src/services/order.ts#saveOrder", kind: "fn" }]);
  });

  it("跨文件解析不到且名字不在 props 里，不标 injected", () => {
    const result = analyzeSynthetic({
      "src/Save.tsx": `
        export function Save() {
          // 没有 import，符号解析不到；也不在 props 里
          return <button onClick={unknownHandler}>保存</button>;
        }
      `,
    });

    const element = elementOf(result, "src/Save.tsx#Save/jsx:button/unknownHandler");
    const event = bindingOf(element, "onClick");

    expect(event.targets).toEqual([]);
    // 这是「无法静态判定」，不是「父组件注入」——两者前端文案不同
    expect(event.injected).toBe(false);
  });

  it("名字表优先于符号解析：组件内的 hook 绑定遮蔽同名跨文件函数", () => {
    const result = analyzeSynthetic({
      "src/services/order.ts": `
        export function handleSelect(userId: string) {
          console.log(userId);
        }
      `,
      "src/Save.tsx": `
        import { useCallback } from "react";
        import { handleSelect as remoteSelect } from "./services/order";

        export function Save() {
          // 组件内绑定的 handleSelect 与跨文件的 remoteSelect 是两回事
          const handleSelect = useCallback((userId: string) => {
            remoteSelect(userId);
          }, []);

          return <button onClick={handleSelect}>选择</button>;
        }
      `,
    });

    const element = elementOf(result, "src/Save.tsx#Save/jsx:button/handleSelect");
    const event = bindingOf(element, "onClick");

    // 连到组件内的 hook 节点，而不是跨文件同名函数
    expect(event.targets).toEqual([{ id: "src/Save.tsx#Save/handleSelect", kind: "hook" }]);
  });
});
