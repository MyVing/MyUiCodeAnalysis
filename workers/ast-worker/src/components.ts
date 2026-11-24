/**
 * 组件发现：从项目源码里找出「导出的 Revact 组件」，并给出稳定 id。
 *
 * 三条硬约束：
 *
 * 1. **id 由「文件路径 + 组件名」组成**（`src/TableCard.tsx#TableCard`）。
 *    同一文件多组件因此天然不冲突 —— 这是 ReactGraph「缺陷 1」的根因：
 *    按文件生成 id 再去重，`TableCard` 与 `CardFooter` 会被合并成一个。
 *
 * 2. **按定义位置去重，不按导出名计数**。`export function X() {}` + `export default X`
 *    会让 `getExportedDeclarations()` 同时返回 `X` 与 `default`，两者指向同一 AST 节点。
 *    若按导出名建节点，单组件会变成两个。这里以声明节点为键合并。
 *
 * 3. **只认当前文件内的声明**。`getExportedDeclarations()` 会沿 barrel 穿透到最终定义，
 *    对 `src/index.ts` 调用会返回其他文件的组件。只取定义文件等于当前文件的条目，
 *    组件便只在定义处产出一次。
 *
 * 判定组件的依据**不是函数名**（不做 `startsWith("use")` 之类的猜测），而是结构：
 * 体内出现 JSX 节点，或返回类型可赋给 JSX.Element / ReactElement。
 * 前者不依赖 `@types/react` 是否安装，是主依据；后者用于 `return createElement(...)` 这类写法。
 */

import type { Node, Project, SourceFile } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { Warning } from "@uicodeanalysis/contracts";

import { toProjectRelative } from "./paths.js";
import { positionKey } from "./positions.js";

/** 发现的组件。 */
export interface DiscoveredComponent {
  /** IR 节点 id：`<相对路径>#<组件名>` */
  id: string;
  /** 组件名；匿名默认导出退化为文件名（去扩展名） */
  name: string;
  /** 相对项目根目录的文件路径 */
  file: string;
  /** 定义起始位置（1-based） */
  loc: { line: number; col: number };
  /** props 类型名（如 `UserListProps`）；内联类型或未标注时为 null */
  propsTypeName: string | null;
  /** 组件形态，写进 attrs 便于前端区分 */
  form: "function" | "arrow" | "class";
  /** 该组件以哪些名字被导出（用于诊断与后续边解析） */
  exportNames: string[];
}

export interface ComponentDiscoveryResult {
  components: DiscoveredComponent[];
  /**
   * 组件 id → 定义它的 AST 节点。
   *
   * 刻意与 components 分开：`DiscoveredComponent` 是纯数据（可直接序列化进
   * IR），AST 节点只在分析进程内有效，混进去会让人误以为它属于契约的一部分。
   */
  declarations: Map<string, Node>;
  /** 定义位置键（见 positions.ts）→ 组件 id，用于把 JSX 标签反查回组件 */
  definitionIndex: Map<string, string>;
  warnings: Warning[];
}

const JSX_KINDS = new Set<SyntaxKind>([
  SyntaxKind.JsxElement,
  SyntaxKind.JsxSelfClosingElement,
  SyntaxKind.JsxFragment,
]);

/** 组件判定的结构条件：体内有 JSX，或返回类型是 JSX/React 元素。 */
function looksLikeComponent(node: Node): boolean {
  // 体内含 JSX：不依赖类型声明是否可用，是主依据
  const hasJsx = node.getDescendants().some((d) => JSX_KINDS.has(d.getKind()));
  if (hasJsx) return true;

  // 返回 JSX.Element / ReactElement：覆盖 `return createElement(...)` 的写法
  const returnTypeText = node
    .getType()
    .getCallSignatures()
    .map((sig) => sig.getReturnType().getText())
    .join("|");
  return /JSX\.Element|ReactElement|React\.ReactElement/.test(returnTypeText);
}

/** 取函数式节点的 props 类型名；内联对象类型、无参、仅解构无标注时返回 null。 */
function extractPropsTypeName(node: Node): string | null {
  const params = getParametersOf(node);
  const first = params[0];
  if (!first) return null;

  // React 组件约定第一个参数是 props；ref 作为第二参数不参与
  const typeNode = first.getTypeNode();
  if (!typeNode) return null;

  // 只接受具名类型引用：`UserListProps`、`ProTableProps<T>`
  if (MorphNode.isTypeReference(typeNode)) {
    return typeNode.getTypeName().getText();
  }
  // 内联对象字面量类型与交叉类型暂不解析成名字，留待 props 分析阶段处理
  return null;
}

/** 只从函数式节点上取参数列表；其他节点（如类）没有组件意义上的 props。 */
function getParametersOf(node: Node) {
  if (
    MorphNode.isFunctionDeclaration(node) ||
    MorphNode.isFunctionExpression(node) ||
    MorphNode.isArrowFunction(node)
  ) {
    return node.getParameters();
  }
  return [];
}

/** 匿名默认导出时用文件名兜底，保证 name 与 id 非空。 */
function fallbackNameFromFile(file: string): string {
  const base = file.split("/").pop() ?? file;
  return base.replace(/\.(tsx?|jsx?)$/, "");
}

/** 从 AST 节点上取声明名：函数/类有 getName()，变量声明有 getName()。 */
function getNodeName(node: Node): string | undefined {
  if (
    MorphNode.isFunctionDeclaration(node) ||
    MorphNode.isFunctionExpression(node) ||
    MorphNode.isClassDeclaration(node) ||
    MorphNode.isVariableDeclaration(node)
  ) {
    return node.getName() ?? undefined;
  }
  return undefined;
}

/**
 * 从一个声明节点构造组件；不是组件返回 null。
 */
/** 最多追踪几层变量 / memo / forwardRef 初始化器，避免异常别名链。 */
const MAX_COMPONENT_UNWRAP_DEPTH = 8;

/** 将函数节点或受支持包装器中的静态函数引用解析到底层函数。 */
function resolveFunctionComponentNode(
  node: Node,
  visited: Set<Node> = new Set(),
  depth = 0,
): Node | null {
  if (depth > MAX_COMPONENT_UNWRAP_DEPTH || visited.has(node)) return null;
  visited.add(node);

  if (MorphNode.isArrowFunction(node) || MorphNode.isFunctionExpression(node) || MorphNode.isFunctionDeclaration(node)) {
    return node;
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
    for (const declaration of symbol?.getDeclarations() ?? []) {
      if (!MorphNode.isVariableDeclaration(declaration) && !MorphNode.isFunctionDeclaration(declaration)) continue;
      const initializer = MorphNode.isVariableDeclaration(declaration) ? declaration.getInitializer() : declaration;
      if (!initializer) continue;
      return resolveFunctionComponentNode(initializer, visited, depth + 1);
    }
    return null;
  }

  if (!MorphNode.isCallExpression(node)) return null;
  const callee = node.getExpression().getText();
  if (!/(?:^|\.)(?:memo|forwardRef)$/.test(callee)) return null;
  const wrapped = node.getArguments()[0];
  return wrapped ? resolveFunctionComponentNode(wrapped, visited, depth + 1) : null;
}

function toComponent(decl: Node, sourceFile: SourceFile, relativeFile: string): DiscoveredComponent | null {
  let componentNode: Node = decl;
  let form: DiscoveredComponent["form"] = "function";

  if (MorphNode.isVariableDeclaration(decl)) {
    const init = decl.getInitializer();
    if (!init) return null;
    const resolved = resolveFunctionComponentNode(init);
    if (!resolved) return null;
    componentNode = resolved;
    form = MorphNode.isArrowFunction(resolved) ? "arrow" : "function";
  } else if (MorphNode.isFunctionDeclaration(decl) || MorphNode.isFunctionExpression(decl) || MorphNode.isArrowFunction(decl)) {
    form = MorphNode.isArrowFunction(decl) ? "arrow" : "function";
  } else if (MorphNode.isClassDeclaration(decl)) {
    if (!looksLikeClassComponent(decl)) return null;
    form = "class";
  } else {
    // interface / type alias / enum 等：是类型而非组件
    return null;
  }

  if (!looksLikeComponent(componentNode)) return null;

  // 名字优先取自变量声明或组件节点本身；匿名默认导出才回退到文件名
  const declaredName = getNodeName(decl) ?? getNodeName(componentNode);
  const name = declaredName && declaredName.length > 0 ? declaredName : fallbackNameFromFile(relativeFile);
  const pos = sourceFile.getLineAndColumnAtPos(decl.getStart());

  return {
    id: `${relativeFile}#${name}`,
    name,
    file: relativeFile,
    loc: { line: pos.line, col: pos.column },
    propsTypeName: form === "class" ? null : extractPropsTypeName(componentNode),
    form,
    exportNames: [],
  };
}

/**
 * 类组件判定：继承自 React.Component / PureComponent / Component。
 *
 * 只读继承表达式文本，不以类名猜测 —— `class Foo extends Component` 与
 * `class Foo extends React.PureComponent` 都应命中，而 `class Foo extends Bar` 不应。
 */
function looksLikeClassComponent(decl: Node): boolean {
  if (!MorphNode.isClassDeclaration(decl)) return false;
  const extendsText = decl.getExtends()?.getExpression().getText() ?? "";
  if (!/^(React\.)?(PureComponent|Component)$/.test(extendsText)) return false;
  // 必须渲染：有 render 方法
  return decl.getMethod("render") !== undefined;
}

/** 体内是否含 JSX 节点；纯语法判断，不依赖类型检查器。 */
function hasJsxBody(node: Node): boolean {
  return node.getDescendants().some((descendant) => JSX_KINDS.has(descendant.getKind()));
}

/**
 * 语法层的组件导出候选。
 *
 * 只用于「导出没有被解析成组件」的告警，不参与组件识别本身。即使
 * `getExportedDeclarations()` 因为 allowJs 未生效等原因返回空 Map，
 * 也能直接从 AST 看出文件到底导出了哪些含 JSX 的函数 / 类。
 *
 * 判定口径与 toComponent 保持一致：函数类看体内 JSX，类沿用
 * looksLikeClassComponent，不做名字猜测，因此导出的常量、hook、工具函数
 * 不会被误当成候选。
 */
function collectComponentExportCandidates(file: SourceFile, relativeFile: string): string[] {
  // 先登记本文件的函数 / 函数变量声明，供 `export { X }` 与 `export default X` 反查
  const localDeclarations = new Map<string, Node>();
  for (const statement of file.getStatements()) {
    if (MorphNode.isFunctionDeclaration(statement) || MorphNode.isClassDeclaration(statement)) {
      const name = statement.getName();
      if (name) localDeclarations.set(name, statement);
      continue;
    }
    if (!MorphNode.isVariableStatement(statement)) continue;
    for (const declaration of statement.getDeclarations()) {
      const initializer = declaration.getInitializer();
      if (initializer && (MorphNode.isArrowFunction(initializer) || MorphNode.isFunctionExpression(initializer))) {
        localDeclarations.set(declaration.getName(), initializer);
      } else if (initializer) {
        const resolved = resolveFunctionComponentNode(initializer);
        if (resolved) localDeclarations.set(declaration.getName(), resolved);
      }
    }
  }

  const candidates = new Set<string>();
  const addIfComponentLike = (name: string | undefined, node: Node): void => {
    const isComponentLike = MorphNode.isClassDeclaration(node) ? looksLikeClassComponent(node) : hasJsxBody(node);
    if (!isComponentLike) return;
    candidates.add(name && name.length > 0 ? name : fallbackNameFromFile(relativeFile));
  };

  for (const statement of file.getStatements()) {
    if (MorphNode.isFunctionDeclaration(statement) || MorphNode.isClassDeclaration(statement)) {
      if (statement.isExported() || statement.isDefaultExport()) {
        addIfComponentLike(statement.getName(), statement);
      }
      continue;
    }

    if (MorphNode.isVariableStatement(statement)) {
      if (!statement.isExported() && !statement.isDefaultExport()) continue;
      for (const declaration of statement.getDeclarations()) {
        const initializer = declaration.getInitializer();
        if (initializer && (MorphNode.isArrowFunction(initializer) || MorphNode.isFunctionExpression(initializer))) {
          addIfComponentLike(declaration.getName(), initializer);
        }
      }
      continue;
    }

    if (MorphNode.isExportDeclaration(statement)) {
      // 带模块说明符的是跨文件再导出，不能算作本文件定义的组件
      if (statement.getModuleSpecifier() !== undefined) continue;
      for (const specifier of statement.getNamedExports()) {
        const local = localDeclarations.get(specifier.getName());
        if (local) addIfComponentLike(specifier.getName(), local);
      }
      continue;
    }

    if (MorphNode.isExportAssignment(statement)) {
      // `export default Foo`：反查本地声明；`export default () => <div />` 直接判定
      const expression = statement.getExpression();
      if (MorphNode.isIdentifier(expression)) {
        const local = localDeclarations.get(expression.getText());
        if (local) addIfComponentLike(expression.getText(), local);
        continue;
      }
      if (MorphNode.isArrowFunction(expression) || MorphNode.isFunctionExpression(expression)) {
        addIfComponentLike(undefined, expression);
      }
    }
  }

  return [...candidates];
}

/**
 * 发现项目内所有组件。
 *
 * 只遍历「项目自身源码」——外层已按 node_modules / .d.ts 过滤，这里再以
 * relativeFile 是否为 null 兜一次底，避免外部 .d.ts 里的类型被误当组件。
 */
export function discoverComponents(rootPath: string, project: Project): ComponentDiscoveryResult {
  const warnings: Warning[] = [];
  const byId = new Map<string, DiscoveredComponent>();
  const declarations = new Map<string, Node>();
  const definitionIndex = new Map<string, string>();

  for (const file of project.getSourceFiles()) {
    const relativeFile = toProjectRelative(rootPath, file.getFilePath());
    if (relativeFile === null) continue;
    if (!/\.(tsx|jsx|ts|js)$/.test(relativeFile)) continue;

    // 本文件实际解析出的组件名，用于和语法层候选对账
    const resolvedNames = new Set<string>();

    // getExportedDeclarations 会穿透 barrel；只保留定义在本文件的条目
    const exported = file.getExportedDeclarations();

    for (const [exportName, decls] of exported) {
      for (const decl of decls) {
        if (decl.getSourceFile() !== file) continue;

        const component = toComponent(decl, file, relativeFile);
        if (!component) continue;

        resolvedNames.add(component.name);

        // 同一 AST 节点可能有多个导出名（具名 + default），合并为一个组件
        const existing = byId.get(component.id);
        if (existing) {
          if (!existing.exportNames.includes(exportName)) existing.exportNames.push(exportName);
          continue;
        }

        component.exportNames.push(exportName);
        byId.set(component.id, component);
        declarations.set(component.id, decl);
        definitionIndex.set(positionKey(relativeFile, decl.getStart()), component.id);
      }
    }

    // 语法的导出候选与实际解析结果对账：有候选却没建出组件，说明导出在
    // 类型层不可见（典型是 allowJs 未生效），必须留下带文件名的告警，
    // 不允许再出现"组件静默消失"。
    const missing = collectComponentExportCandidates(file, relativeFile).filter(
      (name) => !resolvedNames.has(name),
    );
    if (missing.length > 0) {
      warnings.push({
        code: "component_exports_unresolved",
        severity: "warning",
        stage: "static",
        file: relativeFile,
        message: `${relativeFile} 中以下导出具备组件结构（体内含 JSX 的函数/类），但未解析为组件：${missing.join("、")}。常见原因是 allowJs 未生效，导致 .js/.jsx 导出在类型层不可见。`,
      });
    }
  }

  return { components: [...byId.values()], declarations, definitionIndex, warnings };
}

/**
 * 校验组件 id 的唯一性。
 *
 * 正常路径下 `byId` 已经保证唯一；这个函数用于在构造图之前显式断言，
 * 因为 id 重复会静默覆盖节点，比报错更难查。
 */
export function assertUniqueIds(components: readonly DiscoveredComponent[]): void {
  const seen = new Set<string>();
  for (const component of components) {
    if (seen.has(component.id)) {
      throw new Error(`组件 id 重复：${component.id}（同一文件多组件必须各自独立命名）`);
    }
    seen.add(component.id);
  }
}

/** 供调试：把组件列表格式化成一行一条。 */
export function formatComponents(components: readonly DiscoveredComponent[]): string[] {
  return components.map(
    (c) => `${c.id}  form=${c.form}  props=${c.propsTypeName ?? "-"}  exports=${c.exportNames.join(",")}`,
  );
}
