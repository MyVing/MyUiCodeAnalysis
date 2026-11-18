/**
 * 路由 / 页面节点分析测试。
 *
 * 分三部分：
 *  - 真实样本仓：`src/router.tsx` 的 `createBrowserRouter` 路由表
 *  - Next.js 文件约定样本：`app/` 下的 page 文件、`(group)`、`[id]`、`[[...slug]]`、`_private`
 *  - 合成项目：边界（嵌套、index、非字面量、element 解析不到、自造 Route、重复路径）
 *
 * 合成项目写进临时目录而不是 createSourceFile 注入，因为解析链路依赖真实
 * 文件路径——用内存节点会绕开它本该走的那条路径。
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, describe, expect, it } from "vitest";
import type { IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import { discoverComponents } from "../src/components.js";
import { createFullProject } from "../src/project.js";
import { createResolutionIndex } from "../src/resolver.js";
import { analyzeRoutes } from "../src/routes.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");
const NEXT_FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "next-app-router");

interface RouteResult {
  routeNodes: IRNode[];
  edges: IREdge[];
  warnings: Warning[];
  componentIds: string[];
}

function analyze(rootPath: string, framework?: "react" | "next"): RouteResult {
  const { project } = createFullProject(rootPath);
  const resolution = createResolutionIndex(rootPath, project);
  const discovery = discoverComponents(rootPath, project);
  const routes = analyzeRoutes({
    rootPath,
    project,
    components: discovery.components,
    definitionIndex: discovery.definitionIndex,
    resolution: resolution.index,
    ...(framework ? { framework } : {}),
  });

  return {
    routeNodes: routes.routeNodes,
    edges: routes.edges,
    warnings: [...resolution.warnings, ...discovery.warnings, ...routes.warnings],
    componentIds: discovery.components.map((c) => c.id),
  };
}

const tempDirs: string[] = [];

/** 搭一个合成项目；默认带一个能解析到的页面组件，便于观察路径拼接。 */
function analyzeSynthetic(
  files: Record<string, string>,
  framework?: "react" | "next",
): RouteResult {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-routes-"));
  tempDirs.push(dir);

  const withDefaults: Record<string, string> = {
    "src/Pages.tsx": `
      export function Home() { return <div />; }
      export function UserList() { return <div />; }
      export function UserDetail() { return <div />; }
      export function Layout() { return <div />; }
    `,
    ...files,
  };

  for (const [relative, content] of Object.entries(withDefaults)) {
    const absolute = path.join(dir, relative);
    mkdirSync(path.dirname(absolute), { recursive: true });
    writeFileSync(absolute, content, "utf8");
  }

  return analyze(dir, framework);
}

afterAll(() => {
  for (const dir of tempDirs) {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** 按路径取 route 节点；不存在直接失败，避免断言在 undefined 上静默通过。 */
function routeByPath(result: RouteResult, routePath: string): IRNode {
  const node = result.routeNodes.find((item) => item.name === routePath);
  if (!node) {
    throw new Error(
      `缺少路由 ${routePath}；实际路由：\n` +
        result.routeNodes.map((item) => `  ${item.name} (${item.id})`).join("\n"),
    );
  }
  return node;
}

describe("路由节点（真实样本仓）", () => {
  const result = analyze(FIXTURE_ROOT);

  it("createBrowserRouter 的路由表被识别成 route 节点", () => {
    expect(routeByPath(result, "/users")).toBeDefined();
  });

  it("嵌套的相对子路径拼接成完整 URL", () => {
    const detail = routeByPath(result, "/users/:userId");
    expect(detail.kind).toBe("route");
    expect(detail.file).toBe("src/router.tsx");
    expect(detail.attrs.path).toBe("/users/:userId");
    expect(detail.attrs.framework).toBe("react-router");
    expect(detail.attrs.params).toEqual(["userId"]);
  });

  it("route 连到对应的页面组件", () => {
    const listId = "src/pages/UserList.tsx#UserList";
    const detailId = "src/pages/UserDetail.tsx#UserDetail";
    expect(result.componentIds).toContain(listId);
    expect(result.componentIds).toContain(detailId);

    const toList = result.edges.find(
      (edge) => edge.from.endsWith("#/users") && edge.to === listId,
    );
    expect(toList?.kind).toBe("renders");

    const toDetail = result.edges.find(
      (edge) => edge.from.endsWith("#/users/:userId") && edge.to === detailId,
    );
    expect(toDetail?.kind).toBe("renders");
  });

  it("route 节点是语法事实：置信度 1.0，边是推断：0.9", () => {
    const node = routeByPath(result, "/users");
    expect(node.evidence[0]).toMatchObject({ source: "static", confidence: 1 });

    const edge = result.edges.find((item) => item.from === node.id);
    expect(edge?.evidence[0]?.confidence).toBe(0.9);
    // 边的位置指向路由声明处，不是文件开头
    expect(edge?.evidence[0]?.loc?.line).toBeGreaterThan(1);
  });

  it("route 节点带 file 与 loc，可回溯到 path 字面量", () => {
    const node = routeByPath(result, "/users");
    expect(node.file).toBe("src/router.tsx");
    expect(node.loc?.line).toBeGreaterThan(0);
  });

  it("模块顶层配置里的 JSX 不额外产生 renders 边", () => {
    // router.tsx 里的 <UserList /> 在配置对象里，不归任何组件；
    // routes.ts 产的是 route→页面，起点是 route 节点，不是组件
    for (const edge of result.edges) {
      expect(edge.from).toContain("#/");
    }
  });
});

describe("路由节点（Next.js 文件约定）", () => {
  const result = analyze(NEXT_FIXTURE_ROOT, "next");

  it("app/page.tsx → /", () => {
    expect(routeByPath(result, "/").attrs.framework).toBe("next-app");
  });

  it("目录层级转成 URL 路径", () => {
    expect(routeByPath(result, "/users")).toBeDefined();
    expect(routeByPath(result, "/users/:id").attrs.params).toEqual(["id"]);
  });

  it("分组目录 (marketing) 不参与 URL", () => {
    const about = routeByPath(result, "/about");
    expect(about.file).toBe("app/(marketing)/about/page.tsx");
  });

  it("捕获所有段与可选捕获所有段的参数形态", () => {
    expect(routeByPath(result, "/blog/:slug*").attrs.params).toEqual(["slug"]);
    expect(routeByPath(result, "/settings/:slug*?").attrs.params).toEqual(["slug"]);
  });

  it("_ 前缀私有目录不产生路由", () => {
    expect(result.routeNodes.some((node) => node.file?.includes("_internal"))).toBe(false);
  });

  it("layout.tsx 不是页面，不产生 route 节点", () => {
    expect(result.routeNodes.some((node) => node.file?.includes("layout"))).toBe(false);
  });

  it("同文件多组件时取 default export 作为页面组件", () => {
    const multi = routeByPath(result, "/multi");
    const edge = result.edges.find((item) => item.from === multi.id);
    expect(edge?.to).toBe("app/multi/page.tsx#MultiPage");
    expect(result.edges.some((item) => item.to === "app/multi/page.tsx#PageHeader")).toBe(false);
  });
});

describe("路由节点的边界（合成项目）", () => {
  it("JSX 式路由：BrowserRouter + Routes + Route", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { BrowserRouter, Routes, Route } from "react-router-dom";
        import { Home, UserList } from "./Pages";
        export function App() {
          return (
            <BrowserRouter>
              <Routes>
                <Route path="/" element={<Home />} />
                <Route path="/users" element={<UserList />} />
              </Routes>
            </BrowserRouter>
          );
        }
      `,
    });

    expect(routeByPath(result, "/").id).toContain("#/");
    expect(routeByPath(result, "/users")).toBeDefined();
  });

  it("JSX 式路由：嵌套 Route 拼接父路径", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { BrowserRouter, Routes, Route } from "react-router-dom";
        import { Home, UserList, UserDetail } from "./Pages";
        export function App() {
          return (
            <BrowserRouter>
              <Routes>
                <Route path="/users" element={<UserList />}>
                  <Route index element={<Home />} />
                  <Route path=":id" element={<UserDetail />} />
                </Route>
              </Routes>
            </BrowserRouter>
          );
        }
      `,
    });

    expect(routeByPath(result, "/users/:id")).toBeDefined();
    // index 路由就是父路径本身
    const indexRoutes = result.routeNodes.filter((node) => node.name === "/users");
    expect(indexRoutes.length).toBeGreaterThan(0);
    expect(indexRoutes.some((node) => node.attrs.index === true)).toBe(true);
  });

  it("useRoutes 的路由数组同样被识别", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { useRoutes } from "react-router-dom";
        import { Home, UserList } from "./Pages";
        export function App() {
          const element = useRoutes([
            { path: "/", element: <Home /> },
            { path: "/users", element: <UserList /> },
          ]);
          return element;
        }
      `,
    });

    expect(routeByPath(result, "/users")).toBeDefined();
  });

  it("path 不是字面量时不产节点，并记 route_path_unresolved", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { Home } from "./Pages";
        const dynamicPath = "/dynamic";
        export const router = createBrowserRouter([
          { path: dynamicPath, element: <Home /> },
        ]);
      `,
    });

    expect(result.routeNodes).toEqual([]);
    const warning = result.warnings.find((item) => item.code === "route_path_unresolved");
    expect(warning?.severity).toBe("warning");
    expect(warning?.file).toBe("src/App.tsx");
  });

  it("element 解析不到组件时：产 route 节点但不产边", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { Home, UserList } from "./Pages";
        export const router = createBrowserRouter([
          { path: "/", element: <Home /> },
          { path: "/users", element: <UserList /> },
        ]);
      `,
      // 覆盖默认 Pages：UserList 未导出，因此不在组件列表里
      "src/Pages.tsx": `
        export function Home() { return <div />; }
        function UserList() { return <div />; }
      `,
    });

    const users = routeByPath(result, "/users");
    expect(result.edges.some((edge) => edge.from === users.id)).toBe(false);

    const warning = result.warnings.find((item) => item.code === "route_element_unresolved");
    expect(warning?.message).toContain("/users");
  });

  it("不在 <Routes> 里的自造 Route 组件不被当成路由", () => {
    const result = analyzeSynthetic({
      "src/Breadcrumb.tsx": `
        export function Route({ to, label }: { to: string; label: string }) {
          return <span>{label}</span>;
        }
        export function Breadcrumb() {
          return (
            <nav>
              <Route to="/orders" label="订单" />
            </nav>
          );
        }
      `,
    });

    expect(result.routeNodes).toEqual([]);
    expect(result.warnings).toEqual([]);
  });

  it("同一路径重复声明时两个节点都保留，并记 duplicate_route_path", () => {
    const result = analyzeSynthetic({
      "src/a.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { Home } from "./Pages";
        export const routerA = createBrowserRouter([{ path: "/dup", element: <Home /> }]);
      `,
      "src/b.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { UserList } from "./Pages";
        export const routerB = createBrowserRouter([{ path: "/dup", element: <UserList /> }]);
      `,
    });

    const dupes = result.routeNodes.filter((node) => node.name === "/dup");
    expect(dupes).toHaveLength(2);
    expect(result.warnings.some((item) => item.code === "duplicate_route_path")).toBe(true);
  });

  it("index: true 且无 path 的路由解析为父路径本身", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { Home, UserList } from "./Pages";
        export const router = createBrowserRouter([
          {
            path: "/users",
            element: <UserList />,
            children: [{ index: true, element: <Home /> }],
          },
        ]);
      `,
    });

    const indexNode = result.routeNodes.find(
      (node) => node.name === "/users" && node.attrs.index === true,
    );
    expect(indexNode).toBeDefined();
  });

  it("子路径以 / 开头时视为绝对路径，不拼接父路径", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { Home, UserDetail } from "./Pages";
        export const router = createBrowserRouter([
          {
            path: "/users",
            element: <Home />,
            children: [{ path: "/absolute/:id", element: <UserDetail /> }],
          },
        ]);
      `,
    });

    expect(routeByPath(result, "/absolute/:id")).toBeDefined();
    expect(result.routeNodes.some((node) => node.name === "/users/absolute/:id")).toBe(false);
  });

  it("Component 键指向组件时同样能连边", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { UserList } from "./Pages";
        export const router = createBrowserRouter([
          { path: "/users", Component: UserList },
        ]);
      `,
    });

    const users = routeByPath(result, "/users");
    expect(result.edges.some((edge) => edge.from === users.id && edge.to === "src/Pages.tsx#UserList")).toBe(
      true,
    );
  });

  it("path 里的动态段被提取成 params", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { UserDetail } from "./Pages";
        export const router = createBrowserRouter([
          { path: "/orgs/:orgId/users/:userId", element: <UserDetail /> },
        ]);
      `,
    });

    expect(routeByPath(result, "/orgs/:orgId/users/:userId").attrs.params).toEqual([
      "orgId",
      "userId",
    ]);
  });

  it("spread 合并的路由表被展开（useRoutes([...A, ...B])）", () => {
    const result = analyzeSynthetic({
      "src/Pages.tsx": `
        export function Home() { return <div />; }
        export function UserList() { return <div />; }
        export function OrderList() { return <div />; }
      `,
      "src/features/users/routes.tsx": `
        import { UserList } from "../../Pages";
        export const userRoutes = [{ path: "/users", element: <UserList /> }];
      `,
      "src/features/orders/routes.tsx": `
        import { OrderList } from "../../Pages";
        export const orderRoutes = [{ path: "/orders", element: <OrderList /> }];
      `,
      "src/router.tsx": `
        import { useRoutes } from "react-router-dom";
        import { userRoutes } from "./features/users/routes";
        import { orderRoutes } from "./features/orders/routes";
        export const appRoutes = [...userRoutes, ...orderRoutes];
        export function AppRouter() { return useRoutes(appRoutes); }
      `,
    });

    expect(routeByPath(result, "/users")).toBeDefined();
    expect(routeByPath(result, "/orders")).toBeDefined();
    // 两条路由各自连到自己的组件，说明展开没有串位
    const users = routeByPath(result, "/users");
    expect(result.edges.some((e) => e.from === users.id && e.to === "src/Pages.tsx#UserList")).toBe(
      true,
    );
  });

  it("路由常量经标识符 / as const 断言传入时仍能解析", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { useRoutes } from "react-router-dom";
        import { UserList } from "./Pages";
        const routes = ([
          { path: "/users", element: <UserList /> },
        ] as const) satisfies unknown[];
        export const appRoutes = routes;
        export function AppRouter() { return useRoutes(appRoutes); }
      `,
    });

    expect(routeByPath(result, "/users")).toBeDefined();
  });
});

describe("路由标题提取（差距表 #4）", () => {
  it("对象式路由表的 title 字面量进 attrs.title", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { UserList } from "./Pages";
        export const router = createBrowserRouter([
          { path: "/users", element: <UserList />, title: "用户列表" },
        ]);
      `,
    });

    expect(routeByPath(result, "/users").attrs.title).toBe("用户列表");
  });

  it("react-router 的 handle.title 约定被识别", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { UserList } from "./Pages";
        export const router = createBrowserRouter([
          { path: "/users", element: <UserList />, handle: { title: "订单管理" } },
        ]);
      `,
    });

    expect(routeByPath(result, "/users").attrs.title).toBe("订单管理");
  });

  it("label / name 是菜单配置里常见的标题键，同样收", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { UserList, UserDetail } from "./Pages";
        export const router = createBrowserRouter([
          { path: "/users", element: <UserList />, label: "用户管理" },
          { path: "/users/detail", element: <UserDetail />, name: "用户详情" },
        ]);
      `,
    });

    expect(routeByPath(result, "/users").attrs.title).toBe("用户管理");
    expect(routeByPath(result, "/users/detail").attrs.title).toBe("用户详情");
  });

  it("标题不是字符串字面量时跳过，不编造也不告警", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { UserList } from "./Pages";
        const menu = { users: "用户列表" };
        export const router = createBrowserRouter([
          { path: "/users", element: <UserList />, title: menu.users },
        ]);
      `,
    });

    expect(routeByPath(result, "/users").attrs.title).toBeUndefined();
    expect(
      result.warnings.some((warning) => warning.message.includes("标题")),
    ).toBe(false);
  });

  it("JSX 式 <Route title> 被提取", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { Routes, Route } from "react-router-dom";
        import { UserList } from "./Pages";
        export function AppRouter() {
          return (
            <Routes>
              <Route path="/users" element={<UserList />} title="用户列表" />
            </Routes>
          );
        }
      `,
    });

    expect(routeByPath(result, "/users").attrs.title).toBe("用户列表");
  });

  it("index 声明带标题而主声明没有时，合并后标题补上", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { Layout, UserList } from "./Pages";
        export const router = createBrowserRouter([
          {
            path: "/users",
            element: <Layout />,
            children: [{ path: "index", element: <UserList />, title: "用户列表" }],
          },
        ]);
      `,
    });

    expect(routeByPath(result, "/users").attrs.title).toBe("用户列表");
  });

  it("Next 文件约定产出的路由没有 title 属性", () => {
    const result = analyzeSynthetic(
      { "app/users/page.tsx": "export default function Users() { return null; }" },
      "next",
    );

    expect(routeByPath(result, "/users").attrs.title).toBeUndefined();
  });
});
