/**
 * 导航边测试。
 *
 * 守住四件事：
 *  1. 只认路由库的 API，项目自造的同名函数（`navigate` / `redirect`）不算
 *  2. `navigate` / `router` 必须回溯到 `useNavigate()` / `useRouter()` 的返回值
 *  3. 路径对上 route 节点才产边：动态路由按段匹配，插值路径降一档置信度
 *  4. 拿不到路径 / 路由表里没有时告警，不编造目标
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterAll, describe, expect, it } from "vitest";

import type { IRNode } from "@uicodeanalysis/contracts";

import { discoverComponents } from "../src/components.js";
import { buildJsxInventory } from "../src/jsx-inventory.js";
import { analyzeNavigation, type NavigationAnalysisResult } from "../src/navigate.js";
import { createFullProject } from "../src/project.js";
import { createResolutionIndex } from "../src/resolver.js";
import { analyzeRoutes } from "../src/routes.js";
import { analyzeView } from "../src/view.js";

const tempDirs: string[] = [];

function setup(files: Record<string, string>) {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-nav-"));
  tempDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const absolute = path.join(dir, relative);
    mkdirSync(path.dirname(absolute), { recursive: true });
    writeFileSync(absolute, content, "utf8");
  }
  return analyze(dir);
}

function analyze(rootPath: string): {
  navigation: NavigationAnalysisResult;
  routeNodes: readonly IRNode[];
} {
  const { project } = createFullProject(rootPath);
  const resolution = createResolutionIndex(rootPath, project);
  const discovery = discoverComponents(rootPath, project);
  const inventory = buildJsxInventory(
    rootPath,
    project,
    discovery.components,
    discovery.declarations,
  );
  const view = analyzeView({ inventory });
  const routes = analyzeRoutes({
    rootPath,
    project,
    components: discovery.components,
    definitionIndex: discovery.definitionIndex,
    resolution: resolution.index,
  });

  const navigation = analyzeNavigation({
    rootPath,
    inventory,
    elementNodeIds: new Set(view.elementNodes.map((n) => n.id)),
    routeNodes: routes.routeNodes,
    components: discovery.components,
    declarations: discovery.declarations,
  });

  return { navigation, routeNodes: routes.routeNodes };
}

afterAll(() => {
  for (const dir of tempDirs) rmSync(dir, { recursive: true, force: true });
});

/** 一个最小可用的路由表外壳，每个用例都从这里出发。 */
const ROUTES = `
  import { createBrowserRouter } from "react-router-dom";
  export const router = createBrowserRouter([
    { path: "/users", element: <div /> },
    { path: "/users/:userId", element: <div /> },
    { path: "/orders/:orderNo/detail", element: <div /> },
  ]);
`;

/** 包一层：把用例里的组件代码与路由表拼在一起。 */
function withRoutes(componentFiles: Record<string, string>): Record<string, string> {
  return { "src/router.tsx": ROUTES, ...componentFiles };
}

describe("导航边（合成项目）", () => {
  it("useNavigate 的 navigate 调用产出 navigates 边，边从组件出发", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          import { useNavigate } from "react-router-dom";
          export function Page() {
            const navigate = useNavigate();
            return <button onClick={() => navigate("/users")}>去列表</button>;
          }
        `,
      }),
    );

    const edge = navigation.edges.find((e) => e.from === "src/Page.tsx#Page");
    expect(edge?.kind).toBe("navigates");
    expect(edge?.to).toBe("src/router.tsx#/users");
    expect(edge?.evidence[0]).toMatchObject({ source: "static", confidence: 0.9 });
  });

  it("项目自造的 navigate 函数不算导航（只看名字会误判）", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          function navigate(target: string) { window.location.href = target; }
          export function Page() {
            return <button onClick={() => navigate("/users")}>go</button>;
          }
        `,
      }),
    );

    expect(navigation.edges).toEqual([]);
  });

  it("useNavigate 未被调用、只是同名变量时不算", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          const navigate = (target: string) => console.log(target);
          export function Page() {
            return <button onClick={() => navigate("/users")}>go</button>;
          }
        `,
      }),
    );

    expect(navigation.edges).toEqual([]);
  });

  it("<Link to> 产边，起点是这个链接元素（视图层已收录）", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          import { Link } from "react-router-dom";
          export function Page() {
            return <Link to="/users">用户列表</Link>;
          }
        `,
      }),
    );

    const edge = navigation.edges[0];
    expect(edge?.from).toBe("src/Page.tsx#Page/jsx:Link");
    expect(edge?.to).toBe("src/router.tsx#/users");
    // 视图层把 Link 记成 link 角色，所以它确实在元素清单里
    expect(navigation.warnings.some((w) => w.code === "navigate_source_not_in_view")).toBe(false);
  });

  it("<Navigate to> 不渲染 UI，边从所属组件出发", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Guard.tsx": `
          import { Navigate } from "react-router-dom";
          export function Guard() {
            return <Navigate to="/users" replace />;
          }
        `,
      }),
    );

    const edge = navigation.edges[0];
    expect(edge?.from).toBe("src/Guard.tsx#Guard");
    expect(edge?.to).toBe("src/router.tsx#/users");
  });

  it("动态路由按段匹配：/users/42 命中 /users/:userId", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          import { useNavigate } from "react-router-dom";
          export function Page() {
            const navigate = useNavigate();
            return <button onClick={() => navigate("/users/42")} />;
          }
        `,
      }),
    );

    expect(navigation.edges[0]?.to).toBe("src/router.tsx#/users/:userId");
  });

  it("多段动态路由同样按段匹配", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          import { useNavigate } from "react-router-dom";
          export function Page() {
            const navigate = useNavigate();
            return <button onClick={() => navigate("/orders/A-1/detail")} />;
          }
        `,
      }),
    );

    expect(navigation.edges[0]?.to).toBe("src/router.tsx#/orders/:orderNo/detail");
  });

  it("模板插值路径按「任意段」匹配，置信度降一档", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          import { useNavigate } from "react-router-dom";
          export function Page({ id }: { id: string }) {
            const navigate = useNavigate();
            return <button onClick={() => navigate(\`/users/\${id}\`)} />;
          }
        `,
      }),
    );

    const edge = navigation.edges[0];
    expect(edge?.to).toBe("src/router.tsx#/users/:userId");
    expect(edge?.evidence[0]?.confidence).toBe(0.8);
  });

  it("router.push / router.replace 都算（Next.js 形态）", () => {
    const result = setup({
      "src/router.tsx": ROUTES,
      "src/Page.tsx": `
        import { useRouter } from "next/navigation";
        export function Page() {
          const router = useRouter();
          return (
            <button
              onClick={() => {
                router.push("/users");
                router.replace("/users");
              }}
            />
          );
        }
      `,
    });

    // 同一对 (组件, 路由) 合并成一条边，证据两条
    const edges = result.navigation.edges.filter((e) => e.from === "src/Page.tsx#Page");
    expect(edges).toHaveLength(1);
    expect(edges[0]?.to).toBe("src/router.tsx#/users");
    expect(edges[0]?.evidence).toHaveLength(2);
  });

  it("router.push 的其他方法（如 back）不是导航", () => {
    const { navigation } = setup({
      "src/router.tsx": ROUTES,
      "src/Page.tsx": `
        import { useRouter } from "next/navigation";
        export function Page() {
          const router = useRouter();
          return <button onClick={() => router.back()} />;
        }
      `,
    });

    expect(navigation.edges).toEqual([]);
  });

  it("redirect 来自路由库时产出边，且边从所在函数的组件出发", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Actions.tsx": `
          import { redirect } from "react-router-dom";
          export function Actions() {
            const run = () => redirect("/users");
            return <button onClick={run}>go</button>;
          }
        `,
      }),
    );

    expect(navigation.edges[0]?.from).toBe("src/Actions.tsx#Actions");
    expect(navigation.edges[0]?.to).toBe("src/router.tsx#/users");
  });

  it("路径是变量时告警 navigate_target_unresolved，不猜目标", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          import { useNavigate } from "react-router-dom";
          export function Page({ target }: { target: string }) {
            const navigate = useNavigate();
            return <button onClick={() => navigate(target)} />;
          }
        `,
      }),
    );

    expect(navigation.edges).toEqual([]);
    expect(navigation.warnings.some((w) => w.code === "navigate_target_unresolved")).toBe(true);
  });

  it("路径确定但路由表里没有时告警 navigate_route_missing，不编造边", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          import { useNavigate } from "react-router-dom";
          export function Page() {
            const navigate = useNavigate();
            return <button onClick={() => navigate("/not-declared")} />;
          }
        `,
      }),
    );

    expect(navigation.edges).toEqual([]);
    expect(navigation.warnings.some((w) => w.code === "navigate_route_missing")).toBe(true);
  });

  it("对象形态的 to={{ pathname }} 取 pathname", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          import { Link } from "react-router-dom";
          export function Page() {
            return <Link to={{ pathname: "/users", search: "?a=1" }}>go</Link>;
          }
        `,
      }),
    );

    expect(navigation.edges[0]?.to).toBe("src/router.tsx#/users");
  });

  it("Next.js 的 <Link href> 同样识别", () => {
    const { navigation } = setup({
      "src/router.tsx": ROUTES,
      "src/Page.tsx": `
        import Link from "next/link";
        export function Page() {
          return <Link href="/users">go</Link>;
        }
      `,
    });

    expect(navigation.edges[0]?.to).toBe("src/router.tsx#/users");
  });

  it("to={ROUTES.HOME} 沿符号声明取到常量值（含跨模块与 as const）", () => {
    const { navigation } = setup(
      withRoutes({
        "src/shared/constants.ts": `
          export const ROUTES = { HOME: "/users", DETAIL: "/users/:userId" } as const;
        `,
        "src/Page.tsx": `
          import { Link } from "react-router-dom";
          import { ROUTES } from "./shared/constants";
          export function Page() {
            return (
              <div>
                <Link to={ROUTES.HOME}>列表</Link>
                <Link to={ROUTES.DETAIL}>详情</Link>
              </div>
            );
          }
        `,
      }),
    );

    expect(navigation.warnings.some((w) => w.code === "navigate_target_unresolved")).toBe(false);
    const targets = navigation.edges.map((e) => e.to).sort();
    expect(targets).toEqual(["src/router.tsx#/users", "src/router.tsx#/users/:userId"]);
  });

  it("间接别名的常量（const HOME = ROUTES.HOME）同样能回溯", () => {
    const { navigation } = setup(
      withRoutes({
        "src/shared/constants.ts": `export const ROUTES = { HOME: "/users" } as const;`,
        "src/Page.tsx": `
          import { useNavigate } from "react-router-dom";
          import { ROUTES } from "./shared/constants";
          const HOME = ROUTES.HOME;
          export function Page() {
            const navigate = useNavigate();
            return <button onClick={() => navigate(HOME)} />;
          }
        `,
      }),
    );

    expect(navigation.edges[0]?.to).toBe("src/router.tsx#/users");
  });

  it("常量取值不是路径时仍告警，不把任意常量当路径", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Page.tsx": `
          import { Link } from "react-router-dom";
          const target = someTarget();
          export function Page() {
            return <Link to={target}>go</Link>;
          }
          declare function someTarget(): string;
        `,
      }),
    );

    expect(navigation.edges).toEqual([]);
    expect(navigation.warnings.some((w) => w.code === "navigate_target_unresolved")).toBe(true);
  });

  it("模块顶层的 navigate 调用不属于任何组件，不产边", () => {
    const { navigation } = setup(
      withRoutes({
        "src/Top.tsx": `
          import { redirect } from "react-router-dom";
          export const loader = () => redirect("/users");
        `,
      }),
    );

    // 没有组件归属：边无处可指，宁可不连
    expect(navigation.edges).toEqual([]);
  });
});
