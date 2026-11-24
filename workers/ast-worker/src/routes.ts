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
 * | React Router | 路由工厂函数的参数（`createBrowserRouter([...])` / `useRoutes([...])`）、`lazy` 动态导入或 `<Routes>` 内的 `<Route>` |
 * | Next.js 文件约定 | `app/**\/page.tsx`、`pages/**\/*.tsx` 的目录结构 |
 *
 * 两条路都只认**框架的语言级约定**，不出现任何业务名判断：`createBrowserRouter`
 * 是 react-router 的 API，`page.tsx` 是 Next 的约定，对所有项目一致。
 *
 * ## layout / page 区分
 *
 * 同一路径下可能同时挂布局组件和页面组件（父路由出 layout、index 出内容）。
 * 把布局组件当成页面，0.9 的置信度也救不回来——所以判定规则必须显式：
 *
 *  - 路由 element 内出现 react-router 的 `<Outlet>`（内联 JSX 或组件声明子树里）→ layout
 *  - 有子路由的 element → layout（React Router 语义：子路由渲染在 Outlet 里）
 *  - 叶子路由（无子路由）的 element → page
 *  - index route 的 element 优先作为页面
 *  - Next.js：`layout.tsx` 按目录前缀归属到其子树内所有页面路由
 *
 * 结论写进 route 节点的 attrs：`pageComponentId`（唯一页面）与
 * `layoutComponentIds`（布局组件列表）。renders 边照常产出——layout 和 page
 * 都是真实的渲染事实，区别只在于语义归类。同一路径解析到**多个**页面组件时
 * 不写 `pageComponentId`，记 `route_page_ambiguous`，召回结果同时给出候选，
 * 不静默取第一个。
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

import { readdirSync, statSync, type Dirent } from "node:fs";
import path from "node:path";

import type {
  ArrayLiteralExpression,
  CallExpression,
  Node,
  Project,
  SourceFile,
} from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { Evidence, IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";
import {
  findImportSource,
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
export type RouteFramework = "react-router" | "next-app" | "docusaurus";

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
export function normalizePath(raw: string): string {
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
 * 段名后面可能带修饰符，这些都不是名字的一部分：
 *  - `:id?`   可选参数（React Router v6）→ `id`
 *  - `:slug*` 捕获所有 → `slug`
 *  - `:slug*?` 可选捕获所有 → `slug`
 *
 * 顺序必须是从长到短：先剥 `?` 再剥 `*` 会把 `:slug*?` 变成 `slug*`。
 */
function extractParams(path: string): string[] {
  return path
    .split("/")
    .filter((segment) => segment.startsWith(":") && segment.length > 1)
    .map((segment) => segment.slice(1).replace(/\*?\?$/, "").replace(/\*$/, ""));
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

/** 解析简单路径常量、嵌套对象属性和 `paths.users.getHref()` 工厂调用。 */
/**
 * 沿对象字面量的属性链取出静态值：`paths.app.discussions.path`。
 *
 * 这里不能只依赖最终属性的 symbol：当项目配置不完整或跨 tsconfig 时，
 * ts-morph 可能只给中间属性绑定 symbol，最末端的 `path` 没有 symbol。
 * 直接沿 AST 的对象声明回溯仍然是确定的语法解析，不是按名称猜值。
 */
function staticObjectValue(node: Node, depth = 0): Node | null {
  if (depth > 8) return null;
  let current = node;
  while (
    MorphNode.isParenthesizedExpression(current) ||
    MorphNode.isAsExpression(current) ||
    MorphNode.isSatisfiesExpression(current)
  ) {
    current = current.getExpression();
  }

  if (MorphNode.isObjectLiteralExpression(current)) return current;

  if (MorphNode.isIdentifier(current)) {
    let symbol = current.getSymbol();
    if (symbol?.isAlias()) {
      try {
        symbol = symbol.getAliasedSymbol();
      } catch {
        return null;
      }
    }
    for (const declaration of symbol?.getDeclarations() ?? []) {
      if (MorphNode.isVariableDeclaration(declaration)) {
        const initializer = declaration.getInitializer();
        if (initializer) return staticObjectValue(initializer, depth + 1);
      }
      if (MorphNode.isPropertyAssignment(declaration)) {
        const initializer = declaration.getInitializer();
        if (initializer) return staticObjectValue(initializer, depth + 1);
      }
    }
    return null;
  }

  if (MorphNode.isPropertyAccessExpression(current)) {
    const object = staticObjectValue(current.getExpression(), depth + 1);
    if (!object || !MorphNode.isObjectLiteralExpression(object)) return null;
    const property = object.getProperty(current.getName());
    if (!MorphNode.isPropertyAssignment(property)) return null;
    return property.getInitializer() ?? null;
  }

  return null;
}

function staticPathString(node: Node | null, depth = 0): string | null {
  if (!node || depth > 8) return null;
  let current = node;
  while (MorphNode.isParenthesizedExpression(current) || MorphNode.isAsExpression(current) || MorphNode.isSatisfiesExpression(current)) {
    current = current.getExpression();
  }
  const literal = literalString(current);
  if (literal !== null) return literal;
  if (MorphNode.isTemplateExpression(current)) {
    let text = current.getHead().getLiteralText();
    for (const span of current.getTemplateSpans()) {
      const value = staticPathString(span.getExpression(), depth + 1);
      if (value === null) return null;
      text += value + span.getLiteral().getLiteralText();
    }
    return text;
  }
  if (MorphNode.isBinaryExpression(current) && current.getOperatorToken().getKind() === SyntaxKind.PlusToken) {
    const left = staticPathString(current.getLeft(), depth + 1);
    const right = staticPathString(current.getRight(), depth + 1);
    return left !== null && right !== null ? left + right : null;
  }
  if (MorphNode.isCallExpression(current) && MorphNode.isPropertyAccessExpression(current.getExpression())) {
    const callee = current.getExpression();
    if (!callee.getText().endsWith(".getHref")) return null;
    let symbol = callee.getSymbol();
    if (symbol?.isAlias()) {
      try { symbol = symbol.getAliasedSymbol(); } catch { return null; }
    }
    for (const declaration of symbol?.getDeclarations() ?? []) {
      let fn: Node | null = null;
      if (MorphNode.isPropertyAssignment(declaration)) fn = declaration.getInitializer() ?? null;
      else if (MorphNode.isMethodDeclaration(declaration)) fn = declaration;
      if (!fn || (!MorphNode.isArrowFunction(fn) && !MorphNode.isFunctionExpression(fn) && !MorphNode.isMethodDeclaration(fn))) continue;
      const body = fn.getBody();
      if (!body) continue;
      if (!MorphNode.isBlock(body)) return staticPathString(body, depth + 1);
      const statement = body.getStatements().find((item) => MorphNode.isReturnStatement(item));
      if (statement && MorphNode.isReturnStatement(statement)) {
        const expression = statement.getExpression();
        if (expression) return staticPathString(expression, depth + 1);
      }
    }
    return null;
  }
  if (MorphNode.isPropertyAccessExpression(current)) {
    const value = staticObjectValue(current);
    if (value) return staticPathString(value, depth + 1);

    let symbol = current.getSymbol();
    if (symbol?.isAlias()) {
      try { symbol = symbol.getAliasedSymbol(); } catch { return null; }
    }
    for (const declaration of symbol?.getDeclarations() ?? []) {
      if (MorphNode.isVariableDeclaration(declaration) || MorphNode.isPropertyAssignment(declaration)) {
        const initializer = declaration.getInitializer();
        const value = staticPathString(initializer ?? null, depth + 1);
        if (value !== null) return value;
      }
    }
  }
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

/**
 * 读取路由对象里的页面组件字段。
 *
 * react-router 的数据路由用大写 `Component`，老式 CRA 路由表普遍用小写 `component`；
 * 两种都认。只匹配这两个确定的键名，不做全局大小写不敏感，避免把业务字段当组件。
 * 简写属性 `{ component }`（ShorthandPropertyAssignment）取同名标识符。
 */
function readRouteComponentValue(element: Node): Node | null {
  if (!MorphNode.isObjectLiteralExpression(element)) return null;
  // 不用 getProperty：它不覆盖简写属性 `{ component }`，这里按名字遍历两种属性形态
  const prop = ["Component", "component"]
    .map((name) =>
      element
        .getProperties()
        .find(
          (candidate) =>
            (MorphNode.isPropertyAssignment(candidate) ||
              MorphNode.isShorthandPropertyAssignment(candidate)) &&
            candidate.getName() === name,
        ),
    )
    .find((candidate) => candidate !== undefined);
  if (prop === undefined) return null;
  if (MorphNode.isPropertyAssignment(prop)) return prop.getInitializer() ?? null;
  if (MorphNode.isShorthandPropertyAssignment(prop)) {
    const nameNode = prop.getNameNode();
    // 简写属性的名字节点拿到的是 property symbol（声明就是简写本身），
    // 换成同文件同名变量声明的名字节点，才能沿符号找到 `const component = ...`。
    return (
      nameNode.getSourceFile().getVariableDeclaration(nameNode.getText())?.getNameNode() ??
      nameNode
    );
  }
  return null;
}

/**
 * 解析 React Router 对象式路由的 `lazy` 属性。
 *
 * 只接受静态动态导入，并从目标模块的 default export 取页面组件；`.then(convert(...))`
 * 只是 React Router lazy 模块包装，不影响页面模块本身的归属。
 */
function resolveLazyPageComponent(
  value: Node | null,
  context: TagResolveContext,
  componentsByFile: ReadonlyMap<string, readonly DiscoveredComponent[]>,
): string | null {
  if (!value) return null;
  const imports = value.getDescendantsOfKind(SyntaxKind.CallExpression).filter((call) => {
    return call.getExpression().getKind() === SyntaxKind.ImportKeyword;
  });
  for (const call of imports) {
    const resolved = resolveLazyImportCall(call, context, componentsByFile);
    if (resolved) return resolved;
  }
  return null;
}

/**
 * `component: Home` 且 `Home = lazy(() => import("..."))` 的归属解析。
 *
 * 与 `lazy` 属性的区别是这里要**先严格确认包装是 `lazy(...)`**：普通组件函数体里
 * 也可能出现动态 import 做代码分割，仅凭“看到 import()”就归属会误判。
 * 标识符只穿透一层声明（`const Home = lazy(...)`），不做多层 HOC 展开。
 */
function resolveLazyComponentValue(
  value: Node | null,
  context: TagResolveContext,
  componentsByFile: ReadonlyMap<string, readonly DiscoveredComponent[]>,
): string | null {
  const importCall = lazyImportCallOf(value);
  return importCall ? resolveLazyImportCall(importCall, context, componentsByFile) : null;
}

/** 取 `lazy(() => import("..."))` 里的那个 import 调用；不是 lazy 形态就返回 null。 */
function lazyImportCallOf(value: Node | null): CallExpression | null {
  if (!value) return null;
  let node: Node = value;
  while (MorphNode.isParenthesizedExpression(node)) node = node.getExpression();

  if (MorphNode.isIdentifier(node)) {
    let symbol = node.getSymbol();
    if (symbol?.isAlias()) {
      try {
        symbol = symbol.getAliasedSymbol();
      } catch {
        return null;
      }
    }
    for (const decl of symbol?.getDeclarations() ?? []) {
      if (!MorphNode.isVariableDeclaration(decl)) continue;
      const initializer = decl.getInitializer();
      if (!initializer) continue;
      const lazyCall = asLazyCall(initializer);
      const importCall = lazyCall ? findImportCall(lazyCall) : null;
      if (importCall) return importCall;
    }
    return null;
  }

  const lazyCall = asLazyCall(node);
  return lazyCall ? findImportCall(lazyCall) : null;
}

function asLazyCall(node: Node): CallExpression | null {
  let current: Node = node;
  while (MorphNode.isParenthesizedExpression(current)) current = current.getExpression();
  if (!MorphNode.isCallExpression(current)) return null;
  return lastTagSegment(current.getExpression().getText()) === "lazy" ? current : null;
}

function findImportCall(lazyCall: CallExpression): CallExpression | null {
  const factory = lazyCall.getArguments()[0];
  if (!factory) return null;
  for (const call of factory.getDescendantsOfKind(SyntaxKind.CallExpression)) {
    if (call.getExpression().getKind() === SyntaxKind.ImportKeyword) return call;
  }
  return null;
}

/** 动态导入 → 目标模块唯一的 default export 组件；解析失败返回 null，不猜。 */
function resolveLazyImportCall(
  call: CallExpression,
  context: TagResolveContext,
  componentsByFile: ReadonlyMap<string, readonly DiscoveredComponent[]>,
): string | null {
  const argument = call.getArguments()[0];
  if (!argument || !MorphNode.isStringLiteral(argument)) return null;
  // 基准文件必须是 import 调用自身所在的文件：穿透标识符后它可能不在路由表文件里
  const resolved = context.resolution.resolve(call.getSourceFile(), argument.getLiteralValue());
  if (resolved.status !== "internal") return null;
  const relativeFile = toProjectRelative(context.rootPath, resolved.filePath);
  if (!relativeFile) return null;
  const defaults = (componentsByFile.get(relativeFile) ?? []).filter((component) =>
    component.exportNames.includes("default"),
  );
  return defaults.length === 1 ? defaults[0]?.id ?? null : null;
}

/**
 * 只看标签名——是否真的来自 react-router 由调用方的导入检查兜底。
 */
function subtreeHasOutletTag(node: Node): boolean {
  return node.getDescendants().some((descendant) => {
    const tag = elementTagName(descendant);
    return tag !== null && lastTagSegment(tag) === "Outlet";
  });
}

/**
 * 从「指向组件的表达式」取到组件声明节点（变量声明 / 函数声明）。
 *
 * 与 resolveSymbolTarget 同一条符号解析路径，但返回声明本身而不是组件 id。
 */
function componentDeclarationNodes(value: Node): Node[] {
  let tagNode: Node | null = null;
  if (MorphNode.isJsxSelfClosingElement(value) || MorphNode.isJsxElement(value)) {
    tagNode = MorphNode.isJsxSelfClosingElement(value)
      ? value.getTagNameNode()
      : value.getOpeningElement().getTagNameNode();
  } else if (MorphNode.isIdentifier(value)) {
    tagNode = value;
  }
  if (!tagNode) return [];

  let symbol = tagNode.getSymbol();
  if (symbol?.isAlias()) {
    try {
      symbol = symbol.getAliasedSymbol();
    } catch {
      return [];
    }
  }
  return [...(symbol?.getDeclarations() ?? [])];
}

/**
 * 判断「路由 element 的值」渲染的内容里是否出现 react-router 的 `<Outlet>`。
 *
 * 三种来源都要认：
 *  - 内联 JSX：`element={<Layout><Outlet /></Layout>}` —— 值节点子树里就有
 *  - 组件声明子树：`element={<UsersLayout />}`，`<Outlet />` 写在组件体内
 *  - 点号形式：`<Router.Outlet />`
 *
 * 组件声明侧要求该文件真的从 react-router 导入了 `Outlet`（含别名导入的反查
 * 只认原名），排除项目自造的同名组件——判断依据是导入来源，不看名字猜。
 * 扫描只做一层，不做传递展开：layout 套 layout 的写法由外层路由自己声明。
 */
function containsOutlet(value: Node | null): boolean {
  if (!value) return false;
  if (subtreeHasOutletTag(value)) return true;
  for (const decl of componentDeclarationNodes(value)) {
    const sourceFile = decl.getSourceFile();
    const importSource = findImportSource(sourceFile, "Outlet");
    if (importSource === null || !importSource.includes("react-router")) continue;
    if (subtreeHasOutletTag(decl)) return true;
  }
  return false;
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
  /** 解析到的组件 id；null 表示只产节点不产边 */
  pageComponentId: string | null;
  /**
   * 该组件是否是布局而非页面。
   *
   * 判定依据（按优先级）：element 内出现 react-router 的 `<Outlet>` → 是；
   * 有子路由（React Router 语义：子路由渲染在 Outlet 里）→ 是；否则是页面。
   */
  isLayout: boolean;
  /**
   * 路由 / 菜单标题（`title` / `label` / `name` 属性，或 react-router 的
   * `handle.title`）。需求写的是「订单管理页」，代码里只有 `/order/list`，
   * 没有标题就永远对不上。取不到时为 null——标题是可选信息，不编造。
   */
  title: string | null;
  /**
   * Next 文件约定下按目录前缀归属到本页面的 layout 组件。
   *
   * `app/users/layout.tsx` 包住 `app/users/page.tsx` 及其子树。React Router
   * 场景不用这个字段——layout 由 element 的解析结果与 isLayout 判定得出。
   */
  inheritedLayoutComponentIds?: readonly string[];
  /**
   * 文件约定产出的路由。
   *
   * md/mdx 页面本来就没有组件节点，不能按「显式路由解析不到 element」告警；
   * 代码页面缺默认导出时由文件路由扫描单独告警。
   */
  fileConvention?: boolean;
}

/** 同一 nodeId 下累积的声明。 */
interface RouteBucket {
  /** 首次出现的那条，决定节点位置与标题 */
  primary: CollectedRoute;
  /** 该路径下的页面组件（index 路由与父路由会落在同一路径） */
  pageComponentIds: Set<string>;
  /** 该路径下的布局组件 */
  layoutComponentIds: Set<string>;
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

  /** 当前已声明的路径快照；文件路由用它判断是否被显式配置占用。 */
  paths(): ReadonlySet<string> {
    return new Set(this.pathOwners.keys());
  }

  add(route: CollectedRoute): void {
    const nodeId = `${route.file}#${route.path}`;
    const existing = this.buckets.get(nodeId);

    if (existing) {
      if (route.index) existing.index = true;
      // layout 与 page 分流：同一路径下父路由的布局组件不能冒充页面
      if (route.pageComponentId !== null) {
        (route.isLayout ? existing.layoutComponentIds : existing.pageComponentIds).add(
          route.pageComponentId,
        );
      }
      for (const layoutId of route.inheritedLayoutComponentIds ?? []) {
        existing.layoutComponentIds.add(layoutId);
      }
      // 同一路径的 index 声明可能带标题而主声明没有：标题补上，不互相覆盖
      if (existing.primary.title === null && route.title !== null) {
        existing.primary.title = route.title;
      }
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

    if (route.pageComponentId === null && route.fileConvention !== true) {
      this.warnings.push({
        code: "route_element_unresolved",
        severity: "warning",
        stage: "static",
        file: route.file,
        loc: route.loc,
        message: `路径 ${route.path} 的页面组件无法解析到项目内组件；只记录路由，不连边`,
      });
    }

    const layoutComponentIds = new Set(route.inheritedLayoutComponentIds ?? []);
    if (route.pageComponentId !== null && route.isLayout) {
      layoutComponentIds.add(route.pageComponentId);
    }

    const isPageResolved = route.pageComponentId !== null && !route.isLayout;
    this.buckets.set(nodeId, {
      primary: route,
      pageComponentIds: new Set(isPageResolved ? [route.pageComponentId as string] : []),
      layoutComponentIds,
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

      const pageComponentIds = [...bucket.pageComponentIds];
      const layoutComponentIds = [...bucket.layoutComponentIds].sort();

      // 页面组件存在歧义时不静默取第一个：留空 attrs，由召回结果给出全部候选
      if (pageComponentIds.length > 1) {
        this.warnings.push({
          code: "route_page_ambiguous",
          severity: "warning",
          stage: "static",
          file: route.file,
          loc: route.loc,
          message: `路径 ${route.path} 解析到多个页面组件（${pageComponentIds.join("、")}）；不标记唯一页面，召回结果会同时给出候选`,
        });
      }

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
          ...(bucket.primary.title !== null ? { title: bucket.primary.title } : {}),
          ...(pageComponentIds.length === 1 ? { pageComponentId: pageComponentIds[0] } : {}),
          ...(layoutComponentIds.length > 0 ? { layoutComponentIds } : {}),
        },
        evidence: [evidence],
      });

      // layout 与 page 都是真实的渲染事实，renders 边照常产出
      for (const targetId of [...pageComponentIds, ...layoutComponentIds]) {
        edges.push({
          id: `e_renders_${nodeId}__${targetId}`,
          from: nodeId,
          to: targetId,
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
/**
 * 把「指向路由数组的表达式」还原成数组字面量。
 *
 * 三种写法都要认，它们在真实项目里同样普遍：
 *  - `useRoutes([...])`            直接是字面量
 *  - `useRoutes(appRoutes)`        引用变量（`const appRoutes = [...]`）
 *  - `useRoutes(routes.filter(…))` 这类求值不出来的 → null，由调用方决定是否告警
 *
 * 沿符号声明回溯而不是按名字猜：同名变量在不同文件里是不同数组。
 */
function resolveRouteArray(node: Node, depth = 0): ArrayLiteralExpression | null {
  // 层数只记「跨变量」的跳数：括号与 as const / satisfies 只是包裹语法，
  // 不改变指向哪个数组，不该消耗深度——否则 `const a = b` 套两层断言就越界
  if (depth > 3) return null;
  if (MorphNode.isArrayLiteralExpression(node)) return node;

  if (MorphNode.isParenthesizedExpression(node)) {
    return resolveRouteArray(node.getExpression(), depth);
  }

  // `as const` / 类型断言不影响数组本身
  if (MorphNode.isAsExpression(node) || MorphNode.isSatisfiesExpression(node)) {
    return resolveRouteArray(node.getExpression(), depth);
  }

  if (MorphNode.isIdentifier(node)) {
    let symbol = node.getSymbol();
    if (symbol?.isAlias()) {
      try {
        symbol = symbol.getAliasedSymbol();
      } catch {
        return null;
      }
    }
    for (const decl of symbol?.getDeclarations() ?? []) {
      if (!MorphNode.isVariableDeclaration(decl)) continue;
      const init = decl.getInitializer();
      if (!init) continue;
      const resolved = resolveRouteArray(init, depth + 1);
      if (resolved) return resolved;
    }
  }

  return null;
}

/**
 * 从路由对象里取标题字面量。
 *
 * 三个属性名都认：`title` 最通用，`label` / `name` 常出现在「路由表顺带当
 * 菜单配置用」的项目里；react-router 的官方约定 `handle.title` 也一并收。
 * 只收字符串字面量——`t("menu.order")` 这类表达式静态求不出值，不编造。
 * 标题是可选信息，取不到就返回 null，不告警。
 */
function routeTitleLiteral(element: Node): string | null {
  if (!MorphNode.isObjectLiteralExpression(element)) return null;
  for (const key of ["title", "label", "name"] as const) {
    const prop = element.getProperty(key);
    if (MorphNode.isPropertyAssignment(prop)) {
      const literal = literalString(prop.getInitializer() ?? null);
      if (literal !== null) return literal;
    }
  }
  const handleProp = element.getProperty("handle");
  if (MorphNode.isPropertyAssignment(handleProp)) {
    const handle = handleProp.getInitializer();
    if (handle && MorphNode.isObjectLiteralExpression(handle)) {
      return routeTitleLiteral(handle);
    }
  }
  return null;
}

function walkRouteObjects(
  array: Node,
  parentPath: string,
  context: TagResolveContext,
  collector: RouteCollector,
  componentsByFile: ReadonlyMap<string, readonly DiscoveredComponent[]>,
  visited?: Set<string>,
): void {
  const resolved = resolveRouteArray(array);
  if (!resolved) return;

  // 同一条数组只展开一次：工厂通道与 Routes 渲染通道可能指向同一份数据。
  // key 用「文件 + 起始偏移」而不是路径字符串——同一文件里两个同名数组是不同数组。
  if (visited) {
    const key = `${resolved.getSourceFile().getFilePath()}:${resolved.getStart()}`;
    if (visited.has(key)) return;
    visited.add(key);
  }

  const relativeFile = toProjectRelative(
    context.rootPath,
    resolved.getSourceFile().getFilePath(),
  );
  if (relativeFile === null) return;

  for (const element of resolved.getElements()) {
    // `[...WelcomeRoutes, ...PostRoutes]`：每条 spread 都是一个独立的路由数组，
    // 递归展开到它的字面量。真实项目普遍按 feature 拆分路由再聚合，
    // 只认数组字面量会让整张路由表一个节点都产不出。
    if (MorphNode.isSpreadElement(element)) {
      const spreadTarget = resolveRouteArray(element.getExpression());
      if (spreadTarget) {
        walkRouteObjects(spreadTarget, parentPath, context, collector, componentsByFile, visited);
        continue;
      }
      // 展开不到字面量（函数返回、条件表达式）：路由确实存在但看不到，
      // 如实记一条，不假装这个项目没有路由。
      collector.warnings.push({
        code: "route_table_unresolved",
        severity: "info",
        stage: "static",
        file: relativeFile,
        loc: locOf(element),
        message: `路由表展开项 \`${element.getText()}\` 无法静态求值，其下的路由未计入图谱`,
      });
      continue;
    }

    if (!MorphNode.isObjectLiteralExpression(element)) continue;

    const pathProp = element.getProperty("path");
    const indexProp = element.getProperty("index");
    const elementProp = element.getProperty("element");
    const lazyProp = element.getProperty("lazy");
    const childrenProp = element.getProperty("children");

    const isIndex =
      indexProp !== undefined &&
      MorphNode.isPropertyAssignment(indexProp) &&
      indexProp.getInitializer()?.getText() === "true";

    let pathLiteral: string | null = null;
    if (MorphNode.isPropertyAssignment(pathProp)) {
      pathLiteral = staticPathString(pathProp.getInitializer() ?? null);
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
        if (childArray) walkRouteObjects(childArray, parentPath, context, collector, componentsByFile, visited);
      }
      continue;
    }

    const fullPath = isIndex ? normalizePath(parentPath || "/") : joinPath(parentPath, pathLiteral ?? "");

    const elementValue =
      MorphNode.isPropertyAssignment(elementProp) ? (elementProp.getInitializer() ?? null) : null;
    const componentValue = readRouteComponentValue(element);
    const lazyValue =
      MorphNode.isPropertyAssignment(lazyProp)
        ? (lazyProp.getInitializer() ?? null)
        : null;

    const pageComponentId =
      resolvePageComponent(elementValue, context) ??
      resolvePageComponent(componentValue, context) ??
      // 小写 component 常见指向 lazy 局部变量，与 react-router 的 lazy 键同等对待
      resolveLazyComponentValue(componentValue, context, componentsByFile) ??
      resolveLazyPageComponent(lazyValue, context, componentsByFile);

    // layout 判定：<Outlet>（内联或组件声明子树里）优先，其次嵌套路由层级
    const hasChildren = childrenProp !== undefined;
    const isLayout = containsOutlet(elementValue) || containsOutlet(componentValue) || hasChildren;

    collector.add({
      path: fullPath,
      index: isIndex,
      framework: "react-router",
      file: relativeFile,
      loc: locOf(pathProp ?? element),
      pageComponentId,
      isLayout,
      title: routeTitleLiteral(element),
    });

    if (childrenProp !== undefined && MorphNode.isPropertyAssignment(childrenProp)) {
      const childArray = childrenProp.getInitializer();
      if (childArray) walkRouteObjects(childArray, fullPath, context, collector, componentsByFile, visited);
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

/**
 * 判断 JSX 式 `<Route>` 是否带子 `<Route>`。
 *
 * 嵌套路由的子元素渲染在父 element 的 Outlet 里——与对象式的 `children`
 * 属性同一条判定依据，不能只看对象式漏掉 JSX 式。
 */
function hasRouteChildren(node: Node): boolean {
  if (!MorphNode.isJsxElement(node)) return false;
  return node.getDescendants().some((descendant) => {
    const tag = elementTagName(descendant);
    return tag !== null && lastTagSegment(tag) === "Route";
  });
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
      pathLiteral = staticPathString(attributeValueNode(pathAttr));
    }

    if (pathLiteral === null && !isIndex) {
      // 与对象式路由表保持一致：路径拿不到时这个路由自己不产节点，但仍要登记
      // 一条「子路由的基准路径」——纯 layout 路由（无 path、只有 children）在
      // JSX 写法里很常见，不登记的话子路由会丢掉父前缀，`/users/:id` 变成 `/:id`。
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
      computed.set(node.getStart(), parentPath);
      continue;
    }

    const fullPath = isIndex
      ? normalizePath(parentPath || "/")
      : joinPath(parentPath, pathLiteral ?? "");

    const elementAttr = jsxAttribute(node, "element");
    const componentAttr = jsxAttribute(node, "component") ?? jsxAttribute(node, "Component");
    // JSX 形态没有 handle 约定，只认 title 属性（有的项目把菜单名写在 <Route> 上）
    const titleAttr = jsxAttribute(node, "title");

    const elementValue = elementAttr ? attributeValueNode(elementAttr) : null;
    const componentValue = componentAttr ? attributeValueNode(componentAttr) : null;

    const pageComponentId =
      resolvePageComponent(elementValue, context) ?? resolvePageComponent(componentValue, context);

    const isLayout =
      containsOutlet(elementValue) || containsOutlet(componentValue) || hasRouteChildren(node);

    computed.set(node.getStart(), fullPath);

    collector.add({
      path: fullPath,
      index: isIndex,
      framework: "react-router",
      file: relativeFile,
      loc: locOf(pathAttr ?? node),
      pageComponentId,
      isLayout,
      title: titleAttr ? literalString(attributeValueNode(titleAttr)) : null,
    });
  }
}

/* ------------------------------------------------------------------ *
 * React Router：`<Routes>` 子树内的自定义递归渲染
 *
 * 有些项目不把路由数组交给路由工厂，而是在 `<Routes>` 里调用自己的递归函数，
 * 由它 `routes.map(item => <Route ... />)`。这类路由的 `<Route>` 写在独立函数
 * 作用域里，祖先链不含 `<Routes>`，JSX 通道看不到；数组也没传给工厂，对象通道
 * 也看不到。这里只补「消费点」这一段：**必须从 `<Routes>` 子树内的调用进入**，
 * 再把实参（或函数体内 `.map` 的数据源）交回 `walkRouteObjects`，路径规则与
 * 页面组件解析完全复用，不另写一套。
 * ------------------------------------------------------------------ */

/** 从调用点到函数体的最大穿透层数（`useCallback(fn)` / `const a = fn`）。 */
const MAX_ROUTE_RENDERER_HOPS = 3;

/** 表达式 → 函数节点：解开括号、标识符声明与 `useCallback` / `useMemo` 包装。 */
function resolveRendererFunction(node: Node, hops = 0): Node | null {
  if (hops > MAX_ROUTE_RENDERER_HOPS) return null;
  if (
    MorphNode.isArrowFunction(node) ||
    MorphNode.isFunctionExpression(node) ||
    MorphNode.isFunctionDeclaration(node)
  ) {
    return node;
  }
  if (MorphNode.isParenthesizedExpression(node)) {
    return resolveRendererFunction(node.getExpression(), hops);
  }
  if (MorphNode.isIdentifier(node)) {
    // 沿符号声明回溯，不按名字猜——同名函数在不同文件里是不同实现
    let symbol = node.getSymbol();
    if (symbol?.isAlias()) {
      try {
        symbol = symbol.getAliasedSymbol();
      } catch {
        return null;
      }
    }
    for (const decl of symbol?.getDeclarations() ?? []) {
      if (MorphNode.isFunctionDeclaration(decl)) return decl;
      if (!MorphNode.isVariableDeclaration(decl)) continue;
      const init = decl.getInitializer();
      if (init) return resolveRendererFunction(init, hops + 1);
    }
    return null;
  }
  if (MorphNode.isCallExpression(node)) {
    const callee = lastTagSegment(node.getExpression().getText());
    if (callee === "useCallback" || callee === "useMemo") {
      const first = node.getArguments()[0];
      return first ? resolveRendererFunction(first, hops + 1) : null;
    }
  }
  return null;
}

/** 函数体内是否直接产出 `<Route>` 的 JSX（这是「递归渲染函数」的判据）。 */
function rendersRouteJsx(fn: Node): boolean {
  for (const element of fn.getDescendantsOfKind(SyntaxKind.JsxElement)) {
    const tag = elementTagName(element);
    if (tag !== null && lastTagSegment(tag) === "Route") return true;
  }
  for (const element of fn.getDescendantsOfKind(SyntaxKind.JsxSelfClosingElement)) {
    const tag = elementTagName(element);
    if (tag !== null && lastTagSegment(tag) === "Route") return true;
  }
  return false;
}

/**
 * 递归函数内部 `.map(...)` 的数据源中，能静态求值成数组字面量的那些。
 *
 * 覆盖 `generateRouter()` 不接参数、直接读模块级 `routes` 的写法。
 */
function routeArraySourcesIn(fn: Node): Node[] {
  const sources: Node[] = [];
  for (const call of fn.getDescendantsOfKind(SyntaxKind.CallExpression)) {
    const callee = call.getExpression();
    if (!MorphNode.isPropertyAccessExpression(callee)) continue;
    if (callee.getName() !== "map") continue;
    const receiver = callee.getExpression();
    if (resolveRouteArray(receiver)) sources.push(receiver);
  }
  return sources;
}

/**
 * 通道三：`<Routes>` 子树 → 自定义递归渲染函数 → 路由数组。
 *
 * 只从 `<Routes>` 子树内的调用进入，因此普通数据数组（菜单、面包屑、Tabs）
 * 不会被误收；找不到可求值的数据源时记 `route_renderer_unresolved`，不静默丢弃。
 */
function walkRoutesViaRenderers(
  file: SourceFile,
  relativeFile: string,
  context: TagResolveContext,
  collector: RouteCollector,
  componentsByFile: ReadonlyMap<string, readonly DiscoveredComponent[]>,
  visited: Set<string>,
): void {
  const routesElements = [
    ...file.getDescendantsOfKind(SyntaxKind.JsxElement),
    ...file.getDescendantsOfKind(SyntaxKind.JsxSelfClosingElement),
  ].filter((node) => {
    const tag = elementTagName(node);
    return tag !== null && lastTagSegment(tag) === "Routes";
  });

  for (const routesElement of routesElements) {
    for (const call of routesElement.getDescendantsOfKind(SyntaxKind.CallExpression)) {
      const fn = resolveRendererFunction(call.getExpression());
      // 只处理「确实在产出 <Route>」的函数，其它调用（hooks、条件表达式）不碰
      if (!fn || !rendersRouteJsx(fn)) continue;

      let found = false;
      const firstArg = call.getArguments()[0];
      if (firstArg && resolveRouteArray(firstArg)) {
        walkRouteObjects(firstArg, "/", context, collector, componentsByFile, visited);
        found = true;
      }
      if (!found) {
        for (const source of routeArraySourcesIn(fn)) {
          walkRouteObjects(source, "/", context, collector, componentsByFile, visited);
          found = true;
        }
      }

      if (!found) {
        collector.warnings.push({
          code: "route_renderer_unresolved",
          severity: "info",
          stage: "static",
          file: relativeFile,
          loc: locOf(call),
          message: `Routes 内的渲染函数 \`${call.getExpression().getText()}\` 找不到可静态求值的路由数组，其下路由未计入图谱`,
        });
      }
    }
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
 * Docusaurus 文件路由
 * ------------------------------------------------------------------ */

const DOCUSAURUS_CONFIG_NAMES = new Set([
  "docusaurus.config.js",
  "docusaurus.config.ts",
  "docusaurus.config.mjs",
  "docusaurus.config.cjs",
]);

const FILE_ROUTE_IGNORED_DIRS = new Set([
  "node_modules",
  "dist",
  "build",
  "out",
  "coverage",
  ".git",
  ".docusaurus",
  ".next",
  ".turbo",
]);

/** 文件路由支持的后缀；页面目录也接受 md/mdx。 */
const PAGE_FILE_PATTERN = /\.(?:tsx|jsx|ts|js|mdx?)$/i;
const DOC_FILE_PATTERN = /\.(?:mdx?)$/i;

function isDirectory(candidate: string): boolean {
  try {
    return statSync(candidate).isDirectory();
  } catch {
    return false;
  }
}

/** 找 Docusaurus 站点根（含 docusaurus.config.* 的目录）。 */
function findDocusaurusRoots(rootPath: string): string[] {
  const roots: string[] = [];
  const walk = (directory: string, depth: number): void => {
    if (depth > 8) return;
    let entries: Dirent[];
    try {
      entries = readdirSync(directory, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const fullPath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        const lower = entry.name.toLowerCase();
        if (lower.startsWith(".") || FILE_ROUTE_IGNORED_DIRS.has(lower)) continue;
        walk(fullPath, depth + 1);
      } else if (entry.isFile() && DOCUSAURUS_CONFIG_NAMES.has(entry.name.toLowerCase())) {
        roots.push(directory);
      }
    }
  };
  walk(rootPath, 0);
  return [...new Set(roots)].sort();
}

/** 页面目录里的声明文件、测试文件不产路由。 */
function isNonRouteFile(name: string): boolean {
  const lower = name.toLowerCase();
  return (
    lower.endsWith(".d.ts") ||
    /\.(?:test|spec)\.(?:tsx?|jsx?|mdx?)$/.test(lower)
  );
}

/** 列出一个文件路由目录下的页面文件，跳过 Docusaurus 的下划线/点文件与构建目录。 */
function listRouteFiles(directory: string, pattern: RegExp): string[] {
  const files: string[] = [];
  const walk = (current: string): void => {
    let entries: Dirent[];
    try {
      entries = readdirSync(current, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const lower = entry.name.toLowerCase();
      if (lower.startsWith("_") || lower.startsWith(".")) continue;
      const fullPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        if (FILE_ROUTE_IGNORED_DIRS.has(lower)) continue;
        walk(fullPath);
      } else if (entry.isFile() && pattern.test(entry.name) && !isNonRouteFile(entry.name)) {
        files.push(fullPath);
      }
    }
  };
  walk(directory);
  return files.sort();
}

/** 把 `[id]` / `[...slug]` / `[[...slug]]` 转成路由动态段。 */
function fileRouteSegment(segment: string): string | null {
  const optionalCatchAll = /^\[\[\.\.\.(.+)\]\]$/.exec(segment);
  if (optionalCatchAll) return `:${optionalCatchAll[1]}*?`;

  const catchAll = /^\[\.\.\.(.+)\]$/.exec(segment);
  if (catchAll) return `:${catchAll[1]}*`;

  const dynamic = /^\[(.+)\]$/.exec(segment);
  if (dynamic) return `:${dynamic[1]}`;

  // 不认识的方括号形态不猜，保留告警
  if (segment.includes("[") || segment.includes("]")) return null;
  return segment;
}

/**
 * 文件路径 → 路由路径。
 *
 * 规则：
 *  - 去掉扩展名；`index` 映射到所在目录（根 `index` → `/`）
 *  - 目录按层级拼接；Docusaurus docs 统一挂在 `/docs`
 *  - `[id]` → `:id`，`[...slug]` → `:slug*`，`[[...slug]]` → `:slug*?`
 */
function filePathToRoute(
  relativeToBase: string,
  basePath: string,
): { path: string; index: boolean } | null {
  const normalized = relativeToBase.replace(/\\/g, "/");
  const withoutExtension = normalized.replace(PAGE_FILE_PATTERN, "");
  const rawSegments = withoutExtension.split("/").filter((segment) => segment.length > 0);
  if (rawSegments.length === 0) return null;

  let index = false;
  if (rawSegments[rawSegments.length - 1] === "index") {
    rawSegments.pop();
    index = true;
  }

  const segments: string[] = [];
  for (const segment of rawSegments) {
    const converted = fileRouteSegment(segment);
    if (converted === null) return null;
    segments.push(converted);
  }

  const joined = [basePath, ...segments].filter((part) => part.length > 0).join("/");
  return { path: normalizePath(joined === "" ? "/" : joined), index };
}

/**
 * 给 Docusaurus 站点补文件约定路由。
 *
 * 只处理 `src/pages` 与 `docs` 的目录约定，不解析 Markdown 内容（frontmatter
 * 的 id/slug 不参与路径推导）。显式路由已占用的路径跳过并告警，不覆盖。
 */
function addDocusaurusFileRoutes(
  rootPath: string,
  collector: RouteCollector,
  componentsByFile: ReadonlyMap<string, readonly DiscoveredComponent[]>,
): void {
  const siteRoots = findDocusaurusRoots(rootPath);
  if (siteRoots.length === 0) return;

  const explicitPaths = collector.paths();
  const seenFiles = new Set<string>();
  const rootDocs = path.join(rootPath, "docs");

  for (const siteRoot of siteRoots) {
    const directories: { directory: string; basePath: string; pattern: RegExp }[] = [];
    const pagesDir = path.join(siteRoot, "src", "pages");
    if (isDirectory(pagesDir)) {
      directories.push({ directory: pagesDir, basePath: "", pattern: PAGE_FILE_PATTERN });
    }

    const siteDocs = path.join(siteRoot, "docs");
    if (isDirectory(siteDocs)) {
      directories.push({ directory: siteDocs, basePath: "/docs", pattern: DOC_FILE_PATTERN });
    }
    // Docusaurus 常把 docs 放在站点根之外（本项目就是 website + 根 docs）
    if (siteRoot !== rootPath && isDirectory(rootDocs)) {
      directories.push({ directory: rootDocs, basePath: "/docs", pattern: DOC_FILE_PATTERN });
    }

    for (const { directory, basePath, pattern } of directories) {
      for (const absolute of listRouteFiles(directory, pattern)) {
        if (seenFiles.has(absolute)) continue;
        seenFiles.add(absolute);

        const relativeFile = toProjectRelative(rootPath, absolute);
        if (relativeFile === null) continue;

        const relativeToBase = path.relative(directory, absolute);
        const parsed = filePathToRoute(relativeToBase, basePath);
        if (!parsed) {
          collector.warnings.push({
            code: "file_route_path_unresolved",
            severity: "warning",
            stage: "static",
            file: relativeFile,
            loc: { line: 1, col: 1 },
            message: `无法按目录约定推导 ${relativeFile} 的路由路径；保留文件但不产 route 节点`,
          });
          continue;
        }

        if (explicitPaths.has(parsed.path)) {
          collector.warnings.push({
            code: "file_route_shadowed",
            severity: "warning",
            stage: "static",
            file: relativeFile,
            loc: { line: 1, col: 1 },
            message: `文件路由 ${parsed.path} 已被显式路由占用；保留显式路由，不重复产节点`,
          });
          continue;
        }

        const isMarkdown = DOC_FILE_PATTERN.test(absolute);
        const pageComponent = isMarkdown ? null : pickPageComponent(relativeFile, componentsByFile);
        if (!isMarkdown && !pageComponent) {
          collector.warnings.push({
            code: "file_route_component_missing",
            severity: "warning",
            stage: "static",
            file: relativeFile,
            loc: { line: 1, col: 1 },
            message: `页面文件 ${relativeFile} 没有默认导出组件；只记录路由，不连边`,
          });
        }

        collector.add({
          path: parsed.path,
          index: parsed.index,
          framework: "docusaurus",
          file: relativeFile,
          loc: pageComponent?.loc ?? { line: 1, col: 1 },
          pageComponentId: pageComponent?.id ?? null,
          isLayout: false,
          title: null,
          fileConvention: true,
        });
      }
    }
  }
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
/** Next 的 `layout.tsx`：不产 route 节点，但按目录前缀归属到子树内的页面路由。 */
const APP_LAYOUT_PATTERN = /^(?:src\/)?app\/(.*?)layout\.(?:tsx|jsx|ts|js)$/;
const PAGES_PAGE_PATTERN = /^(?:src\/)?pages\/(.*?)\.(?:tsx|jsx|ts|js)$/;
/** Next 的 pages 目录里这些文件不是路由。 */
const PAGES_SPECIAL = new Set(["_app", "_document", "_error", "middleware"]);

export function analyzeRoutes(input: RouteAnalysisInput): RouteAnalysisResult {
  const { rootPath, project, components, definitionIndex, resolution, framework } = input;

  const componentsByFile = new Map<string, DiscoveredComponent[]>();
  for (const component of components) {
    const list = componentsByFile.get(component.file);
    if (list) list.push(component);
    else componentsByFile.set(component.file, [component]);
  }

  const collector = new RouteCollector();
  const context: TagResolveContext = { rootPath, definitionIndex, resolution };
  // 同一条路由数组可能同时被工厂通道与 Routes 渲染通道消费，展开一次即可
  const visitedArrays = new Set<string>();

  for (const file of project.getSourceFiles()) {
    const relativeFile = toProjectRelative(rootPath, file.getFilePath());
    if (relativeFile === null) continue;

    // 对象式路由表
    for (const call of file.getDescendantsOfKind(SyntaxKind.CallExpression)) {
      const callee = call.getExpression().getText();
      if (!ROUTE_FACTORIES.has(lastTagSegment(callee))) continue;
      const firstArg = call.getArguments()[0];
      if (!firstArg) continue;
      walkRouteObjects(firstArg, "/", context, collector, componentsByFile, visitedArrays);
    }

    // JSX 式路由表
    walkRouteElements(file, relativeFile, context, collector);

    // `<Routes>` 内自定义递归渲染函数消费的数据路由数组
    walkRoutesViaRenderers(file, relativeFile, context, collector, componentsByFile, visitedArrays);
  }

  if (framework === "next") {
    // 先收集 layout.tsx：`app/users/layout.tsx` 的目录前缀是 `app/users/`，
    // 以它开头的页面文件都在这个 layout 的子树里（含分组目录的原始段名，
    // 不做 URL 转换——文件约定按目录算，不按 URL 算）
    const layouts: { prefix: string; componentId: string }[] = [];
    for (const file of project.getSourceFiles()) {
      const relativeFile = toProjectRelative(rootPath, file.getFilePath());
      if (relativeFile === null || !APP_LAYOUT_PATTERN.test(relativeFile)) continue;
      const layoutComponent = pickPageComponent(relativeFile, componentsByFile);
      if (!layoutComponent) continue;
      const prefix = relativeFile.slice(0, relativeFile.lastIndexOf("/") + 1);
      layouts.push({ prefix, componentId: layoutComponent.id });
    }
    // 前缀长者排前 = 目录从深到浅（离页面最近的 layout 在最前），顺序可复现
    layouts.sort((a, b) => b.prefix.length - a.prefix.length || a.prefix.localeCompare(b.prefix));

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

      // 包住本页面的 layout：文件路径以 layout 目录为前缀（根 layout → "app/"）
      const inheritedLayoutComponentIds = layouts
        .filter((layout) => relativeFile.startsWith(layout.prefix))
        .map((layout) => layout.componentId);

      collector.add({
        path,
        index: path === "/" && rawSegments.length === 0,
        framework: "next-app",
        file: relativeFile,
        // Next 没有 path 字面量，位置取页面组件声明处；组件缺失时退回文件头
        loc: pageComponent?.loc ?? { line: 1, col: 1 },
        pageComponentId,
        isLayout: false,
        title: null,
        inheritedLayoutComponentIds,
      });
    }
  }

  // Docusaurus 等文件约定站点：显式路由收集完后再补，保证显式配置优先。
  addDocusaurusFileRoutes(rootPath, collector, componentsByFile);

  const built = collector.build();
  return { routeNodes: built.routeNodes, edges: built.edges, warnings: collector.warnings };
}
