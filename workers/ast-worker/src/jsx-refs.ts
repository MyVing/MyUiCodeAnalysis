/**
 * JSX 标签 → 组件身份的解析。
 *
 * 「这个标签指向哪个组件」是 renders 边与路由节点共用的同一个问题：
 *  - renders：`<TableCard />` 出现在谁的函数体里，指向谁；
 *  - routes：`<Route element={<UserList />} />` 里的 `UserList` 是哪个组件。
 *
 * 两处各写一份会分叉——同一段代码被算到不同组件上，而这类分叉在图上表现为
 * "边凭空多了/少了"，很难归因。因此这里只保留一份解析逻辑。
 *
 * 本模块不关心"谁渲染了谁"（那是 renders.ts 的归属判定），也不关心路由路径拼接
 * （那是 routes.ts 的事）。它只回答一个问题：给定一个标签名节点，它指向什么。
 */

import type { Node, SourceFile } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { DiscoveredComponent } from "./components.js";
import { canonicalId } from "./ids.js";
import { getPackageName, toProjectRelative } from "./paths.js";
import { positionKey } from "./positions.js";
import type { ResolutionIndex } from "./resolver.js";

/**
 * 判断标签是不是宿主元素。
 *
 * 依据是 JSX 的编译语义而非名字猜测：首字母小写的标签编译成字符串字面量，
 * 一定是 DOM 元素。点号形式（`Antd.Button`）以最后一段的首字母为准。
 */
export function isHostElement(tagName: string): boolean {
  // Fragment 是语法结构，不产生渲染边
  if (tagName === "React.Fragment" || tagName === "Fragment") return true;
  const last = tagName.split(".").pop() ?? tagName;
  return !/^[A-Z]/.test(last);
}

/** 标签的解析结果。 */
export type TagTarget =
  | { kind: "component"; id: string }
  | { kind: "external"; nodeId: string; packageName: string; tagName: string }
  | { kind: "unresolved"; localComponent?: boolean };

/**
 * 多分支标签（`const Comp = cond ? Slot : "button"`）的展开深度上限。
 *
 * 只服务于「沿声明回溯初始值」，三元 / 逻辑运算各算一层。三层足够覆盖
 * slot 组件这种一层的写法；再深就不是可以静态确定的东西了。
 */
const MAX_TAG_BRANCH_DEPTH = 3;

/** 解析所需的上下文，由调用方从分析流程里透传。 */
export interface TagResolveContext {
  rootPath: string;
  /** 定义位置键（见 positions.ts）→ 组件 id，由 discoverComponents 产出 */
  definitionIndex: ReadonlyMap<string, string>;
  resolution: ResolutionIndex;
}

/**
 * 在上文里做具体的解析，供 resolveTagTarget 与 resolveIdentifierTarget 共用。
 *
 * `tagNameNode` 既可以是 `JsxTagName`，也可以是普通 `Identifier`——两者都能
 * 取到符号声明位置，这正是 `Component={UserList}` 这类路由写法需要的。
 */
function resolveSymbolTarget(tagNameNode: Node, context: TagResolveContext): TagTarget {
  const { rootPath, definitionIndex } = context;
  const tagName = tagNameNode.getText();

  // 先从标签符号取声明位置，直接命中项目内组件。
  // 这一步不经过"按名字找"——拿到的是 TypeScript 自己解析出的声明。
  let symbol = tagNameNode.getSymbol();
  if (symbol?.isAlias()) {
    try {
      symbol = symbol.getAliasedSymbol();
    } catch {
      // 别名解析失败（依赖未安装）时保留原符号，下面按"未命中"处理
    }
  }

  for (const decl of symbol?.getDeclarations() ?? []) {
    const declFile = toProjectRelative(rootPath, decl.getSourceFile().getFilePath());
    if (declFile === null) continue;
    const hit = definitionIndex.get(positionKey(declFile, decl.getStart()));
    if (hit) return { kind: "component", id: hit };
  }

  // 只有标识符 / 命名空间属性可能对应 import；表达式候选（如 memo(helper)）不能按文本猜包来源。
  if (!MorphNode.isIdentifier(tagNameNode) && !MorphNode.isPropertyAccessExpression(tagNameNode)) {
    return { kind: "unresolved" };
  }

  // 未命中：用 resolver 的既有结论判断来源，不在这里重新实现一遍判断规则
  const sourceFile = tagNameNode.getSourceFile();
  const localDeclarations = symbol?.getDeclarations() ?? [];
  const localComponent = localDeclarations.some((decl) =>
    decl.getSourceFile() === sourceFile && isJsxFunctionDeclaration(decl),
  );
  const localBinding = localDeclarations.some((decl) => decl.getSourceFile() === sourceFile);
  // 本地声明优先于同名命名空间 import；非 JSX 本地值不是外部组件。
  if (localBinding && !localComponent) return { kind: "unresolved" };
  const importSource = findImportSource(sourceFile, tagName);
  if (!importSource) {
    return localComponent ? { kind: "unresolved", localComponent: true } : { kind: "unresolved" };
  }

  const target = context.resolution.resolve(sourceFile, importSource);

  if (target.status === "internal") {
    const importedName = importedNameOf(sourceFile, tagName) ?? tagName;
    const exported = target.sourceFile.getExportedDeclarations().get(importedName) ?? [];
    for (const decl of exported) {
      const component = componentIdentityOf(decl, rootPath);
      if (component) return { kind: "component", id: component };
    }

    const localComponent = (symbol?.getDeclarations() ?? []).some((decl) =>
      decl.getSourceFile() === sourceFile && isJsxFunctionDeclaration(decl),
    );
    // 本地未导出组件属于建模边界；不创建目标节点或 renders 边。
    return localComponent ? { kind: "unresolved", localComponent: true } : { kind: "unresolved" };
  }

  // external（已安装）、builtin（Node 内置）与 unresolved 且非本地（未安装的裸包名）
  // 都归为外部引用；asset 不是模块，无法指向组件，按未解析处理
  const packageName =
    target.status === "external"
      ? target.packageName
      : target.status === "builtin"
        ? target.packageName
        : target.status === "unresolved" && !target.isLocal
          ? getPackageName(importSource)
          : null;

  if (!packageName) return { kind: "unresolved" };

  return {
    kind: "external",
    nodeId: canonicalId(`${packageName}#${tagName}`),
    packageName,
    tagName,
  };
}

/** 解析 JSX 标签指向的组件身份。 */
export function resolveTagTarget(
  tagNameNode: Node,
  context: TagResolveContext,
): TagTarget {
  return resolveSymbolTarget(tagNameNode, context);
}

/**
 * 解析一个标识符指向的组件身份（用于 `Component={UserList}` 这类非 JSX 写法）。
 *
 * 与 JSX 标签走同一条符号解析路径：`Component={UserList}` 与 `<UserList />`
 * 指向的是同一个声明，没有理由得出不同结论。
 */
export function resolveIdentifierTarget(
  identifier: Node,
  context: TagResolveContext,
): TagTarget {
  return resolveSymbolTarget(identifier, context);
}

/**
 * 从「形如 `<X />` 或 `<X>...</X>` 的 JSX 节点」里取出标签名节点。
 *
 * 返回 null 说明传进来的不是 JSX 元素（如 Fragment 或普通表达式），
 * 调用方据此判断"这个 element 属性不是组件的直接写法"。
 */
export function tagNameOfJsxElement(node: Node): Node | null {
  if (MorphNode.isJsxSelfClosingElement(node)) return node.getTagNameNode();
  if (MorphNode.isJsxElement(node)) return node.getOpeningElement().getTagNameNode();
  return null;
}

function importedNameOf(sourceFile: SourceFile, localName: string): string | null {
  const rootName = localName.split(".")[0] ?? localName;
  for (const imp of sourceFile.getImportDeclarations()) {
    if (imp.getDefaultImport()?.getText() === rootName) return "default";
    for (const named of imp.getNamedImports()) {
      if (named.getAliasNode()?.getText() === rootName) return named.getName();
      if (named.getName() === rootName) return named.getName();
    }
  }
  return null;
}

const MAX_COMPONENT_UNWRAP_DEPTH = 8;

function resolveJsxFunction(
  node: Node,
  visited: Set<Node> = new Set(),
  depth = 0,
): Node | null {
  if (depth > MAX_COMPONENT_UNWRAP_DEPTH || visited.has(node)) return null;
  visited.add(node);

  if (
    MorphNode.isFunctionDeclaration(node) ||
    MorphNode.isFunctionExpression(node) ||
    MorphNode.isArrowFunction(node)
  ) {
    return node.getDescendants().some((descendant) =>
      descendant.getKind() === SyntaxKind.JsxElement ||
      descendant.getKind() === SyntaxKind.JsxSelfClosingElement ||
      descendant.getKind() === SyntaxKind.JsxFragment,
    ) ? node : null;
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
      if (!MorphNode.isVariableDeclaration(decl) && !MorphNode.isFunctionDeclaration(decl)) continue;
      const initializer = MorphNode.isVariableDeclaration(decl) ? decl.getInitializer() : decl;
      if (initializer) return resolveJsxFunction(initializer, visited, depth + 1);
    }
    return null;
  }

  if (!MorphNode.isCallExpression(node)) return null;
  if (!/(?:^|\\.)(?:memo|forwardRef)$/.test(node.getExpression().getText())) return null;
  const wrapped = node.getArguments()[0];
  return wrapped ? resolveJsxFunction(wrapped, visited, depth + 1) : null;
}

function isJsxFunctionDeclaration(decl: Node): boolean {
  let candidate: Node = decl;
  if (MorphNode.isVariableDeclaration(decl)) {
    const init = decl.getInitializer();
    if (!init) return false;
    candidate = init;
  }
  return resolveJsxFunction(candidate) !== null;
}

function componentIdentityOf(decl: Node, rootPath: string): string | null {
  if (!isJsxFunctionDeclaration(decl)) return null;
  const sourceFile = decl.getSourceFile();
  const relativeFile = toProjectRelative(rootPath, sourceFile.getFilePath());
  if (!relativeFile) return null;
  let name: string | undefined;
  if (MorphNode.isVariableDeclaration(decl) || MorphNode.isFunctionDeclaration(decl) || MorphNode.isClassDeclaration(decl)) {
    name = decl.getName() ?? undefined;
  }
  if (!name && MorphNode.isFunctionDeclaration(decl)) name = decl.getName() ?? undefined;
  return name ? canonicalId(`${relativeFile}#${name}`) : null;
}

/** 在文件里查找某个本地名的 import 来源说明符。 */
export function findImportSource(sourceFile: SourceFile, localName: string): string | null {
  // `Antd.Button` 这类点号标签取第一段去找命名空间导入
  const rootName = localName.split(".")[0] ?? localName;

  for (const imp of sourceFile.getImportDeclarations()) {
    if (imp.getDefaultImport()?.getText() === rootName) return imp.getModuleSpecifierValue();
    if (imp.getNamespaceImport()?.getText() === rootName) return imp.getModuleSpecifierValue();
    for (const named of imp.getNamedImports()) {
      if (named.getAliasNode()?.getText() === rootName) return imp.getModuleSpecifierValue();
      if (named.getName() === rootName) return imp.getModuleSpecifierValue();
    }
  }
  return null;
}

/**
 * 把「指向组件的表达式」展开成候选表达式列表。
 *
 * 典型形态是 slot / polymorphic 组件：
 *   `const Comp = asChild ? Slot : "button";` 然后 `<Comp />`
 *
 * 这不是解析失败——两个分支各自都指向真实的东西（一个外部组件、一个宿主元素），
 * 只是「这一次渲染用哪个」取决于运行时 prop。静态分析只能如实说「可能是这些」。
 */
function expandTagCandidates(expr: Node, depth: number): Node[] {
  if (depth > MAX_TAG_BRANCH_DEPTH) return [expr];

  if (MorphNode.isParenthesizedExpression(expr)) {
    return expandTagCandidates(expr.getExpression(), depth + 1);
  }
  // `as const` / 非空断言不改变指向
  if (MorphNode.isAsExpression(expr) || MorphNode.isSatisfiesExpression(expr)) {
    return expandTagCandidates(expr.getExpression(), depth + 1);
  }
  if (MorphNode.isNonNullExpression(expr)) {
    return expandTagCandidates(expr.getExpression(), depth + 1);
  }

  // `cond ? A : B`：两个分支都在候选里
  if (MorphNode.isConditionalExpression(expr)) {
    return [
      ...expandTagCandidates(expr.getWhenTrue(), depth + 1),
      ...expandTagCandidates(expr.getWhenFalse(), depth + 1),
    ];
  }

  // `A || B` / `A ?? B` / `A && <B />` 里都可能出现组件
  if (MorphNode.isBinaryExpression(expr)) {
    const operator = expr.getOperatorToken().getText();
    if (operator === "||" || operator === "??" || operator === "&&") {
      return [
        ...expandTagCandidates(expr.getLeft(), depth + 1),
        ...expandTagCandidates(expr.getRight(), depth + 1),
      ];
    }
  }

  // 变量：沿声明回溯到初始值。回溯不到（函数参数、props）时保留自身，
  // 由调用方按未解析处理——那是真正定不下来的情况
  if (MorphNode.isIdentifier(expr)) {
    const initializer = initializerOf(expr);
    if (initializer) return expandTagCandidates(initializer, depth + 1);
  }

  return [expr];
}

/** 标识符 → 其变量声明的初始值（穿透别名）。 */
function initializerOf(identifier: Node): Node | null {
  let symbol = identifier.getSymbol();
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
    if (init) return init;
  }
  return null;
}

/**
 * 解析「可能指向多个组件」的标签，返回全部可确定的目标。
 *
 * 与 resolveTagTarget 的关系：它只回答单值。当单值解析未命中时，这里的多分支
 * 解析能说明「标签不是错的，只是运行时才决定」——两者结论不同，不该混为一谈。
 *
 * 宿主元素分支（`"button"`）不进结果：字符串标签编译成 DOM 元素，不是组件。
 * 返回类型里没有 unresolved：解析不到的分支只是没进结果，不冒充「确定不了」。
 */
export function resolvePolymorphicTagTargets(
  tagNameNode: Node,
  context: TagResolveContext,
): Exclude<TagTarget, { kind: "unresolved" }>[] {
  const targets: Exclude<TagTarget, { kind: "unresolved" }>[] = [];
  const seen = new Set<string>();

  for (const candidate of expandTagCandidates(tagNameNode, 0)) {
    // 字符串字面量分支 = 宿主元素，不产 renders 边
    if (
      MorphNode.isStringLiteral(candidate) ||
      MorphNode.isNoSubstitutionTemplateLiteral(candidate)
    ) {
      continue;
    }

    const target = resolveSymbolTarget(candidate, context);
    if (target.kind === "unresolved") continue;

    const key = target.kind === "component" ? `c:${target.id}` : `e:${target.nodeId}`;
    if (seen.has(key)) continue;
    seen.add(key);
    targets.push(target);  }

  return targets;
}

/** 便于调试：把解析结果格式化成一行。 */
export function formatTagTarget(target: TagTarget): string {
  switch (target.kind) {
    case "component":
      return `component:${target.id}`;
    case "external":
      return `external:${target.nodeId}`;
    case "unresolved":
      return "unresolved";
  }
}

/** 供调用方标注类型：组件发现结果的别名，避免 jsx-refs 反过来依赖分析流程。 */
export type ComponentRef = DiscoveredComponent;
