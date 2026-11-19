/**
 * 导航边分析：把「谁把用户带到哪个页面」变成 `navigates` 边。
 *
 * ## 为什么需要它
 *
 * 没有导航边，路由表是一张**孤立的地图**：图上知道 `/users` 与 `/users/:id`
 * 存在，却不知道从哪里能到达它们。需求里的「点击列表行进入详情」这类描述，
 * 正是从页 A 指向页 B 的一条边——这是功能链路里最关键的一跳。
 *
 * ## 识别哪些写法
 *
 * 全部是框架级 API 名，不是业务名：
 *
 * | 形态 | 来源 | 边从哪出发 |
 * | --- | --- | --- |
 * | `<Link to>` / `<NavLink to>` | react-router | 该链接元素 |
 * | `<Link href>` | Next.js | 该链接元素 |
 * | `<Navigate to>` / `<Redirect to>` | react-router | 所属组件 |
 * | `navigate("/x")` | `useNavigate()` 的返回值 | 所属组件 |
 * | `router.push("/x")` / `.replace` | `useRouter()` 的返回值 | 所属组件 |
 * | `redirect("/x")` | react-router loader / Next | 所在函数所属组件 |
 *
 * 三处细节保证「不是猜的」：
 *  - `useNavigate` / `useRouter` / `redirect` 都要确认来自 router 包
 *    （`react-router*` / `next/*` / `@remix-run/react`），项目自造的同名函数不算；
 *  - `navigate` 与 `router` 是变量，必须回溯到那三个 hook 的调用结果；
 *  - `<Navigate>` 这类声明式组件不渲染任何东西，它表达的是「渲染到这里就跳」，
 *    所以边从组件出发，而不是虚构一个视图元素。
 *
 * ## 路径怎么对上 route 节点
 *
 * 字面量路径先精确匹配，再按段匹配动态路由（`/users/1` ↔ `/users/:id`）。
 * 模板插值（`` `/users/${id}` ``）按「插值处可匹配任意段」处理——它确定到
 * 段数级别，只是段值不定，置信度降一档。
 *
 * 拿不到路径（变量、函数返回值）时不产边，记 `navigate_target_unresolved`；
 * 路径确定但路由表里没有对应节点（外部地址、动态生成的路径）时不产边，
 * 记 `navigate_route_missing`。两者都不编造。
 *
 * ## 去重
 *
 * 同一 (源, 目标路由) 只产一条边，多处跳转合并证据——和 requests 边同一口径：
 * 「A 能到 B」是一个事实，不管是几个按钮触发的。
 */

import type { Node } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { Evidence, IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";
import { findImportSource } from "./jsx-refs.js";
import type { JsxInventory } from "./jsx-inventory.js";
import { buildOwnerIndex, findOwner } from "./ownership.js";
import { normalizePath } from "./routes.js";

/** 路径是字面量、且与 route 节点精确对应：符号与字面量都确定。 */
const LITERAL_CONFIDENCE = 0.9;
/** 路径含插值或需要按模式匹配动态路由：段数确定，段值不定。 */
const PATTERN_CONFIDENCE = 0.8;

/**
 * 导航 API 所属的包。以 `包名` 或 `包名/子路径` 命中。
 *
 * 只收路由库本身，不含任何业务封装（`@/utils/router` 之类）。业务封装由
 * `mapping.json` 声明，不在这里硬编码。
 */
const ROUTER_PACKAGES: readonly string[] = [
  "react-router",
  "react-router-dom",
  "next/navigation",
  "next/router",
  "@remix-run/react",
  "@tanstack/react-router",
];

/** 产生 navigate 函数的 hook。 */
const NAVIGATE_HOOKS: ReadonlySet<string> = new Set(["useNavigate"]);

/** 产生 router 对象的 hook（Next.js / react-router v7 的 data router）。 */
const ROUTER_HOOKS: ReadonlySet<string> = new Set(["useRouter"]);

/** router 对象上的跳转方法。 */
const ROUTER_PUSH_METHODS: ReadonlySet<string> = new Set(["push", "replace"]);

/** 函数式跳转：`redirect("/x")`。 */
const REDIRECT_FUNCTIONS: ReadonlySet<string> = new Set(["redirect"]);

/** 链接组件：会渲染出可点击元素的（边从元素出发）。 */
const LINK_COMPONENTS: ReadonlySet<string> = new Set(["Link", "NavLink"]);

/** 声明式跳转组件：不渲染任何东西（边从组件出发）。 */
const DECLARATIVE_NAVIGATE_COMPONENTS: ReadonlySet<string> = new Set([
  "Navigate",
  "Redirect",
]);

/** 取路径的属性名：react-router 用 `to`，Next.js 用 `href`。 */
const PATH_ATTRIBUTES: readonly string[] = ["to", "href"];

/** `Link` 等组件取路径的属性名（与上面一致，单列是为了让语义清楚）。 */
const LINK_PATH_ATTRIBUTES: readonly string[] = ["to", "href"];

export interface NavigationAnalysisResult {
  /** 元素 ─navigates→ route */
  edges: IREdge[];
  warnings: Warning[];
}

export interface NavigationAnalysisInput {
  rootPath: string;
  /** 统一元素清单：`<Link>` 的 id 与视图层/事件层同源 */
  inventory: JsxInventory;
  /** 已存在的元素节点 id（视图层 + 事件层产出的）；不在其中的元素不能当边起点 */
  elementNodeIds: ReadonlySet<string>;
  /** 路由节点：路径从 attrs.path 取 */
  routeNodes: readonly IRNode[];
  components: readonly DiscoveredComponent[];
  declarations: ReadonlyMap<string, Node>;
}

/** 已解析的跳转目标。 */
interface NavigationTarget {
  /**
   * 路径分段。`null` 表示这一段的值定不下来（模板插值），但段数与位置是确定的
   * ——这正是匹配动态路由（`/users/:id`）需要的信息。
   */
  segments: (string | null)[];
  /** 是否含插值（段值不定，只能按模式匹配） */
  pattern: boolean;
}

/**
 * 模板里的插值占位符。
 *
 * 用 NUL 而不用 `*`：`*` 在路径里是合法字符（`/files/*.txt`），撞上会把字面量
 * 误判成插值。NUL 不可能出现在源码字符串里。
 */
const PLACEHOLDER = "\u0000";

/* ------------------------------------------------------------------ *
 * 路径解析
 * ------------------------------------------------------------------ */

/** 路径文本 → 分段；含占位符的段记为 null，其余取字面量。 */
function toSegments(raw: string): (string | null)[] {
  const withoutQuery = raw.split(/[?#]/)[0] ?? raw;
  return withoutQuery
    .split("/")
    .filter((segment) => segment.length > 0)
    // 段里只要含插值，这一段的实测值就定不下来；段本身仍然占位
    .map((segment) => (segment.includes(PLACEHOLDER) ? null : segment));
}

/**
 * 从表达式里读出跳转路径。
 *
 * 认三种形态：字面量、无插值模板、带插值模板。带插值时把插值所在的**段**记为
 * null——`/users/${id}` 确定到「两段，第二段是变量」，足以匹配 `/users/:userId`。
 *
 * 对象形态（`to={{ pathname: "/x" }}`）取 `pathname`。
 */
/**
 * 常量回溯的最大层数。
 *
 * 常量表 → 字段/索引 → map 回调参数 → 字面量会串起多次函数调用，层数过浅会
 * 在真正读到字面量之前就截断。六层足以覆盖这些静态形态，同时防止自引用/循环
 * 导入不收敛；再多就不属于「可在编译期确定」的常量了。
 */
const MAX_CONSTANT_DEPTH = 6;

/**
 * 把标识符 / 属性访问追溯到它指向的字符串字面量。
 *
 * `to={ROUTES.HOME}` 里的 `ROUTES.HOME` 在源码中不含任何路径文本，硬看字面量
 * 只能得到「不是字面量」。但它的值在编译期是完全确定的：沿符号声明找到
 * `HOME: "/"` 就能读出来。这正是 TypeScript 自己的解析结果，不是按名字猜。
 *
 * 退化为「按名字匹配」的做法（用 `HOME` 去猜 `/`）是错的：同名常量在不同
 * 模块里可以有不同值，而且猜出来的路径会直接进图。
 */
function resolveAliasedSymbol(node: Node) {
  let symbol = node.getSymbol();
  if (symbol?.isAlias()) {
    try {
      symbol = symbol.getAliasedSymbol();
    } catch {
      // 别名解析失败（依赖未安装）时保留原符号，下面按「未命中」处理
    }
  }
  return symbol;
}

/** 去掉不改变运行时值的包装表达式。 */
function unwrapStaticExpression(node: Node): Node {
  let current = node;
  while (
    MorphNode.isParenthesizedExpression(current) ||
    MorphNode.isAsExpression(current) ||
    MorphNode.isSatisfiesExpression(current)
  ) {
    current = current.getExpression();
  }
  return current;
}

/** 标识符是否指向函数参数（例如 map 回调参数）。 */
function isParameterReference(node: Node): boolean {
  if (!MorphNode.isIdentifier(node)) return false;
  return (resolveAliasedSymbol(node)?.getDeclarations() ?? []).some((decl) =>
    MorphNode.isParameterDeclaration(decl),
  );
}

/**
 * 把可在编译期确定值的表达式收窄到真正的字面量节点。
 *
 * 支持：
 *  - `const X = "/x"` / `const X = OBJECT.field` / `OBJECT.field`
 *  - `ARRAY[0]`
 *  - `as const` / `satisfies` / 括号
 *
 * 不支持函数调用、条件表达式、map 回调参数本身等无法静态确定的形态；返回 null。
 */
function resolveStaticNode(node: Node | null, depth: number): Node | null {
  if (!node || depth > MAX_CONSTANT_DEPTH) return null;
  const current = unwrapStaticExpression(node);

  if (MorphNode.isIdentifier(current)) {
    for (const decl of resolveAliasedSymbol(current)?.getDeclarations() ?? []) {
      if (MorphNode.isVariableDeclaration(decl) || MorphNode.isPropertyAssignment(decl)) {
        const initializer = decl.getInitializer();
        if (initializer) return resolveStaticNode(initializer, depth + 1);
      }
    }
    return null;
  }

  if (MorphNode.isPropertyAccessExpression(current)) {
    // `section.to`：section 是 map 回调参数，不能从参数声明里读值。
    // 交给 readMappedTargets 按数组元素逐个求值；这里直接判为未命中。
    if (isParameterReference(current.getExpression())) return null;

    for (const decl of resolveAliasedSymbol(current)?.getDeclarations() ?? []) {
      if (MorphNode.isVariableDeclaration(decl) || MorphNode.isPropertyAssignment(decl)) {
        const initializer = decl.getInitializer();
        if (initializer) return resolveStaticNode(initializer, depth + 1);
      }
    }

    // 对象字面量字段：`({ path: "/x" }).path`
    const base = resolveStaticNode(current.getExpression(), depth + 1);
    if (base && MorphNode.isObjectLiteralExpression(base)) {
      const property = base.getProperty(current.getName());
      if (property && MorphNode.isPropertyAssignment(property)) {
        const initializer = property.getInitializer();
        if (initializer) return resolveStaticNode(initializer, depth + 1);
      }
      if (property && MorphNode.isShorthandPropertyAssignment(property)) {
        return resolveStaticNode(property.getNameNode(), depth + 1);
      }
    }
    return null;
  }

  if (MorphNode.isElementAccessExpression(current)) {
    const base = resolveStaticNode(current.getExpression(), depth + 1);
    const argument = current.getArgumentExpression();
    if (
      base &&
      MorphNode.isArrayLiteralExpression(base) &&
      argument &&
      MorphNode.isNumericLiteral(argument)
    ) {
      const index = Number(argument.getLiteralText());
      const element = base.getElements()[index];
      if (element) return resolveStaticNode(element, depth + 1);
    }
    return null;
  }

  return current;
}

/**
 * 读出完全静态的字符串值。
 *
 * 只处理字面量、静态模板、字面量 `+` 拼接和可回溯的常量；任何一段定不下来
 * 都返回 null，由调用方保留「目标未解析」告警。
 */
function readStaticString(node: Node | null, depth = 0): string | null {
  if (!node || depth > MAX_CONSTANT_DEPTH) return null;
  const current = unwrapStaticExpression(node);

  if (MorphNode.isStringLiteral(current) || MorphNode.isNoSubstitutionTemplateLiteral(current)) {
    return current.getLiteralText();
  }

  if (MorphNode.isTemplateExpression(current)) {
    let text = current.getHead().getLiteralText();
    for (const span of current.getTemplateSpans()) {
      const part = readStaticString(span.getExpression(), depth + 1);
      if (part === null) return null;
      text += `${part}${span.getLiteral().getLiteralText()}`;
    }
    return text;
  }

  if (
    MorphNode.isBinaryExpression(current) &&
    current.getOperatorToken().getKind() === SyntaxKind.PlusToken
  ) {
    const left = readStaticString(current.getLeft(), depth + 1);
    const right = readStaticString(current.getRight(), depth + 1);
    return left !== null && right !== null ? `${left}${right}` : null;
  }

  if (
    MorphNode.isIdentifier(current) ||
    MorphNode.isPropertyAccessExpression(current) ||
    MorphNode.isElementAccessExpression(current)
  ) {
    const resolved = resolveStaticNode(current, depth + 1);
    if (resolved && resolved !== current) return readStaticString(resolved, depth + 1);
  }

  return null;
}

function readConstantString(node: Node, depth: number): NavigationTarget | null {
  if (depth > MAX_CONSTANT_DEPTH) return null;
  if (
    MorphNode.isIdentifier(node) ||
    MorphNode.isPropertyAccessExpression(node) ||
    MorphNode.isElementAccessExpression(node)
  ) {
    const resolved = resolveStaticNode(node, depth + 1);
    if (resolved && resolved !== node) return readTargetPath(resolved, depth + 1);
  }
  return null;
}

function readTargetPath(node: Node | null, depth = 0): NavigationTarget | null {
  if (!node || depth > MAX_CONSTANT_DEPTH) return null;

  const current = unwrapStaticExpression(node);

  // `to={{ pathname: "/users/1", search: "?a=1" }}`
  if (MorphNode.isObjectLiteralExpression(current)) {
    const pathname = current.getProperty("pathname");
    if (!pathname || !MorphNode.isPropertyAssignment(pathname)) return null;
    return readTargetPath(pathname.getInitializer() ?? null, depth + 1);
  }

  const staticString = readStaticString(current, depth + 1);
  if (staticString !== null) {
    // 空字符串是「当前路由」而非根路径，两者语义不同，不能一概按 "/" 处理
    if ((staticString.split(/[?#]/)[0] ?? staticString).length === 0) return null;
    // 根路径 "/" 的分段为空数组，这是合法的目标，必须保留
    return { segments: toSegments(staticString), pattern: false };
  }

  if (MorphNode.isTemplateExpression(current)) {
    // 首个字面量块必须给出起始段：`${base}/x` 连从哪一节开始都定不下来
    const head = current.getHead().getLiteralText();
    if (!head.includes("/")) return null;

    let text = head;
    for (const span of current.getTemplateSpans()) {
      const resolved = readStaticString(span.getExpression(), depth + 1);
      text += resolved !== null ? resolved : PLACEHOLDER;
      text += span.getLiteral().getLiteralText();
    }

    const segments = toSegments(text);
    if (segments.length === 0) return null;
    return { segments, pattern: true };
  }

  return readConstantString(current, depth);
}

/** 读取模块级常量里的数组元素；只接受真正的数组字面量。 */
function readStaticArrayElements(node: Node | null, depth: number): Node[] | null {
  const resolved = resolveStaticNode(node, depth + 1);
  if (!resolved || !MorphNode.isArrayLiteralExpression(resolved)) return null;
  return resolved.getElements();
}

/** `array.map((item) => ...)` 里的 item 参数，回溯到 array 字面量元素。 */
function mapParameterElements(identifier: Node, depth: number): Node[] | null {
  if (!MorphNode.isIdentifier(identifier)) return null;

  for (const decl of resolveAliasedSymbol(identifier)?.getDeclarations() ?? []) {
    if (!MorphNode.isParameterDeclaration(decl)) continue;
    const fn = decl.getParent();
    if (!MorphNode.isArrowFunction(fn) && !MorphNode.isFunctionExpression(fn)) continue;

    let call: Node | undefined = fn.getParent();
    while (call && MorphNode.isParenthesizedExpression(call)) call = call.getParent();
    if (!call || !MorphNode.isCallExpression(call)) continue;

    const callee = call.getExpression();
    if (!MorphNode.isPropertyAccessExpression(callee) || callee.getName() !== "map") continue;

    const elements = readStaticArrayElements(callee.getExpression(), depth + 1);
    if (elements && elements.length > 0) return elements;
  }

  return null;
}

/** 从对象字面量里取字段初始值；不是对象或字段定不下来时返回 null。 */
function readObjectPropertyValue(node: Node, propertyName: string, depth: number): Node | null {
  const resolved = resolveStaticNode(node, depth + 1);
  if (!resolved || !MorphNode.isObjectLiteralExpression(resolved)) return null;

  const property = resolved.getProperty(propertyName);
  if (!property) return null;
  if (MorphNode.isPropertyAssignment(property)) return property.getInitializer() ?? null;
  if (MorphNode.isShorthandPropertyAssignment(property)) return property.getNameNode();
  return null;
}

/**
 * `sections.map((section) => <Link to={section.to}>...)`：
 * 按数组元素逐个求值，返回全部目标；任何一个元素求不出来就整体放弃，
 * 避免只连接一部分目标却让调用方以为已完整解析。
 */
function readMappedTargets(node: Node, depth: number): NavigationTarget[] | null {
  if (depth > MAX_CONSTANT_DEPTH || !MorphNode.isPropertyAccessExpression(node)) return null;
  const base = node.getExpression();
  if (!MorphNode.isIdentifier(base)) return null;

  const elements = mapParameterElements(base, depth + 1);
  if (!elements) return null;

  const targets: NavigationTarget[] = [];
  for (const element of elements) {
    const value = readObjectPropertyValue(element, node.getName(), depth + 1);
    const target = readTargetPath(value, depth + 1);
    if (!target) return null;
    targets.push(target);
  }
  return targets.length > 0 ? targets : null;
}

/** 入口：先按 map 映射表展开，再走普通单目标解析。 */
function readTargetPaths(node: Node | null, depth = 0): NavigationTarget[] | null {
  if (!node) return null;
  const mapped = readMappedTargets(node, depth);
  if (mapped) return mapped;
  const single = readTargetPath(node, depth);
  return single ? [single] : null;
}

/** 一个跳转点：从哪、到哪、证据在哪。 */
interface NavigationSite {
  fromId: string;
  target: NavigationTarget;
  file: string;
  loc: { line: number; col: number };
}

/* ------------------------------------------------------------------ *
 * 路径解析
 * ------------------------------------------------------------------ */

/** 取 JSX 属性值节点（`to="x"` / `to={"x"}` 都收窄到值本身）。 */
function jsxAttributeValue(opening: Node, names: readonly string[]): Node | null {
  if (!MorphNode.isJsxOpeningElement(opening) && !MorphNode.isJsxSelfClosingElement(opening)) {
    return null;
  }
  for (const attr of opening.getAttributes()) {
    if (!MorphNode.isJsxAttribute(attr)) continue;
    if (!names.includes(attr.getNameNode().getText())) continue;
    const initializer = attr.getInitializer();
    if (!initializer) continue;
    if (MorphNode.isJsxExpression(initializer)) return initializer.getExpression() ?? null;
    return initializer;
  }
  return null;
}

/* ------------------------------------------------------------------ *
 * 路由索引
 * ------------------------------------------------------------------ */

/** 路径 → 规范化文本（用于精确匹配与告警文案）。 */
function segmentsToPath(segments: readonly (string | null)[]): string {
  return normalizePath(`/${segments.map((segment) => segment ?? "*").join("/")}`);
}

/* ------------------------------------------------------------------ *
 * 路由索引
 * ------------------------------------------------------------------ */

/** 路由段：`null` 表示动态段（`/users/:id` 的第二段）。 */
type RouteSegment = string | null;

interface RouteIndex {
  /** 精确路径 → route 节点 id 列表（同一路径可能被多处声明） */
  exact: ReadonlyMap<string, readonly string[]>;
  /** 含动态段的路由 */
  dynamic: readonly { segments: readonly RouteSegment[]; nodeId: string }[];
}

function splitSegments(path: string): string[] {
  return path.split("/").filter((segment) => segment.length > 0);
}

function buildRouteIndex(routeNodes: readonly IRNode[]): RouteIndex {
  const exact = new Map<string, string[]>();
  const dynamic: { segments: RouteSegment[]; nodeId: string }[] = [];

  for (const node of routeNodes) {
    const path = typeof node.attrs.path === "string" ? node.attrs.path : null;
    if (!path) continue;

    const raw = splitSegments(path);
    // `:id` / `:slug*` / `:slug*?` 都是动态段；修饰符不影响匹配
    const segments = raw.map((segment) =>
      segment.startsWith(":") ? null : segment,
    );

    if (segments.some((segment) => segment === null)) {
      dynamic.push({ segments, nodeId: node.id });
      continue;
    }

    const key = normalizePath(path);
    const list = exact.get(key);
    if (list) list.push(node.id);
    else exact.set(key, [node.id]);
  }

  return { exact, dynamic };
}

/**
 * 把跳转路径对上 route 节点。
 *
 * 先精确匹配（最好情况，两者都确定），再按段匹配动态路由。段匹配要求段数一致：
 * 路由侧的动态段匹配任意段，跳转侧的 `null` 段（模板插值）也匹配任意段。
 *
 * 返回全部命中：同一路径被两个文件声明过时，两个都连——那是真实的歧义，
 * 由 routes 分析器告警，导航边不该替它做选择。
 */
function matchRoutes(index: RouteIndex, target: NavigationTarget): string[] {
  if (!target.pattern) {
    const exactHit = index.exact.get(segmentsToPath(target.segments));
    if (exactHit && exactHit.length > 0) return [...exactHit];
  }

  const hits: string[] = [];
  for (const route of index.dynamic) {
    if (route.segments.length !== target.segments.length) continue;
    const matched = route.segments.every((segment, position) => {
      // 路由侧是动态段：处处匹配
      if (segment === null) return true;
      // 跳转侧是插值段：段值定不下来，也按匹配处理
      const targetSegment = target.segments[position];
      return targetSegment === undefined || targetSegment === null || targetSegment === segment;
    });
    if (matched) hits.push(route.nodeId);
  }
  return hits;
}

/* ------------------------------------------------------------------ *
 * 识别
 * ------------------------------------------------------------------ */

/** 文件里登记的、由 router hook 产生的变量名。 */
interface RouterBindings {
  /** useNavigate() 的返回值变量名 */
  navigators: ReadonlySet<string>;
  /** useRouter() 的返回值变量名 */
  routers: ReadonlySet<string>;
}

/** 导入是否来自受控的路由包。 */
function isFromRouterPackage(sourceFile: Node, localName: string): boolean {
  const moduleSpecifier = findImportSource(sourceFile.getSourceFile(), localName);
  if (!moduleSpecifier) return false;
  return ROUTER_PACKAGES.some(
    (pkg) => moduleSpecifier === pkg || moduleSpecifier.startsWith(`${pkg}/`),
  );
}

/** 收集文件内 `const x = useNavigate()` / `const r = useRouter()`。 */
function readRouterBindings(file: Node): RouterBindings {
  const navigators = new Set<string>();
  const routers = new Set<string>();

  for (const decl of file.getSourceFile().getDescendantsOfKind(SyntaxKind.VariableDeclaration)) {
    const init = decl.getInitializer();
    if (!init || !MorphNode.isCallExpression(init)) continue;

    const callee = init.getExpression();
    if (!MorphNode.isIdentifier(callee)) continue;
    const name = callee.getText();

    if (NAVIGATE_HOOKS.has(name) && isFromRouterPackage(callee, name)) {
      navigators.add(decl.getName());
    } else if (ROUTER_HOOKS.has(name) && isFromRouterPackage(callee, name)) {
      routers.add(decl.getName());
    }
  }

  return { navigators, routers };
}

/**
 * 一次调用是不是跳转，返回跳转路径。
 *
 * 三种形态：
 *  - `navigate("/x")`：标识符名在本文件的 navigators 里
 *  - `router.push("/x")`：属性访问，根名在 routers 里，方法名在 push/replace
 *  - `redirect("/x")`：标识符来自 router 包
 */
function readNavigationCall(
  call: Node,
  bindings: RouterBindings,
): Node | null {
  if (!MorphNode.isCallExpression(call)) return null;
  const expr = call.getExpression();
  const [first] = call.getArguments();
  if (!first) return null;

  if (MorphNode.isPropertyAccessExpression(expr)) {
    // router.push / router.replace
    const root = expr.getExpression();
    if (!MorphNode.isIdentifier(root)) return null;
    if (!bindings.routers.has(root.getText())) return null;
    if (!ROUTER_PUSH_METHODS.has(expr.getName())) return null;
    return first;
  }

  if (!MorphNode.isIdentifier(expr)) return null;
  const name = expr.getText();

  if (bindings.navigators.has(name)) return first;
  if (REDIRECT_FUNCTIONS.has(name) && isFromRouterPackage(expr, name)) return first;

  return null;
}

/* ------------------------------------------------------------------ *
 * 入口
 * ------------------------------------------------------------------ */

export function analyzeNavigation(input: NavigationAnalysisInput): NavigationAnalysisResult {
  const { rootPath, inventory, elementNodeIds, routeNodes, components, declarations } = input;

  const warnings: Warning[] = [];
  const edges = new Map<string, IREdge>();
  const routeIndex = buildRouteIndex(routeNodes);
  const ownerIndex = buildOwnerIndex(components, declarations);

  const sites: NavigationSite[] = [];

  /** 记录一次跳转；目标元素不存在时降级到所属组件。 */
  const record = (site: NavigationSite): void => {
    sites.push(site);
  };

  // --- 一次扫描：JSX 形态 ---
  for (const [file, entries] of inventory.byFile) {
    const sourceFile = entries[0]?.opening.getSourceFile();
    if (!sourceFile) continue;
    const bindings = readRouterBindings(sourceFile);

    for (const info of entries) {
      const opening = info.opening;
      const last = info.tag.split(".").pop() ?? info.tag;

      const isLink = LINK_COMPONENTS.has(last);
      const isDeclarative = DECLARATIVE_NAVIGATE_COMPONENTS.has(last);
      if (!isLink && !isDeclarative) continue;

      const pathNode = jsxAttributeValue(
        opening,
        isLink ? LINK_PATH_ATTRIBUTES : PATH_ATTRIBUTES,
      );
      const targets = readTargetPaths(pathNode);

      if (!targets) {
        // 属性存在但定不下来 / 属性不存在（Link 不写 to 是错误用法）
        if (pathNode !== null || isLink) {
          warnings.push({
            code: "navigate_target_unresolved",
            severity: "info",
            stage: "static",
            file,
            loc: info.loc,
            message: `<${info.tag}> 的跳转路径不是字面量，无法确定目标页面`,
          });
        }
        continue;
      }

      // 链接元素在视图层里就是那个可点击元素，边从它出发；
      // 声明式跳转不渲染任何东西，边从所属组件出发
      let fromId = info.componentId;
      if (isLink) {
        if (elementNodeIds.has(info.id)) fromId = info.id;
        else warnings.push({
          code: "navigate_source_not_in_view",
          severity: "info",
          stage: "static",
          file,
          loc: info.loc,
          message: `<${info.tag}> 未进视图层，跳转边改为从所属组件 ${info.componentId} 出发`,
        });
      }

      for (const target of targets) {
        record({ fromId, target, file, loc: info.loc });
      }
    }

    // --- 一次扫描：调用形态（每文件一次，与上面的元素遍历分开）---
    for (const call of sourceFile.getDescendantsOfKind(SyntaxKind.CallExpression)) {
      const pathNode = readNavigationCall(call, bindings);
      if (!pathNode) continue;

      const targets = readTargetPaths(pathNode);
      if (!targets) {
        const pos = sourceFile.getLineAndColumnAtPos(call.getStart());
        warnings.push({
          code: "navigate_target_unresolved",
          severity: "info",
          stage: "static",
          file,
          loc: { line: pos.line, col: pos.column },
          message: "跳转路径不是字面量，无法确定目标页面",
        });
        continue;
      }

      const fromId = findOwner(call, ownerIndex, rootPath);
      if (!fromId) continue; // 模块顶层的调用不属于任何组件，无法作为边的起点

      const pos = sourceFile.getLineAndColumnAtPos(call.getStart());
      for (const target of targets) {
        record({ fromId, target, file, loc: { line: pos.line, col: pos.column } });
      }
    }
  }

  // --- 落边 ---
  for (const site of sites) {
    const targets = matchRoutes(routeIndex, site.target);
    if (targets.length === 0) {
      warnings.push({
        code: "navigate_route_missing",
        severity: "info",
        stage: "static",
        file: site.file,
        loc: site.loc,
        message: `跳转目标 ${segmentsToPath(site.target.segments)} 在路由表里没有对应节点；可能是外部地址或动态生成的路径`,
      });
      continue;
    }

    const confidence = site.target.pattern ? PATTERN_CONFIDENCE : LITERAL_CONFIDENCE;
    const evidence: Evidence = {
      source: "static",
      confidence,
      file: site.file,
      loc: site.loc,
    };

    for (const routeNodeId of targets) {
      const edgeId = `e_navigates_${site.fromId}__${routeNodeId}`;
      const existing = edges.get(edgeId);
      if (existing) {
        // 同一对 (源, 路由) 多处跳转：一条边合并证据
        existing.evidence.push(evidence);
        continue;
      }
      edges.set(edgeId, {
        id: edgeId,
        from: site.fromId,
        to: routeNodeId,
        kind: "navigates",
        evidence: [evidence],
      });
    }
  }

  return { edges: [...edges.values()], warnings };
}
