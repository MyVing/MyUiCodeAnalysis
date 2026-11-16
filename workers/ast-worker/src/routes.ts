/**
 * 路由 / 页面节点分析：把「哪个 URL 由哪个组件提供」变成图里的 `route` 节点。
 *
 * ## 为什么需要它
 *
 * 需求说的是「用户列表页」「订单详情页」，代码里对应的是 `/users` 与 `/users/:id`。
 * 没有 route 节点时，`src/pages/UserList.tsx#UserList` 只是一个普通组件，
 * "它是页面"这件事只能靠目录名暗示——那不是语法事实，检索时也无从确认。
 *
 * `NodeKind.route` 从 schema 1.0 就在枚举里，本模块是它的第一个产出者，
 * 因此**不升 schema 版本**。
 *
 * ## 两条识别路径
 *
 * | 形态 | 依据 |
 * | --- | --- |
 * | React Router | 路由工厂函数的参数（`createBrowserRouter([...])` / `useRoutes([...])`）或 `<Routes>` 内的 `<Route>` |
 * | Next.js 文件约定 | `app/**\/page.tsx`、`pages/**\/*.tsx` 的目录结构 |
 *
 * 两条路都只认**框架的语言级约定**，不出现任何业务名判断：`createBrowserRouter`
 * 是 react-router 的 API，`page.tsx` 是 Next 的约定，对所有项目一致。
 *
 * ## 定不下来时不编造
 *
 * - `path={variable}` → 不产节点，记 `route_path_unresolved`
 * - 路径定下来了、`element` 解析不到组件 → 产节点但**不产边**，记 `route_element_unresolved`
 * - 同一路径被声明两次 → 两个节点都保留，记 `duplicate_route_path`
 *
 * 只产节点不产边是刻意的：路径本身是确定的语法事实，硬把边指向某个组件才是编造。
 *
 * ## 置信度
 *
 * - route 节点 → 1.0：`path="/users"` 这个字面量就写在那里（Next 则是文件就在那里）
 * - route → 页面组件的边 → 0.9：与 renders 边同档，符号与语法都确定，但未经类型检查
 */

import type { Node, Project, SourceFile } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { Evidence, IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";
import {
  isHostElement,
  resolveIdentifierTarget,
  resolveTagTarget,
  type TagResolveContext,
} from "./jsx-refs.js";
import { toProjectRelative } from "./paths.js";
import type { ResolutionIndex } from "./resolver.js";

/** route 节点是语法事实，置信度取满。 */
const ROUTE_NODE_CONFIDENCE = 1;
/** route → 页面组件：符号解析确定，与 renders 边同档。 */
const ROUTE_EDGE_CONFIDENCE = 0.9;

/**
 * 路由工厂函数名单。
 *
 * 与 `on[A-Z]` 同性质：这是 react-router 的公开 API 名，对所有项目一致。
 * 命中它才继续看参数，避免把任意「带 path 的对象数组」误认成路由表。
 */
const ROUTE_FACTORIES: ReadonlySet<string> = new Set([
  "createBrowserRouter",
  "createHashRouter",
  "createMemoryRouter",
  "createStaticRouter",
  "useRoutes",
]);

/** 路由节点 attrs.framework 的取值。 */
export type RouteFramework = "react-router" | "next-app";

export interface RouteAnalysisResult {
  /** 需要并入图 nodes 的 route 节点 */
  routeNodes: IRNode[];
  /** route ──renders──> 页面组件 */
  edges: IREdge[];
  warnings: Warning[];
}

export interface RouteAnalysisInput {
  rootPath: string;
  project: Project;
  components: readonly DiscoveredComponent[];
  definitionIndex: ReadonlyMap<string, string>;
  resolution: ResolutionIndex;
  /** 项目类型；只有 next 才走文件约定扫描 */
  framework?: "react" | "next";
}

/* ------------------------------------------------------------------ *
 * 路径归一化
 * ------------------------------------------------------------------ */

/** 折叠重复斜杠、去掉尾部斜杠（根路径除外）。 */
function normalizePath(raw: string): string {
  const collapsed = raw.replace(/\/{2,}/g, "/");
  if (!collapsed.startsWith("/")) return `/${collapsed}`.replace(/\/$/, "") || "/";
  const trimmed = collapsed.replace(/\/+$/, "");
  return trimmed === "" ? "/" : trimmed;
}

/**
 * 拼接父子路径。
 *
 * 三条 React Router 的语义必须区分（写错会让 `/users/:id` 变成 `/users/:id/` 之类）：
 *  - 子路径以 `/` 开头 → 视为绝对路径，忽略父路径
 *  - `path="index"` → 索引路由，就是父路径本身
 *  - 空字符串 / `"/"` → 也是父路径本身
 */
function joinPath(parent: string, segment: string): string {
  const child = segment.trim();
  if (child === "index" || child === "" || child === "/") return normalizePath(parent || "/");
  if (child.startsWith("/")) return normalizePath(child);
  return normalizePath(`${parent}/${child}`);
}

/**
 * 从路径里取动态段名（`/users/:id` → `["id"]`）。
 *
 * 捕获所有段要剥掉尾部的 `*` / `*?`：`/blog/:slug*` 的参数名是 `slug`
 * 而不是 `slug*`——星号是匹配范围，不是名字的一部分。
 */
function extractParams(path: string): string[] {
  return path
    .split("/")
    .filter((segment) => segment.startsWith(":") && segment.length > 1)
    .map((segment) => segment.slice(1).replace(/\*+\??$/, ""));
}

/* ------------------------------------------------------------------ *
 * 属性取值
 * ------------------------------------------------------------------ */

/** 取 JSX 属性的值节点：`path="x"`、`path={"x"}`、`element={<X/>}` 都收窄到值本身。 */
function attributeValueNode(attribute: Node): Node | null {
  if (!MorphNode.isJsxAttribute(attribute)) return null;
  const initializer = attribute.getInitializer();
  if (!initializer) return null;
  if (MorphNode.isJsxExpression(initializer)) return initializer.getExpression() ?? null;
  return initializer;
}

/** 取字符串字面量（含无插值模板字符串）。 */
function literalString(node: Node | null): string | null {
  if (!node) return null;
  if (MorphNode.isStringLiteral(node)) return node.getLiteralText();
  if (MorphNode.isNoSubstitutionTemplateLiteral(node)) return node.getLiteralText();
  return null;
}

/**
 * 从一个「指向页面的表达式」解析出组件 id。
 *
 * 支持三种写法，全部走 jsx-refs 的同一套符号解析：
 *  - `<UserList />`（element 属性、对象字面量的 element 键）
 *  - `UserList`（Component 键）
 *  - `<UserList {...props} />` 这类带属性的自闭合元素
 *
 * 返回 null 表示根标签不是可解析的项目内组件（宿主元素、未导出组件、外部包）。
 */
function resolvePageComponent(
  value: Node | null,
  context: TagResolveContext,
): string | null {
  if (!value) return null;

  if (MorphNode.isJsxSelfClosingElement(value) || MorphNode.isJsxElement(value)) {
    const tagNameNode = MorphNode.isJsxSelfClosingElement(value)
      ? value.getTagNameNode()
      : value.getOpeningElement().getTagNameNode();
    if (isHostElement(tagNameNode.getText())) return null;
    const target = resolveTagTarget(tagNameNode, context);
    return target.kind === "component" ? target.id : null;
  }

  if (MorphNode.isIdentifier(value)) {
    const resolved = resolveIdentifierTarget(value, context);
    return resolved.kind === "component" ? resolved.id : null;
  }

  return null;
}

/* ------------------------------------------------------------------ *
 * 产出收集
 * ------------------------------------------------------------------ */

interface CollectedRoute {
  path: string;
  index: boolean;
  framework: RouteFramework;
  file: string;
  loc: { line: number; col: number };
  /** 解析到的页面组件 id；null 表示只产节点不产边 */
  pageComponentId: string | null;
}

/** 同一 nodeId 下累积的声明。 */
interface RouteBucket {
  /** 首次出现的那条，决定节点位置与主组件 */
  primary: CollectedRoute;
  /** 该路径下解析到的全部页面组件（index 路由与父路由会落在同一路径） */
  pageComponentIds: Set<string>;
  /** 只要有任意一条声明是 index，节点就带 index 标记 */
  index: boolean;
}

/**
 * 收集器：负责去重、重复路径告警与最终落图。
 *
 * 节点键是「文件 + 路径」，因此 index 路由（`path="index"`）与它的父路由
 * 必然撞键——这是 React Router 的正常写法（父路由出 layout，index 出内容），
 * 不是重复声明。撞键的处理是**合并**：节点只留一个，两边解析到的页面组件
 * 都作为边保留。只有跨文件撞路径才是真的重复声明，那才告警。
 */
class RouteCollector {
  private readonly buckets = new Map<string, RouteBucket>();
  private readonly pathOwners = new Map<string, string>();
  readonly warnings: Warning[] = [];

  add(route: CollectedRoute): void {
    const nodeId = `${route.file}#${route.path}`;
    const existing = this.buckets.get(nodeId);

    if (existing) {
      if (route.index) existing.index = true;
      if (route.pageComponentId !== null) existing.pageComponentIds.add(route.pageComponentId);
      return;
    }

    // 跨文件的同一路径才是歧义来源；同文件内的合并已在上面处理
    const firstOwner = this.pathOwners.get(route.path);
    if (firstOwner !== undefined && firstOwner !== nodeId) {
      this.warnings.push({
        code: "duplicate_route_path",
        severity: "warning",
        stage: "static",
        file: route.file,
        loc: route.loc,
        message: `路径 ${route.path} 已在 ${firstOwner} 声明，此处重复声明；两个节点都保留，定位时可能出现歧义`,
      });
    } else if (firstOwner === undefined) {
      this.pathOwners.set(route.path, nodeId);
    }

    if (route.pageComponentId === null) {
      this.warnings.push({
        code: "route_element_unresolved",
        severity: "warning",
        stage: "static",
        file: route.file,
        loc: route.loc,
        message: `路径 ${route.path} 的页面组件无法解析到项目内组件；只记录路由，不连边`,
      });
    }

    this.buckets.set(nodeId, {
      primary: route,
      pageComponentIds: route.pageComponentId === null ? new Set() : new Set([route.pageComponentId]),
      index: route.index,
    });
  }

  /** 组装成 IR 节点与边。 */
  build(): { routeNodes: IRNode[]; edges: IREdge[] } {
    const routeNodes: IRNode[] = [];
    const edges: IREdge[] = [];

    for (const [nodeId, bucket] of this.buckets) {
      const route = bucket.primary;
      const evidence: Evidence = {
        source: "static",
        confidence: ROUTE_NODE_CONFIDENCE,
        file: route.file,
        loc: route.loc,
      };

      routeNodes.push({
        id: nodeId,
        kind: "route",
        name: route.path,
        file: route.file,
        loc: route.loc,
        attrs: {
          path: route.path,
          framework: route.framework,
          params: extractParams(route.path),
          ...(bucket.index ? { index: true } : {}),
        },
        evidence: [evidence],
      });

      for (const pageComponentId of bucket.pageComponentIds) {
        edges.push({
          id: `e_renders_${nodeId}__${pageComponentId}`,
          from: nodeId,
          to: pageComponentId,
          kind: "renders",
          evidence: [{ ...evidence, confidence: ROUTE_EDGE_CONFIDENCE }],
        });
      }
    }

    return { routeNodes, edges };
  }
}

/* ------------------------------------------------------------------ *
 * React Router：对象式路由表
 * ------------------------------------------------------------------ */

/**
 * 递归遍历路由对象数组。
 *
 * 只从「确定是路由表的位置」进入：调用点是 ROUTE_FACTORIES 里的函数。
 * 不做「任何带 path 的对象都算路由」的猜测——那会把面包屑配置、菜单配置
 * 一并吞进来。
 */
function walkRouteObjects(
  array: Node,
  parentPath: string,
  context: TagResolveContext,
  collector: RouteCollector,
): void {
  if (!MorphNode.isArrayLiteralExpression(array)) return;
  const relativeFile = toProjectRelative(
    context.rootPath,
    array.getSourceFile().getFilePath(),
  );
  if (relativeFile === null) return;

  for (const element of array.getElements()) {
    if (!MorphNode.isObjectLiteralExpression(element)) continue;

    const pathProp = element.getProperty("path");
    const indexProp = element.getProperty("index");
    const elementProp = element.getProperty("element");
    const componentProp = element.getProperty("Component");
    const childrenProp = element.getProperty("children");

    const isIndex =
      indexProp !== undefined &&
      MorphNode.isPropertyAssignment(indexProp) &&
      indexProp.getInitializer()?.getText() === "true";

    let pathLiteral: string | null = null;
    if (MorphNode.isPropertyAssignment(pathProp)) {
      pathLiteral = literalString(pathProp.getInitializer() ?? null);
    }

    if (pathLiteral === null && !isIndex) {
      // 有 path 属性但不是字面量：路径确实存在，只是拿不到。
      // 没有 path 也没有 index 的，可能是纯 layout 路由，跳过它自己的节点、
      // 但仍要下探 children —— 子路由的路径是相对 layout 的父路径拼出来的。
      if (pathProp !== undefined) {
        collector.warnings.push({
          code: "route_path_unresolved",
          severity: "warning",
          stage: "static",
          file: relativeFile,
          loc: locOf(element),
          message: "路由的 path 不是字符串字面量，无法确定 URL，未产出 route 节点",
        });
      }
      if (childrenProp !== undefined && MorphNode.isPropertyAssignment(childrenProp)) {
        const childArray = childrenProp.getInitializer();
        if (childArray) walkRouteObjects(childArray, parentPath, context, collector);
      }
      continue;
    }

    const fullPath = isIndex ? normalizePath(parentPath || "/") : joinPath(parentPath, pathLiteral ?? "");

    const elementValue =
      MorphNode.isPropertyAssignment(elementProp) ? (elementProp.getInitializer() ?? null) : null;
    const componentValue =
      MorphNode.isPropertyAssignment(componentProp)
        ? (componentProp.getInitializer() ?? null)
        : null;

    const pageComponentId =
      resolvePageComponent(elementValue, context) ??
      resolvePageComponent(componentValue, context);

    collector.add({
      path: fullPath,
      index: isIndex,
      framework: "react-router",
      file: relativeFile,
      loc: locOf(pathProp ?? element),
      pageComponentId,
    });

    if (childrenProp !== undefined && MorphNode.isPropertyAssignment(childrenProp)) {
      const childArray = childrenProp.getInitializer();
      if (childArray) walkRouteObjects(childArray, fullPath, context, collector);
    }
  }
}

/* ------------------------------------------------------------------ *
 * React Router：JSX 式路由
 * ------------------------------------------------------------------ */

/** 取 JSX 元素的标签名，用于判断是不是 `<Route>` / `<Routes>`。 */
function elementTagName(node: Node): string | null {
  if (MorphNode.isJsxSelfClosingElement(node)) return node.getTagNameNode().getText();
  if (MorphNode.isJsxElement(node)) return node.getOpeningElement().getTagNameNode().getText();
  return null;
}

/** 标签的最后一段（`React.Route` → `Route`）。 */
function lastTagSegment(tagName: string): string {
  return tagName.split(".").pop() ?? tagName;
}

/** 取 JSX 元素的属性表；自闭合与成对元素统一处理。 */
function attributesOf(node: Node): Node[] {
  if (MorphNode.isJsxSelfClosingElement(node)) return node.getAttributes();
  if (MorphNode.isJsxElement(node)) return node.getOpeningElement().getAttributes();
  return [];
}

/** 按名字取 JSX 属性（含带值的与不带值的）。 */
function jsxAttribute(node: Node, name: string): Node | undefined {
  return attributesOf(node).find(
    (attr) => MorphNode.isJsxAttribute(attr) && attr.getNameNode().getText() === name,
  );
}

/**
 * 向上找最近的祖先 `<Route>` 元素，用于嵌套路径拼接。
 *
 * 同时检查链上有没有 `<Routes>`：只在 Routes 内部才算路由表，
 * 避免把项目自造的 `<Route>` 组件（如自己实现的 Tabs 项）当成路由。
 * 返回 `{ parent, inRoutes }`。
 */
function routeAncestry(node: Node): { parent: Node | null; inRoutes: boolean } {
  let parent: Node | null = null;
  let inRoutes = false;
  let current = node.getParent();

  while (current) {
    const tag = elementTagName(current);
    if (tag !== null) {
      const last = lastTagSegment(tag);
      if (last === "Route" && parent === null) parent = current;
      if (last === "Routes" || last === "RouterProvider") inRoutes = true;
    }
    current = current.getParent();
  }

  return { parent, inRoutes };
}

/** 遍历文件里的 `<Route>` 元素，按文档顺序处理以保证父路径先确定。 */
function walkRouteElements(
  file: SourceFile,
  relativeFile: string,
  context: TagResolveContext,
  collector: RouteCollector,
): void {
  const routeElements: Node[] = [
    ...file.getDescendantsOfKind(SyntaxKind.JsxSelfClosingElement),
    ...file.getDescendantsOfKind(SyntaxKind.JsxElement),
  ].filter((node) => {
    const tag = elementTagName(node);
    return tag !== null && lastTagSegment(tag) === "Route";
  });

  // 父元素在文档里必然先出现，按 start 排序即可保证「父路径已算好」
  routeElements.sort((a, b) => a.getStart() - b.getStart());

  /** 已算好的元素路径，供子元素拼接。 */
  const computed = new Map<number, string>();

  for (const node of routeElements) {
    const ancestry = routeAncestry(node);
    if (!ancestry.inRoutes) {
      // 不在 <Routes> 里的 Route：可能是自造组件，不做任何断言
      continue;
    }

    const parentPath = ancestry.parent ? (computed.get(ancestry.parent.getStart()) ?? "") : "";
    const pathAttr = jsxAttribute(node, "path");
    const indexAttr = jsxAttribute(node, "index");
    const isIndex = indexAttr !== undefined && pathAttr === undefined;

    let pathLiteral: string | null = null;
    if (pathAttr !== undefined) {
      pathLiteral = literalString(attributeValueNode(pathAttr));
    }

    if (pathLiteral === null && !isIndex) {
      if (pathAttr !== undefined) {
        collector.warnings.push({
          code: "route_path_unresolved",
          severity: "warning",
          stage: "static",
          file: relativeFile,
          loc: locOf(pathAttr),
          message: "路由的 path 不是字符串字面量，无法确定 URL，未产出 route 节点",
        });
      }
      continue;
    }

    const fullPath = isIndex
      ? normalizePath(parentPath || "/")
      : joinPath(parentPath, pathLiteral ?? "");

    const elementAttr = jsxAttribute(node, "element");
    const componentAttr = jsxAttribute(node, "component") ?? jsxAttribute(node, "Component");

    const pageComponentId =
      resolvePageComponent(
        elementAttr ? attributeValueNode(elementAttr) : null,
        context,
      ) ??
      resolvePageComponent(
        componentAttr ? attributeValueNode(componentAttr) : null,
        context,
      );

    computed.set(node.getStart(), fullPath);

    collector.add({
      path: fullPath,
      index: isIndex,
      framework: "react-router",
      file: relativeFile,
      loc: locOf(pathAttr ?? node),
      pageComponentId,
    });
  }
}

/* ------------------------------------------------------------------ *
 * Next.js 文件约定
 * ------------------------------------------------------------------ */

/**
 * 把 Next 的目录段转成 URL 段。
 *
 * 返回 null 表示这一段不参与 URL（分组 `(marketing)`、并行槽 `@modal`、
 * 拦截标记 `(.)`），或整个文件不应被当作页面（`_private` 目录）。
 */
function nextSegment(segment: string): string | null | "skip-file" {
  // `_` 前缀目录是私有目录，不产生路由
  if (segment.startsWith("_")) return "skip-file";
  // `@slot` 并行路由不改变 URL
  if (segment.startsWith("@")) return null;

  if (segment.startsWith("(") && segment.endsWith(")")) {
    const inner = segment.slice(1, -1);
    // `(marketing)` 这类分组不参与 URL
    if (!inner.startsWith(".")) return null;
    // `(.)photos` / `(..)photos` / `(...)photos`：拦截路由，URL 里是后半段
    const rest = inner.replace(/^\.+/, "");
    return rest === "" ? null : rest;
  }

  // 动态段：`[id]` → `:id`、`[...slug]` → `:slug*`、`[[...slug]]` → `:slug*?`
  const optionalCatchAll = /^\[\[\.\.\.(.+)\]\]$/.exec(segment);
  if (optionalCatchAll) return `:${optionalCatchAll[1]}*?`;
  const catchAll = /^\[\.\.\.(.+)\]$/.exec(segment);
  if (catchAll) return `:${catchAll[1]}*`;
  const dynamic = /^\[(.+)\]$/.exec(segment);
  if (dynamic) return `:${dynamic[1]}`;

  return segment;
}

/** 把目录段序列转成 URL 路径；遇到私有目录返回 null。 */
function nextPathFromSegments(segments: readonly string[]): string | null {
  const parts: string[] = [];
  for (const segment of segments) {
    const converted = nextSegment(segment);
    if (converted === "skip-file") return null;
    if (converted !== null) parts.push(converted);
  }
  return normalizePath(parts.length === 0 ? "/" : `/${parts.join("/")}`);
}

/**
 * 从页面文件里挑出默认导出的组件。
 *
 * Next 的页面组件必须是 default export。同文件里可能还有别的组件（布局片段），
 * 所以不能「文件里只有一个组件就用它」——那在有多组件时会选错。
 */
function pickPageComponent(
  file: string,
  componentsByFile: ReadonlyMap<string, readonly DiscoveredComponent[]>,
): DiscoveredComponent | null {
  const candidates = componentsByFile.get(file) ?? [];
  const defaultExport = candidates.find((item) => item.exportNames.includes("default"));
  return defaultExport ?? null;
}

/* ------------------------------------------------------------------ *
 * 入口
 * ------------------------------------------------------------------ */

function locOf(node: Node | undefined): { line: number; col: number } {
  if (!node) return { line: 1, col: 1 };
  const pos = node.getSourceFile().getLineAndColumnAtPos(node.getStart());
  return { line: pos.line, col: pos.column };
}

const APP_PAGE_PATTERN = /^(?:src\/)?app\/(.*?)page\.(?:tsx|jsx|ts|js)$/;
const PAGES_PAGE_PATTERN = /^(?:src\/)?pages\/(.*?)\.(?:tsx|jsx|ts|js)$/;
/** Next 的 pages 目录里这些文件不是路由。 */
const PAGES_SPECIAL = new Set(["_app", "_document", "_error", "middleware"]);

export function analyzeRoutes(input: RouteAnalysisInput): RouteAnalysisResult {
  const { rootPath, project, components, definitionIndex, resolution, framework } = input;

  const collector = new RouteCollector();
  const context: TagResolveContext = { rootPath, definitionIndex, resolution };

  for (const file of project.getSourceFiles()) {
    const relativeFile = toProjectRelative(rootPath, file.getFilePath());
    if (relativeFile === null) continue;

    // 对象式路由表
    for (const call of file.getDescendantsOfKind(SyntaxKind.CallExpression)) {
      const callee = call.getExpression().getText();
      if (!ROUTE_FACTORIES.has(lastTagSegment(callee))) continue;
      const firstArg = call.getArguments()[0];
      if (!firstArg) continue;
      walkRouteObjects(firstArg, "/", context, collector);
    }

    // JSX 式路由表
    walkRouteElements(file, relativeFile, context, collector);
  }

  if (framework === "next") {
    const componentsByFile = new Map<string, DiscoveredComponent[]>();
    for (const component of components) {
      const list = componentsByFile.get(component.file);
      if (list) list.push(component);
      else componentsByFile.set(component.file, [component]);
    }

    for (const file of project.getSourceFiles()) {
      const relativeFile = toProjectRelative(rootPath, file.getFilePath());
      if (relativeFile === null) continue;

      const appMatch = APP_PAGE_PATTERN.exec(relativeFile);
      const pagesMatch = appMatch ? null : PAGES_PAGE_PATTERN.exec(relativeFile);
      if (!appMatch && !pagesMatch) continue;

      // app 形态：`app/users/page.tsx` → 目录段 ["users"]
      // pages 形态：`pages/users/index.tsx` → 文件段 ["users","index"]
      const rawSegments = (appMatch ? (appMatch[1] ?? "") : (pagesMatch?.[1] ?? ""))
        .split("/")
        .filter((segment) => segment.length > 0);

      if (!appMatch) {
        const stem = rawSegments[rawSegments.length - 1];
        // 旧版 pages 目录里的 api 与特殊文件不是页面
        if (rawSegments.includes("api")) continue;
        if (stem !== undefined && PAGES_SPECIAL.has(stem)) continue;
        // `pages/index.tsx` → "/"，`pages/users/index.tsx` → "/users"
        if (stem === "index") rawSegments.pop();
      }

      const path = nextPathFromSegments(rawSegments);
      if (path === null) continue;

      const pageComponent = pickPageComponent(relativeFile, componentsByFile);
      const pageComponentId = pageComponent?.id ?? null;

      collector.add({
        path,
        index: path === "/" && rawSegments.length === 0,
        framework: "next-app",
        file: relativeFile,
        // Next 没有 path 字面量，位置取页面组件声明处；组件缺失时退回文件头
        loc: pageComponent?.loc ?? { line: 1, col: 1 },
        pageComponentId,
      });
    }
  }

  const built = collector.build();
  return { routeNodes: built.routeNodes, edges: built.edges, warnings: collector.warnings };
}
