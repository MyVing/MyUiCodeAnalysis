/**
 * 视图层分析测试。
 *
 * 守住四件事：
 *  1. 只收**有语义**的元素，布局噪声（div / span）不进图——否则一个页面几百个节点
 *  2. 元素 id 与事件层一致：同一个 `<button>` 不能变成两个节点
 *  3. 父子关系用 contains 表达，被跳过的 div 不打断归属
 *  4. 定不下来的东西（动态文案、变量 columns）如实标注，不猜
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, describe, expect, it } from "vitest";

import type { IREdge, IRNode } from "@uicodeanalysis/contracts";

import { discoverComponents } from "../src/components.js";
import { analyzeEvents } from "../src/events.js";
import { analyzeHooks } from "../src/hooks.js";
import { buildJsxInventory } from "../src/jsx-inventory.js";
import { createFullProject } from "../src/project.js";
import { analyzeProps } from "../src/props.js";
import { analyzeView, type ViewAnalysisResult } from "../src/view.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

interface Analysis {
  view: ViewAnalysisResult;
  inventory: ReturnType<typeof buildJsxInventory>;
  rootPath: string;
}

function analyze(rootPath: string, extraElementIds?: Set<string>): Analysis {
  const { project } = createFullProject(rootPath);
  const discovery = discoverComponents(rootPath, project);
  const inventory = buildJsxInventory(
    rootPath,
    project,
    discovery.components,
    discovery.declarations,
  );
  const view = analyzeView(extraElementIds ? { inventory, extraElementIds } : { inventory });
  return { view, inventory, rootPath };
}

const tempDirs: string[] = [];

function analyzeSynthetic(files: Record<string, string>, extraElementIds?: Set<string>): Analysis {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-view-"));
  tempDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const absolute = path.join(dir, relative);
    mkdirSync(path.dirname(absolute), { recursive: true });
    writeFileSync(absolute, content, "utf8");
  }
  return analyze(dir, extraElementIds);
}

afterAll(() => {
  for (const dir of tempDirs) rmSync(dir, { recursive: true, force: true });
});

/** 找视图节点；不存在直接失败并列出实际 id，方便定位。 */
function nodeOf(result: ViewAnalysisResult, id: string): IRNode {
  const node = result.elementNodes.find((n) => n.id === id);
  if (!node) {
    const actual = result.elementNodes.map((n) => n.id).join("\n");
    throw new Error(`缺少视图节点 ${id}；实际节点：\n${actual}`);
  }
  return node;
}

function hasNode(result: ViewAnalysisResult, id: string): boolean {
  return result.elementNodes.some((n) => n.id === id);
}

function edgeOf(
  result: ViewAnalysisResult,
  from: string,
  to: string,
): IREdge {
  const edge = result.edges.find((e) => e.kind === "contains" && e.from === from && e.to === to);
  if (!edge) {
    throw new Error(
      `缺少 contains 边 ${from} → ${to}；实际边：\n${result.edges.map((e) => `${e.from} → ${e.to}`).join("\n")}`,
    );
  }
  return edge;
}

describe("封装表单控件字段", () => {
  it("从 registration={register('field')} 提取字段注册事实", () => {
    const result = analyzeSynthetic({
      "src/Form.tsx": `declare function register(name: string): unknown; export function Form() { return <Input label="Title" registration={register('title')} />; }`,
    }).view;

    const input = nodeOf(result, "src/Form.tsx#Form/jsx:Input");
    expect(input.attrs.formField).toMatchObject({
      name: "title",
      quote: "register('title')",
      loc: { line: 1 },
    });
  });

  it("动态 registration 字段不猜测字段名", () => {
    const result = analyzeSynthetic({
      "src/Form.tsx": `declare function register(name: string): unknown; export function Form({ name }: { name: string }) { return <Input registration={register(name)} />; }`,
    }).view;

    expect(nodeOf(result, "src/Form.tsx#Form/jsx:Input").attrs.formField).toBeUndefined();
  });
});

describe("视图层（真实样本仓）", () => {
  const result = analyze(FIXTURE_ROOT).view;

  it("宿主控件与文本标签进视图层，role 区分控件 / 容器 / 文本", () => {
    // UserList 的搜索框
    const input = nodeOf(result, "src/pages/UserList.tsx#UserList/jsx:input");
    expect(input.attrs.viewRole).toBe("control");
    expect(input.attrs.viewText).toBe("搜索用户");
    expect(input.attrs.tag).toBe("input");
    expect(input.attrs.target).toBe("host");

    // UserDetail 的标题与状态值
    const heading = nodeOf(result, "src/pages/UserDetail.tsx#UserDetail/jsx:h2");
    expect(heading.attrs.viewRole).toBe("text");
    expect(heading.attrs.viewText).toBe("用户详情");

    const status = nodeOf(result, "src/pages/UserDetail.tsx#UserDetail/jsx:dd#2");
    expect(status.attrs.viewText).toBe("待审核");
  });

  it("布局元素（div / span）无文案时不进图：图是交互视图，不是 DOM 快照", () => {
    // RowItem 的 <div className="row-item"> 没有文案，不该成节点
    expect(hasNode(result, "src/components/RowItem.tsx#RowItem/jsx:div")).toBe(false);
    // 两个 <span>{name}</span> / <span>{email}</span> 文案是变量，也不进
    expect(result.elementNodes.some((n) => n.attrs.tag === "span")).toBe(false);
  });

  it("业务封装组件靠 columns 字面量进视图层：需求里的『姓名列』能被召回", () => {
    const proTable = nodeOf(result, "src/pages/UserList.tsx#UserList/jsx:ProTable");
    expect(proTable.attrs.viewRole).toBe("container");
    expect(proTable.attrs.columns).toEqual([
      { title: "姓名", dataIndex: "name" },
      { title: "邮箱", dataIndex: "email" },
    ]);
  });

  it("普通组件实例不进视图层：它的语义由 renders 边表达", () => {
    // <TableCard rows={rows} onSelect={handleSelect} /> 没有 columns 也不是通用 UI 名
    expect(hasNode(result, "src/pages/UserList.tsx#UserList/jsx:TableCard/handleSelect")).toBe(false);
  });

  it("动态文案的结构事实保留，文案缺失如实标注 viewTextDynamic", () => {
    // <h1>{title}</h1>
    const heading = nodeOf(result, "src/pages/UserList.tsx#UserList/jsx:h1");
    expect(heading.attrs.viewRole).toBe("text");
    expect(heading.attrs.viewText).toBeUndefined();
    expect(heading.attrs.viewTextDynamic).toBe(true);
  });

  // 这个用例要把 fixture 跑两遍（事件层 + 视图层各建一次 Project），
  // 全量并发跑 12 个测试文件时单核争抢会逼近默认 5s 上限。给足余量，
  // 而不是靠缩减断言让它"看起来快"。
  it("元素 id 与事件层一致：视图层与事件层描述的是同一个节点", () => {
    const { inventory } = analyze(FIXTURE_ROOT);
    const rootPath = FIXTURE_ROOT;
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
    const events = analyzeEvents({
      inventory,
      components: discovery.components,
      knownNodes: [...hooks.hookNodes, ...hooks.stateNodes],
      propsFields: new Map(
        [...props.byComponent].map(([id, info]) => [id, info.fields.map((f) => f.name)]),
      ),
      rootPath,
    });

    // 事件层产出的每个元素 id 都必须在清单里（清单是唯一 id 来源）
    for (const node of events.elementNodes) {
      expect(inventory.byId.get(node.id)).toBeDefined();
    }

    // ProTable 同时被两层收录（事件层因为 onRowClick，视图层因为 columns），
    // 两边 id 必须完全一致，否则图上会出现两个节点
    const eventIds = new Set(events.elementNodes.map((n) => n.id));
    expect(eventIds.has("src/pages/UserList.tsx#UserList/jsx:ProTable")).toBe(true);
    expect(hasNode(result, "src/pages/UserList.tsx#UserList/jsx:ProTable")).toBe(true);
  }, 20000);

  it("contains 边把元素挂到「最近的有语义祖先」，被跳过的 div 不打断归属", () => {
    // <TableCard> 里 <div class="table-card"> 下有 RowItem（组件，不收）
    // 与 CardFooter（组件，名字不认，不收）——所以 TableCard 里没有视图节点
    // 但 UserList 的 <section> 是语义容器，input / h1 都该挂在它下面
    edgeOf(result, "src/pages/UserList.tsx#UserList/jsx:section", "src/pages/UserList.tsx#UserList/jsx:input");
    edgeOf(result, "src/pages/UserList.tsx#UserList/jsx:section", "src/pages/UserList.tsx#UserList/jsx:h1");
  });

  it("顶层元素连到所属组件，而不是成为孤儿", () => {
    // UserDetail 的 <section> 是组件根节点
    edgeOf(result, "src/pages/UserDetail.tsx#UserDetail", "src/pages/UserDetail.tsx#UserDetail/jsx:section");
  });

  it("每个视图节点都带 file / loc 与静态证据，置信度取满", () => {
    expect(result.elementNodes.length).toBeGreaterThan(0);
    for (const node of result.elementNodes) {
      expect(node.kind).toBe("element");
      expect(node.file).toBeDefined();
      expect(node.loc).toBeDefined();
      expect(node.evidence[0]).toMatchObject({ source: "static", confidence: 1 });
      expect(node.attrs.viewRole).toBeDefined();
    }
  });

  it("每条 contains 边的端点都存在，且 from 不是自己", () => {
    const ids = new Set(result.elementNodes.map((n) => n.id));
    const componentIds = new Set([...result.elementNodes.map((n) => n.id.split("/jsx:")[0])]);

    for (const edge of result.edges) {
      expect(edge.kind).toBe("contains");
      expect(edge.from).not.toBe(edge.to);
      expect(ids.has(edge.to)).toBe(true);
      // from 要么是另一个视图节点，要么是所属组件
      expect(ids.has(edge.from) || componentIds.has(edge.from)).toBe(true);
    }
  });
});

describe("视图层的边界（合成项目）", () => {
  it("columns 是变量时告警而不猜测内容", () => {
    const { view } = analyzeSynthetic({
      "src/Dyn.tsx": `
        const columns = buildColumns();
        export function Dyn() {
          return <Table columns={columns} />;
        }
      `,
    });

    // Table 是通用 UI 名，照常收录，但 columns 不写进 attrs
    const table = nodeOf(view, "src/Dyn.tsx#Dyn/jsx:Table");
    expect(table.attrs.columns).toBeUndefined();
    expect(view.warnings.some((w) => w.code === "columns_dynamic")).toBe(true);
  });

  it("columns 里标题是变量时该列被跳过，其余列照常收录", () => {
    const { view } = analyzeSynthetic({
      "src/Partial.tsx": `
        export function Partial({ t }: { t: string }) {
          return <Table columns={[{ title: t, dataIndex: "a" }, { title: "状态", dataIndex: "s" }]} />;
        }
      `,
    });

    const table = nodeOf(view, "src/Partial.tsx#Partial/jsx:Table");
    expect(table.attrs.columns).toEqual([{ title: "状态", dataIndex: "s" }]);
  });

  it("option 无文案时不成节点，有文案时记为文本", () => {
    const { view } = analyzeSynthetic({
      "src/Options.tsx": `
        export function Options() {
          return (
            <select>
              <option value="1" />
              <option value="2">待审核</option>
            </select>
          );
        }
      `,
    });

    const ids = view.elementNodes.map((n) => n.id);
    expect(ids).toContain("src/Options.tsx#Options/jsx:select");
    // 序号按「全部元素」计算（与收录无关），所以第一个 option 是 jsx:option，
    // 第二个带文案的是 jsx:option#2——只收后者
    expect(ids).not.toContain("src/Options.tsx#Options/jsx:option");
    expect(ids).toContain("src/Options.tsx#Options/jsx:option#2");
  });

  it("跨函数边界的元素（map 回调里）连到组件，不连外层 JSX", () => {
    const { view } = analyzeSynthetic({
      "src/Mapped.tsx": `
        export function Mapped({ rows }: { rows: string[] }) {
          return (
            <ul>
              {rows.map((row) => (
                <li key={row}>{row}</li>
              ))}
            </ul>
          );
        }
      `,
    });

    const li = nodeOf(view, "src/Mapped.tsx#Mapped/jsx:li");
    // 父节点是组件：回调里的元素在渲染时才产生，把 <ul> 当父级是捏造
    edgeOf(view, "src/Mapped.tsx#Mapped", li.id);
  });

  it("同一组件内多个同类元素各自成节点，父子按嵌套关系", () => {
    const { view } = analyzeSynthetic({
      "src/Nested.tsx": `
        export function Nested() {
          return (
            <form>
              <label>姓名</label>
              <div>
                <button>保存</button>
              </div>
            </form>
          );
        }
      `,
    });

    const form = "src/Nested.tsx#Nested/jsx:form";
    const label = "src/Nested.tsx#Nested/jsx:label";
    const button = "src/Nested.tsx#Nested/jsx:button";

    edgeOf(view, form, label);
    // 中间的 div 不收录，但 button 仍然属于 form
    edgeOf(view, form, button);
  });

  it("元素 id 不随上方插入注释而漂移", () => {
    const source = (prefix: string) => `
      ${prefix}
      export function Stable() {
        return <form><input placeholder="搜索" /></form>;
      }
    `;

    const before = analyzeSynthetic({ "src/Stable.tsx": source("") }).view;
    const after = analyzeSynthetic({ "src/Stable.tsx": source("// 注释\n// 注释") }).view;

    expect(after.elementNodes.map((n) => n.id)).toEqual(before.elementNodes.map((n) => n.id));
  });

  it("模块顶层 JSX 不归任何组件，不产视图节点", () => {
    const { view } = analyzeSynthetic({
      "src/Top.tsx": `
        export const config = <button>submit</button>;
      `,
    });

    expect(view.elementNodes).toEqual([]);
  });
});

describe("链接元素的目标地址（attrs.href）", () => {
  /**
   * `<a href>` 是需求定位最难捞的一类：地址不是可见文案（不进 ui-text 白名单），
   * 又没有对应的 route 节点（不产 navigates 边）。不单独记下来，
   * 「点击 GitHub 那个链接」就永远定位不到。
   */
  it("外链 <a> 记录字面量 href 并标记 external", () => {
    const { view } = analyzeSynthetic({
      "src/Header.tsx": `
        export function Header() {
          return (
            <nav>
              <a href="https://github.com/naserrasoulii/feature-based-react" target="_blank">GitHub</a>
            </nav>
          );
        }
      `,
    });

    const link = nodeOf(view, "src/Header.tsx#Header/jsx:a");
    expect(link.attrs.href).toBe("https://github.com/naserrasoulii/feature-based-react");
    expect(link.attrs.external).toBe(true);
    expect(link.attrs.viewRole).toBe("link");
    expect(link.attrs.viewText).toBe("GitHub");
  });

  it("站内绝对路径不算外链", () => {
    const { view } = analyzeSynthetic({
      "src/Nav.tsx": `
        export function Nav() {
          return <a href="/products">Products</a>;
        }
      `,
    });

    const link = nodeOf(view, "src/Nav.tsx#Nav/jsx:a");
    expect(link.attrs.href).toBe("/products");
    expect(link.attrs.external).toBe(false);
  });

  it("协议相对地址（//cdn.example.com）按外链处理", () => {
    const { view } = analyzeSynthetic({
      "src/Nav.tsx": `
        export function Nav() {
          return <a href="//example.com/x">External</a>;
        }
      `,
    });

    expect(nodeOf(view, "src/Nav.tsx#Nav/jsx:a").attrs.external).toBe(true);
  });

  it("href 是变量时不记录：定不下来就不猜", () => {
    const { view } = analyzeSynthetic({
      "src/Nav.tsx": `
        export function Nav({ url }: { url: string }) {
          return <a href={url}>Dynamic</a>;
        }
      `,
    });

    const link = nodeOf(view, "src/Nav.tsx#Nav/jsx:a");
    expect(link.attrs.href).toBeUndefined();
    expect(link.attrs.external).toBeUndefined();
  });

  it("非 <a> 元素的 href 不采：只有 a 才是导航链接", () => {
    const { view } = analyzeSynthetic({
      "src/Link.tsx": `
        export function Widget() {
          return <div href="/not-a-link" title="hint">text</div>;
        }
      `,
    });

    const widget = nodeOf(view, "src/Link.tsx#Widget/jsx:div");
    expect(widget.attrs.href).toBeUndefined();
  });
});

describe("视图层为事件层元素补位（extraElementIds）", () => {
  /**
   * `<div onClick>` 布局元素：事件层收（有事件），视图层不收（无文案、无语义）。
   * 它已经是图上的节点，必须挂上 contains 父边，否则在视图树里是孤儿。
   */
  const FILES = {
    "src/Widget.tsx": `
      export function Widget() {
        return (
          <section>
            <div onClick={() => {}} />
            <button>确定</button>
          </section>
        );
      }
    `,
  };

  it("只给已存在的元素补 contains 边，不重复产节点", () => {
    const buttonOnly = analyzeSynthetic(FILES).view;
    const withExtra = analyzeSynthetic(
      FILES,
      new Set(["src/Widget.tsx#Widget/jsx:div"]),
    ).view;

    // 节点集合完全不变：extra 元素由事件层产出
    expect(withExtra.elementNodes.map((n) => n.id).sort()).toEqual(
      buttonOnly.elementNodes.map((n) => n.id).sort(),
    );

    // 但多出一条边，且挂在最近的语义祖先 section 下
    const section = "src/Widget.tsx#Widget/jsx:section";
    const div = "src/Widget.tsx#Widget/jsx:div";
    expect(buttonOnly.edges.some((e) => e.to === div)).toBe(false);
    expect(withExtra.edges.some((e) => e.from === section && e.to === div)).toBe(true);
  });

  it("不在 extraElementIds 里的布局元素仍然不产边", () => {
    const { view } = analyzeSynthetic(FILES);

    expect(view.edges.every((e) => e.to !== "src/Widget.tsx#Widget/jsx:div")).toBe(true);
  });

  it("extra 元素本身也能作为别的元素的父级占位", () => {
    // 子元素挂在 layout 元素下时，父边指向它——即使它不带 viewRole
    const { view } = analyzeSynthetic(
      {
        "src/Nested.tsx": `
          export function Nested() {
            return (
              <div onClick={() => {}}>
                <button>确定</button>
              </div>
            );
          }
        `,
      },
      new Set(["src/Nested.tsx#Nested/jsx:div"]),
    );

    // div 是顶层元素：它自己连到组件
    edgeOf(view, "src/Nested.tsx#Nested", "src/Nested.tsx#Nested/jsx:div");
    // button 的最近语义祖先是 div（虽然在视图层只占位），不是组件
    edgeOf(view, "src/Nested.tsx#Nested/jsx:div", "src/Nested.tsx#Nested/jsx:button");
  });
});
