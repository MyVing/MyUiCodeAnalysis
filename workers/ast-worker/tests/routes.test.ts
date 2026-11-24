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

  it("lazy 动态导入的 default 页面组件生成 renders 边", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        const convert = (module: unknown) => module;
        export const router = createBrowserRouter([
          {
            path: "/app/discussions",
            lazy: () => import("./pages/Discussions").then(convert),
          },
        ]);
      `,
      "src/pages/Discussions.tsx": `
        export default function Discussions() { return <div>Discussions</div>; }
      `,
    });

    const route = routeByPath(result, "/app/discussions");
    expect(route.attrs.pageComponentId).toBe("src/pages/Discussions.tsx#Discussions");
    expect(result.edges.some(
      (edge) => edge.from === route.id && edge.to === "src/pages/Discussions.tsx#Discussions" && edge.kind === "renders",
    )).toBe(true);
  });

  it("lazy 动态导入无法解析时只保留路由并告警", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        export const router = createBrowserRouter([
          { path: "/missing", lazy: () => import("./pages/Missing") },
        ]);
      `,
    });

    const route = routeByPath(result, "/missing");
    expect(result.edges.some((edge) => edge.from === route.id)).toBe(false);
    expect(result.warnings.some(
      (warning) => warning.code === "route_element_unresolved" && warning.message.includes("/missing"),
    )).toBe(true);
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

describe("layout / page 区分（差距表 #5）", () => {
  it("父路由组件含 <Outlet> 时标为 layout，index 子路由是页面", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter, Outlet } from "react-router-dom";
        import { Home, UserDetail } from "./Pages";
        export function UsersLayout() {
          return (
            <div>
              <Outlet />
            </div>
          );
        }
        export const router = createBrowserRouter([
          {
            path: "/users",
            element: <UsersLayout />,
            children: [
              { index: true, element: <Home /> },
              { path: ":id", element: <UserDetail /> },
            ],
          },
        ]);
      `,
    });

    const users = routeByPath(result, "/users");
    // index 路由与父路由撞键合并：pageComponentId 指向 index 的页面
    expect(users.attrs.pageComponentId).toBe("src/Pages.tsx#Home");
    expect(users.attrs.layoutComponentIds).toEqual(["src/router.tsx#UsersLayout"]);
    expect(users.attrs.index).toBe(true);

    // layout 与 page 的 renders 边都在
    expect(result.edges.some((edge) => edge.from === users.id && edge.to === "src/router.tsx#UsersLayout")).toBe(true);
    expect(result.edges.some((edge) => edge.from === users.id && edge.to === "src/Pages.tsx#Home")).toBe(true);

    // 子路由是叶子：组件是页面，不标 layout
    const detail = routeByPath(result, "/users/:id");
    expect(detail.attrs.pageComponentId).toBe("src/Pages.tsx#UserDetail");
    expect(detail.attrs.layoutComponentIds).toBeUndefined();
  });

  it("内联 JSX 里的 <Outlet> 同样被识别", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter, Outlet } from "react-router-dom";
        import { Layout, UserList } from "./Pages";
        export const router = createBrowserRouter([
          {
            path: "/users",
            element: <Layout><Outlet /></Layout>,
            children: [{ index: true, element: <UserList /> }],
          },
        ]);
      `,
    });

    expect(routeByPath(result, "/users").attrs.layoutComponentIds).toEqual([
      "src/Pages.tsx#Layout",
    ]);
  });

  it("有 children 但组件不含 <Outlet> 时，按嵌套路由层级标为 layout", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { Home, UserList, UserDetail } from "./Pages";
        export const router = createBrowserRouter([
          {
            path: "/users",
            element: <UserList />,
            children: [{ path: ":id", element: <UserDetail /> }],
          },
          { path: "/fallback", element: <Home /> },
        ]);
      `,
    });

    // fixture 的 UserList 没有 Outlet，但它是带 children 的父路由
    expect(routeByPath(result, "/users").attrs.layoutComponentIds).toEqual([
      "src/Pages.tsx#UserList",
    ]);
    // 叶子路由不受影响
    expect(routeByPath(result, "/fallback").attrs.pageComponentId).toBe("src/Pages.tsx#Home");
    expect(routeByPath(result, "/fallback").attrs.layoutComponentIds).toBeUndefined();
  });

  it("同一路径解析到两个页面组件时记 route_page_ambiguous，不写 pageComponentId", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { Home, UserList } from "./Pages";
        export const router = createBrowserRouter([
          { path: "/dup", element: <Home /> },
          { path: "/dup", element: <UserList /> },
        ]);
      `,
    });

    const dup = routeByPath(result, "/dup");
    expect(dup.attrs.pageComponentId).toBeUndefined();
    const warning = result.warnings.find((item) => item.code === "route_page_ambiguous");
    expect(warning?.severity).toBe("warning");
    expect(warning?.message).toContain("/dup");
    // 两个候选的边都保留
    expect(result.edges.filter((edge) => edge.from === dup.id)).toHaveLength(2);
  });

  it("Next：layout.tsx 按目录前缀挂到子树内的页面路由", () => {
    const result = analyzeSynthetic(
      {
        "app/layout.tsx": "export default function RootLayout() { return <div />; }",
        "app/users/layout.tsx": "export default function UsersLayout() { return <div />; }",
        "app/users/page.tsx": "export default function Users() { return <div />; }",
        "app/about/page.tsx": "export default function About() { return <div />; }",
      },
      "next",
    );

    const users = routeByPath(result, "/users");
    // 根 layout + 同目录 layout 都包住 /users（attrs 内按字母序，保证可复现）
    expect(users.attrs.layoutComponentIds).toEqual([
      "app/layout.tsx#RootLayout",
      "app/users/layout.tsx#UsersLayout",
    ]);
    expect(users.attrs.pageComponentId).toBe("app/users/page.tsx#Users");

    // 只命中根 layout
    const about = routeByPath(result, "/about");
    expect(about.attrs.layoutComponentIds).toEqual(["app/layout.tsx#RootLayout"]);

    // layout 的 renders 边也产出
    expect(result.edges.some((edge) => edge.from === users.id && edge.to === "app/layout.tsx#RootLayout")).toBe(
      true,
    );
  });
});

describe("Docusaurus 文件路由", () => {
  it("src/pages 的 index / 嵌套目录 / 动态段，以及 docs 的 md 页面", () => {
    const result = analyzeSynthetic({
      "website/docusaurus.config.ts": "export default {};",
      "website/src/pages/index.tsx":
        "export default function Home() { return <div />; }",
      "website/src/pages/guide/index.tsx":
        "export default function Guide() { return <div />; }",
      "website/src/pages/users/[id].tsx":
        "export default function UserDetail() { return <div />; }",
      "website/src/pages/types.d.ts": "export type Page = true;\n",
      "docs/intro.md": "# Intro\n",
      "docs/guide/getting-started.md": "# Getting started\n",
      "docs/guide/index.md": "# Guide\n",
      "docs/api/[id].md": "# API item\n",
    });

    const home = routeByPath(result, "/");
    expect(home.attrs.framework).toBe("docusaurus");
    expect(home.attrs.pageComponentId).toBe("website/src/pages/index.tsx#Home");
    expect(result.edges.some((edge) => edge.from === home.id && edge.to === "website/src/pages/index.tsx#Home")).toBe(
      true,
    );

    expect(routeByPath(result, "/guide").attrs.pageComponentId).toBe(
      "website/src/pages/guide/index.tsx#Guide",
    );
    expect(routeByPath(result, "/users/:id").attrs.params).toEqual(["id"]);
    expect(routeByPath(result, "/docs/api/:id")).toBeDefined();

    // md 页面只建 route，不造组件、不连边，也不报 route_element_unresolved
    const docsIntro = routeByPath(result, "/docs/intro");
    expect(docsIntro.attrs.pageComponentId).toBeUndefined();
    expect(result.edges.some((edge) => edge.from === docsIntro.id)).toBe(false);
    expect(
      result.warnings.some((warning) => warning.code === "route_element_unresolved"),
    ).toBe(false);

    expect(routeByPath(result, "/docs/guide")).toBeDefined();
    expect(routeByPath(result, "/docs/guide/getting-started")).toBeDefined();
    expect(result.routeNodes.some((node) => node.name === "/types.d")).toBe(false);
  });

  it("显式路由优先：同路径的文件路由跳过并记 file_route_shadowed", () => {
    const result = analyzeSynthetic({
      "src/router.tsx": `
        import { createBrowserRouter } from "react-router-dom";
        import { UserList } from "./Pages";
        export const router = createBrowserRouter([{ path: "/users", element: <UserList /> }]);
      `,
      "website/docusaurus.config.ts": "export default {};",
      "website/src/pages/users.tsx": "export default function Page() { return <div />; }",
    });

    const users = result.routeNodes.filter((node) => node.name === "/users");
    expect(users).toHaveLength(1);
    expect(users[0]?.attrs.framework).toBe("react-router");
    expect(result.warnings.some((warning) => warning.code === "file_route_shadowed")).toBe(true);
  });

  it("两个文件映射到同一路径时都保留，并记 duplicate_route_path", () => {
    const result = analyzeSynthetic({
      "website/docusaurus.config.ts": "export default {};",
      "website/src/pages/foo.tsx": "export default function Foo() { return <div />; }",
      "website/src/pages/foo/index.tsx":
        "export default function FooIndex() { return <div />; }",
    });

    expect(result.routeNodes.filter((node) => node.name === "/foo")).toHaveLength(2);
    expect(
      result.warnings.some((warning) => warning.code === "duplicate_route_path"),
    ).toBe(true);
  });
});

describe("Routes 子树内的自定义递归渲染（通道三）", () => {
  it("数据数组经 useCallback 递归渲染后被识别为 route 节点", () => {
    const result = analyzeSynthetic({
      "src/routes.tsx": `
        import { Home, UserList } from "./Pages";
        export const routes = [
          { path: "/", element: <Home /> },
          { path: "/users", element: <UserList /> },
        ];
      `,
      "src/App.tsx": `
        import { useCallback } from "react";
        import { Routes, Route, Navigate } from "react-router-dom";
        import { routes } from "./routes";
        export function App() {
          const generateRouter = useCallback((items: any[]) => {
            return items.map((item) => (
              <Route key={item.path} path={item.path} element={item.element} />
            ));
          }, []);
          return (
            <Routes>
              <Route path="login" element={<Navigate to="/404" replace />} />
              {generateRouter(routes)}
            </Routes>
          );
        }
      `,
    });

    // 这两个节点只能来自数据数组；直接写在 <Routes> 下的 route 只有 /login
    expect(routeByPath(result, "/").id).toContain("#/");
    expect(routeByPath(result, "/users")).toBeDefined();
    expect(result.warnings.some((warning) => warning.code === "route_renderer_unresolved")).toBe(false);
  });

  it("多层 children 递归拼接完整路径", () => {
    const result = analyzeSynthetic({
      "src/routes.tsx": `
        import { Home, UserList, UserDetail, Layout } from "./Pages";
        export const routes = [
          {
            path: "/projects",
            element: <Layout />,
            children: [
              {
                path: ":id",
                element: <UserDetail />,
                children: [{ path: "settings", element: <Home /> }],
              },
            ],
          },
        ];
      `,
      "src/App.tsx": `
        import { Routes, Route } from "react-router-dom";
        import { routes } from "./routes";
        function generateRouter(items: any[], parentPath?: string) {
          return items.map((item) => (
            <Route key={item.path} path={item.path} element={item.element} />
          ));
        }
        export function App() {
          return <Routes>{generateRouter(routes)}</Routes>;
        }
      `,
    });

    expect(routeByPath(result, "/projects").attrs.index).toBe(undefined);
    expect(routeByPath(result, "/projects/:id")).toBeDefined();
    expect(routeByPath(result, "/projects/:id/settings")).toBeDefined();
  });

  it("普通数据数组没有被 Routes 子树消费时不产 route 节点", () => {
    const result = analyzeSynthetic({
      "src/menu.tsx": `
        import { Home } from "./Pages";
        export const menu = [{ path: "/menu", element: <Home /> }];
      `,
      "src/App.tsx": `
        import { Routes, Route, Navigate } from "react-router-dom";
        import { menu } from "./menu";
        function renderMenu(items: any[]) {
          return items.map((item) => <Route key={item.path} path={item.path} element={item.element} />);
        }
        export function App() {
          return (
            <div>
              <nav>{renderMenu(menu)}</nav>
              <Routes>
                <Route path="login" element={<Navigate to="/404" replace />} />
              </Routes>
            </div>
          );
        }
      `,
    });

    expect(result.routeNodes.some((node) => node.name === "/menu")).toBe(false);
    // 不在 Routes 里的调用不算「识别失败」，不该产生告警
    expect(result.warnings.some((warning) => warning.code === "route_renderer_unresolved")).toBe(false);
  });

  it("循环引用的数组不会无限展开", () => {
    const result = analyzeSynthetic({
      "src/routes.tsx": `
        import { Home, UserList } from "./Pages";
        export const a: any[] = [{ path: "/a", element: <Home /> }, ...b];
        export const b: any[] = [{ path: "/b", element: <UserList /> }, ...a];
      `,
      "src/App.tsx": `
        import { Routes } from "react-router-dom";
        import { a } from "./routes";
        function generateRouter(items: any[]) {
          return items.map((item) => <Route key={item.path} path={item.path} element={item.element} />);
        }
        export function App() {
          return <Routes>{generateRouter(a)}</Routes>;
        }
      `,
    });

    expect(result.routeNodes.filter((node) => node.name === "/a")).toHaveLength(1);
    expect(result.routeNodes.filter((node) => node.name === "/b")).toHaveLength(1);
  });

  it("渲染函数不接参数、直接读模块级数组时也能识别", () => {
    const result = analyzeSynthetic({
      "src/routes.tsx": `
        import { Home } from "./Pages";
        export const routes = [{ path: "/home", element: <Home /> }];
      `,
      "src/App.tsx": `
        import { Routes, Route } from "react-router-dom";
        import { routes } from "./routes";
        function generateRouter() {
          return routes.map((item) => (
            <Route key={item.path} path={item.path} element={item.element} />
          ));
        }
        export function App() {
          return <Routes>{generateRouter()}</Routes>;
        }
      `,
    });

    expect(routeByPath(result, "/home")).toBeDefined();
  });

  it("渲染函数的数据源无法静态求值时记 route_renderer_unresolved", () => {
    const result = analyzeSynthetic({
      "src/App.tsx": `
        import { Routes, Route } from "react-router-dom";
        function generateRouter(items: any[]) {
          return items.map((item) => (
            <Route key={item.path} path={item.path} element={item.element} />
          ));
        }
        export function App(props: { items: any[] }) {
          return <Routes>{generateRouter(props.items)}</Routes>;
        }
      `,
    });

    expect(result.routeNodes).toHaveLength(0);
    expect(result.warnings.some((warning) => warning.code === "route_renderer_unresolved")).toBe(true);
  });
});

describe("对象式路由的 component 键与 lazy 局部变量", () => {
  const appShell = `
    import { Routes, Route } from "react-router-dom";
    import { routes } from "./routes";
    function generateRouter(items: any[]) {
      return items.map((item) => <Route key={item.path} path={item.path} element={item.element} />);
    }
    export function App() {
      return <Routes>{generateRouter(routes)}</Routes>;
    }
  `;

  it("小写 component 指向直接导入的组件时能连上", () => {
    const result = analyzeSynthetic({
      "src/Pages/HomePage.tsx": "export default function HomePage() { return <div />; }",
      "src/routes.tsx": `
        import HomePage from "./Pages/HomePage";
        export const routes = [{ path: "/home", component: HomePage }];
      `,
      "src/App.tsx": appShell,
    });

    const route = routeByPath(result, "/home");
    expect(String(route.attrs.pageComponentId)).toContain("HomePage");
    expect(result.warnings.some((warning) => warning.code === "route_element_unresolved")).toBe(false);
  });

  it("大写 Component 的既有行为不回归", () => {
    const result = analyzeSynthetic({
      "src/Pages/HomePage.tsx": "export default function HomePage() { return <div />; }",
      "src/routes.tsx": `
        import HomePage from "./Pages/HomePage";
        export const routes = [{ path: "/upper", Component: HomePage }];
      `,
      "src/App.tsx": appShell,
    });

    expect(String(routeByPath(result, "/upper").attrs.pageComponentId)).toContain("HomePage");
  });

  it("component 指向 lazy 局部变量时解析到目标模块的 default export", () => {
    const result = analyzeSynthetic({
      "src/Pages/LazyHome.tsx": "export default function LazyHome() { return <div />; }",
      "src/routes.tsx": `
        import { lazy } from "react";
        const Home = lazy(() => import("./Pages/LazyHome"));
        export const routes = [{ path: "/lazy", component: Home }];
      `,
      "src/App.tsx": appShell,
    });

    const route = routeByPath(result, "/lazy");
    expect(String(route.attrs.pageComponentId)).toContain("LazyHome");
    expect(result.warnings.some((warning) => warning.code === "route_element_unresolved")).toBe(false);
  });

  it("简写属性 { component } 也能穿透 lazy 局部变量", () => {
    const result = analyzeSynthetic({
      "src/Pages/LazyHome.tsx": "export default function LazyHome() { return <div />; }",
      "src/routes.tsx": `
        import { lazy } from "react";
        const component = lazy(() => import("./Pages/LazyHome"));
        export const routes = [{ path: "/short", component }];
      `,
      "src/App.tsx": appShell,
    });

    expect(String(routeByPath(result, "/short").attrs.pageComponentId)).toContain("LazyHome");
  });

  it("lazy 目标没有 default export 时保留节点并维持告警", () => {
    const result = analyzeSynthetic({
      "src/Pages/NamedOnly.tsx": "export function NamedOnly() { return <div />; }",
      "src/routes.tsx": `
        import { lazy } from "react";
        const Home = lazy(() => import("./Pages/NamedOnly"));
        export const routes = [{ path: "/named", component: Home }];
      `,
      "src/App.tsx": appShell,
    });

    const route = routeByPath(result, "/named");
    expect(route.attrs.pageComponentId).toBeUndefined();
    expect(result.warnings.some((warning) => warning.code === "route_element_unresolved")).toBe(true);
  });

  it("普通本地函数体内的动态 import 不会被当成 lazy 页面来源", () => {
    const result = analyzeSynthetic({
      "src/routes.tsx": `
        function Login() {
          import("./chunk");
          return <div />;
        }
        export const routes = [{ path: "/login", component: Login }];
      `,
      "src/App.tsx": appShell,
    });

    const route = routeByPath(result, "/login");
    expect(route.attrs.pageComponentId).toBeUndefined();
    expect(result.warnings.some((warning) => warning.code === "route_element_unresolved")).toBe(true);
  });
});

