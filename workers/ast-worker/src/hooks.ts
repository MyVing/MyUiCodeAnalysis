/**
 * hooks / state 分析：把组件里的 hook 调用与状态声明变成节点与边。
 *
 * ## 为什么 useState 与其它 hook 分开处理
 *
 * `useState` 产出的是**状态**（值 + setter），语义上属于 `kind: "state"`，
 * 它的价值在 reads/writes 边；`useEffect` / `useCallback` / 自定义 `useXxx`
 * 产出的是**副作用与派生逻辑**，属于 `kind: "hook"`。把 `useState` 也算成
 * hook 节点会让同一个声明在图上出现两次，前端筛选"状态"时又看不到它。
 *
 * ## 两条判定规则
 *
 * 1. **hook 调用必须位于组件函数体的顶层语句里**。React 的规则就是如此
 *    （不能在条件、循环、嵌套函数里调用），因此从顶层语句向下找时**遇到
 *    函数节点就停**——`useEffect(() => { ... })` 的回调体里即使出现 `useXxx`
 *    也不属于本组件，那是违规写法，不该被当成结构事实。
 *
 * 2. **reads / writes 可以出现在任何深度**。`setLoading(true)` 写在 effect
 *    回调里，仍然是这个组件在写自己的状态。用符号引用查找而不是位置推断。
 *
 * ## 不按名字猜"是不是 hook"
 *
 * 优先看符号：能从 import 追到 `react` 的是内置 hook；追不到（依赖未安装、
 * 或被重新导出）时退回 `use[A-Z]` 约定。这个前缀是 React 生态的语言级约定
 * （eslint-plugin-react-hooks 同样依赖它），不是业务命名，与"禁止用
 * startsWith('use') 判断组件"是两件事——那条禁的是把 hook 当组件。
 */

import type { CallExpression, Identifier, Node, SourceFile } from "ts-morph";
import { Node as MorphNode } from "ts-morph";

import type { Evidence, IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";
import { isProjectSource, toProjectRelative } from "./paths.js";
import { positionKey } from "./positions.js";

/** 组件里的 hook 调用是语法事实。 */
const CALL_CONFIDENCE = 1;
/** 引用查找依赖语言服务；依赖不全时可能漏掉读取点，因此略低于满值。 */
const REFERENCE_CONFIDENCE = 0.9;

/** 内置 hook 里产出状态的那一个，其余都按 hook 节点处理。 */
const STATE_HOOK = "useState";

/** React 的 hook 命名约定：use + 大写字母。 */
const HOOK_NAME_PATTERN = /^use[A-Z0-9]/;

export interface HookNode {
  node: IRNode;
}

export interface HooksAnalysisResult {
  /** hook 节点（useEffect / useCallback / useMemo / 自定义 useXxx） */
  hookNodes: IRNode[];
  /** 状态节点（useState 的值） */
  stateNodes: IRNode[];
  /** 组件 → hook / state 的 uses / reads / writes 边 */
  edges: IREdge[];
  /**
   * 「hook 回调体位置 → hook 节点 id」索引，供 `api.ts` 判断请求归属。
   *
   * 键是回调函数节点的定义位置（`文件@起始偏移`），值是 hook 节点 id。只收录
   * 绑定了名字的 hook（`const handleExport = useCallback(...)`）——没有名字的
   * `useEffect(() => {...})` 没有可归属的节点身份，回调体里的请求仍归组件，
   * 这是事实：它是组件挂载时执行的，不是某个人手动触发的动作。
   */
  callbackOwners: ReadonlyMap<string, string>;
  warnings: Warning[];
}

export interface HooksAnalysisInput {
  rootPath: string;
  components: readonly DiscoveredComponent[];
  /** 组件 id → 定义 AST 节点 */
  declarations: ReadonlyMap<string, Node>;
}

/** 组件定义对应的函数节点；类组件返回 null（没有 hooks）。 */
function getFunctionNode(decl: Node): Node | null {
  if (MorphNode.isVariableDeclaration(decl)) {
    const init = decl.getInitializer();
    if (init && (MorphNode.isArrowFunction(init) || MorphNode.isFunctionExpression(init))) {
      return init;
    }
    return null;
  }
  if (
    MorphNode.isFunctionDeclaration(decl) ||
    MorphNode.isFunctionExpression(decl) ||
    MorphNode.isArrowFunction(decl)
  ) {
    return decl;
  }
  return null;
}

function isFunctionLike(node: Node): boolean {
  return (
    MorphNode.isFunctionDeclaration(node) ||
    MorphNode.isFunctionExpression(node) ||
    MorphNode.isArrowFunction(node) ||
    MorphNode.isMethodDeclaration(node) ||
    MorphNode.isConstructorDeclaration(node) ||
    MorphNode.isGetAccessorDeclaration(node) ||
    MorphNode.isSetAccessorDeclaration(node)
  );
}

/**
 * 收集组件体顶层语句里的所有调用表达式，**不跨越函数边界**。
 *
 * `const rows = loadData ? useProTableData(loadData) : []` 里的调用在条件表达式里，
 * 但仍是顶层语句的一部分，应当命中；而 `useEffect(() => { useFoo() })` 中的
 * `useFoo` 在回调体内，跨了函数边界，不命中。
 */
function collectTopLevelCalls(functionNode: Node): CallExpression[] {
  const body = MorphNode.isArrowFunction(functionNode) || MorphNode.isFunctionExpression(functionNode)
    ? functionNode.getBody()
    : MorphNode.isFunctionDeclaration(functionNode)
      ? functionNode.getBody()
      : undefined;
  if (!body || !MorphNode.isBlock(body)) return [];

  const calls: CallExpression[] = [];
  const visit = (node: Node): void => {
    if (MorphNode.isCallExpression(node)) {
      calls.push(node);
      // 参数里的箭头函数是回调，不是本组件的 hook 调用点；继续下探到参数节点
      // 本身没有意义，直接由子节点递归时的函数判定拦住。
    }
    if (isFunctionLike(node)) return;
    for (const child of node.getChildren()) visit(child);
  };

  for (const statement of body.getStatements()) visit(statement);
  return calls;
}

/** 取调用表达式的被调用名；`React.useState` 这类取最后一段。 */
function getCalleeName(call: CallExpression): string | null {
  const expr = call.getExpression();
  if (MorphNode.isIdentifier(expr)) return expr.getText();
  if (MorphNode.isPropertyAccessExpression(expr)) return expr.getName();
  return null;
}

/**
 * 判断调用是不是 React hook，并区分内置与自定义。
 *
 * 符号能追到 import 时以真实来源为准：`import { useState } from "react"` 是内置；
 * `import { useTable } from "./hooks"` 是自定义。追不到符号（依赖未安装）时
 * 退回 `use[A-Z]` 前缀——这是 React 生态的语言级约定。
 */
function classifyHook(
  call: CallExpression,
  name: string,
): { builtin: boolean } | null {
  if (!HOOK_NAME_PATTERN.test(name)) return null;

  const expr = call.getExpression();
  const symbol = MorphNode.isPropertyAccessExpression(expr)
    ? expr.getNameNode().getSymbol()
    : expr.getSymbol();

  for (const decl of symbol?.getDeclarations() ?? []) {
    // 沿 import 找到模块说明符
    const importDecl = MorphNode.isImportSpecifier(decl) ? decl.getImportDeclaration() : null;
    if (!importDecl) continue;
    const moduleName = importDecl.getModuleSpecifierValue();
    return { builtin: moduleName === "react" || moduleName.startsWith("react/") };
  }

  // 追不到来源：按命名约定视为 hook，但不声称是内置
  return { builtin: false };
}

/** 取 hook 的依赖数组文本；无第二参数返回 null（表示每次渲染都执行）。 */
function extractDeps(call: CallExpression): string[] | null {
  const args = call.getArguments();
  if (args.length < 2) return null;
  const deps = args[1];
  if (!deps || !MorphNode.isArrayLiteralExpression(deps)) return null;
  return deps.getElements().map((element) => element.getText());
}

/** `useState<UserRowProps[]>([])` 的类型参数文本。 */
function extractStateTypeText(call: CallExpression): string | null {
  const [typeArg] = call.getTypeArguments();
  return typeArg?.getText() ?? null;
}

/**
 * 解构形态的 useState 声明。
 *
 * 返回标识符节点本身而不是名字字符串：后续要用它做引用查找，
 * 名字只能用于显示。
 */
function readStateBinding(
  decl: Node,
): { valueName: Identifier; setterName: Identifier | null } | null {
  if (!MorphNode.isVariableDeclaration(decl)) return null;
  const nameNode = decl.getNameNode();
  if (!MorphNode.isArrayBindingPattern(nameNode)) return null;

  const elements = nameNode.getElements();
  // 只接受最简单的标识符解构；`const [{ a }] = useState()` 这类嵌套形态不在静态分析一期范围内
  const valueElement = elements[0];
  if (!valueElement || !MorphNode.isBindingElement(valueElement)) return null;
  const valueName = valueElement.getNameNode();
  if (!MorphNode.isIdentifier(valueName)) return null;

  const setterElement = elements[1];
  let setterName: Identifier | null = null;
  if (setterElement && MorphNode.isBindingElement(setterElement)) {
    const setterNode = setterElement.getNameNode();
    // setter 被解构重命名（`[x, { set }]`）或忽略（`[x]`）时不产 writes 边
    if (MorphNode.isIdentifier(setterNode)) setterName = setterNode;
  }

  return { valueName, setterName };
}

/**
 * 取标识符的全部引用节点；引用查找失败时返回空数组并记告警。
 *
 * 引用查找依赖语言服务，跨文件重新导出等场景可能抛错。失败时宁可少标几条边
 * 并留下可见告警，也不要吞掉异常让分析结果看起来"完整"。
 */
function findReferences(identifier: Identifier, warnings: Warning[], context: string): Node[] {
  try {
    return identifier.findReferencesAsNodes();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    warnings.push({
      code: "reference_lookup_failed",
      severity: "warning",
      stage: "static",
      message: `无法为 ${context} 解析引用位置，reads/writes 边可能不完整：${message}`,
    });
    return [];
  }
}

/**
 * 把引用位置转成证据。
 *
 * 每个引用各自取所在文件，不能统一套用状态声明所在的文件：解构出来的值虽然
 * 不会跨文件导出，但引用查找本身不保证这一点，写死文件路径会产生指向错误
 * 位置的证据。项目外的引用（理论上不会出现）直接丢弃，不伪造位置。
 */
function toEvidence(
  rootPath: string,
  references: readonly Node[],
): Evidence[] {
  const evidence: Evidence[] = [];
  for (const ref of references) {
    const refFile = toProjectRelative(rootPath, ref.getSourceFile().getFilePath());
    if (refFile === null) continue;
    const pos = ref.getSourceFile().getLineAndColumnAtPos(ref.getStart());
    evidence.push({
      source: "static",
      confidence: REFERENCE_CONFIDENCE,
      file: refFile,
      loc: { line: pos.line, col: pos.column },
    });
  }
  return evidence;
}

function nodeIdForState(componentId: string, stateName: string): string {
  return `${componentId}/${stateName}`;
}

/**
 * 取 hook 调用的回调函数节点（第一个实参，且必须是箭头 / 函数表达式）。
 *
 * `useCallback` / `useMemo` / `useEffect` / 自定义 `useXxx` 的回调都在第一个
 * 位置，这是 React 生态的语言级约定（与 `HOOK_NAME_PATTERN` 同一性质）。
 * 第一实参不是函数的（`useTable({ columns })`）返回 null——那不是回调体，
 * 里面的请求属于组件本身。
 */
function callbackFunctionOf(call: CallExpression): Node | null {
  const [first] = call.getArguments();
  if (!first) return null;
  if (MorphNode.isArrowFunction(first) || MorphNode.isFunctionExpression(first)) return first;
  return null;
}

/**
 * hook 节点 id 的后缀：优先用绑定的变量名（`handleSelect`），没有绑定时
 * 用「hook 名 + 组件内序号」（`useEffect#1`）。
 *
 * 不用行列号：在组件上方插一行注释就会让所有 hook 的 id 漂移，导致增量分析
 * 之外的全量重算也产生无意义的节点增删。序号只在同一组件内比较，插入一个新的
 * useEffect 会使其后的序号顺移——这是源码结构真的变了，可以接受。
 */
function hookSuffix(
  bindingName: string | null,
  hookName: string,
  counters: Map<string, number>,
  componentId: string,
): string {
  if (bindingName) return bindingName;
  // 计数按「组件 + hook 名」分开，避免 useMemo 抢掉 useEffect 的序号
  const key = `${componentId} ${hookName}`;
  const next = (counters.get(key) ?? 0) + 1;
  counters.set(key, next);
  return next === 1 ? hookName : `${hookName}#${next}`;
}

export function analyzeHooks(input: HooksAnalysisInput): HooksAnalysisResult {
  const { rootPath, components, declarations } = input;

  const hookNodes: IRNode[] = [];
  const stateNodes: IRNode[] = [];
  const edges = new Map<string, IREdge>();
  const warnings: Warning[] = [];
  const hookCounters = new Map<string, number>();
  const callbackOwners = new Map<string, string>();

  for (const component of components) {
    const decl = declarations.get(component.id);
    if (!decl) continue;
    const functionNode = getFunctionNode(decl);
    if (!functionNode) continue;

    const sourceFile = decl.getSourceFile();
    const relativeFile = toProjectRelative(rootPath, sourceFile.getFilePath());
    if (relativeFile === null) continue;

    for (const call of collectTopLevelCalls(functionNode)) {
      const hookName = getCalleeName(call);
      if (!hookName) continue;

      const classification = classifyHook(call, hookName);
      if (!classification) continue;

      // useState 的解构声明：调用本身在初始化表达式里，声明在外层
      const declaration = findOwningVariableDeclaration(call);

      if (hookName === STATE_HOOK) {
        const stateNode = declaration
          ? buildStateNode({
              rootPath,
              component,
              declaration,
              call,
              sourceFile,
              relativeFile,
              warnings,
              edges,
            })
          : null;
        if (stateNode) {
          stateNodes.push(stateNode);
          continue;
        }
        // 非解构形态（`const tuple = useState(0)`）：仍是一次 hook 调用，
        // 落到下面按 hook 记录，只是不冒充状态——没有 setter 就谈不上读写语义。
      }

      // 其余 hook：useEffect / useCallback / useMemo / 自定义 useXxx。
      // 绑定了变量的（`const handleSelect = useCallback(...)`）以变量名作后缀，
      // 这样「handleSelect 用了哪个 hook」在图上直接可读。
      const boundName = readBoundVariableName(call);
      // 解构 hook 返回值的绑定名：`const { onSubmit } = useLogin()` 里事件引用的
      // 是 onSubmit 而不是 hook 名，事件分析需要这份名单才能落点
      const destructuredNames = readDestructuredBindingNames(declaration);
      const nodeId = `${component.id}/${hookSuffix(boundName, hookName, hookCounters, component.id)}`;

      const pos = sourceFile.getLineAndColumnAtPos(call.getStart());

      hookNodes.push({
        id: nodeId,
        kind: "hook",
        name: boundName ?? hookName,
        file: relativeFile,
        loc: { line: pos.line, col: pos.column },
        attrs: {
          hookName,
          custom: !classification.builtin,
          deps: extractDeps(call),
          // 绑定变量名与 hook 名不同时两个都要能看到
          ...(boundName ? { binding: boundName } : {}),
          ...(destructuredNames.length > 0 ? { destructured: destructuredNames } : {}),
        },
        evidence: [
          {
            source: "static",
            confidence: CALL_CONFIDENCE,
            file: relativeFile,
            loc: { line: pos.line, col: pos.column },
          },
        ],
      });

      // 回调体归属：只有绑定了名字的 hook 才有节点身份可归属。
      // `useEffect(() => { fetch() })` 的请求仍归组件——它在挂载时执行，
      // 不是某个具名动作触发的，挂到匿名 hook 上反而让人误以为可以单独触发。
      if (boundName) {
        const callback = callbackFunctionOf(call);
        if (callback) {
          callbackOwners.set(positionKey(relativeFile, callback.getStart()), nodeId);
        }
      }

      // 自定义 hook：定义体在别的文件里，其内部绑定回调的归属登记到本调用点节点
      if (!classification.builtin) {
        registerCustomHookCallbackBodies(call, nodeId, rootPath, callbackOwners);
      }

      addEdge(edges, {
        id: `e_uses_${component.id}__${nodeId}`,
        from: component.id,
        to: nodeId,
        kind: "uses",
        evidence: [
          {
            source: "static",
            confidence: CALL_CONFIDENCE,
            file: relativeFile,
            loc: { line: pos.line, col: pos.column },
          },
        ],
      });
    }
  }

  return { hookNodes, stateNodes, edges: [...edges.values()], callbackOwners, warnings };
}

/** 沿父链找包住调用表达式的变量声明；只走 3 层以内（初始化表达式 → 声明）。 */
function findOwningVariableDeclaration(call: CallExpression): Node | null {
  let current: Node | undefined = call.getParent();
  let depth = 0;
  while (current && depth < 4) {
    if (MorphNode.isVariableDeclaration(current)) return current;
    if (MorphNode.isStatement(current)) return null;
    current = current.getParent();
    depth += 1;
  }
  return null;
}

/**
 * 取变量声明绑定的名字；解构（`const [x] = ...`）返回 null。
 *
 * 只有当初始化表达式**就是**这个调用时才用绑定名：`const handleSelect = useCallback(...)`
 * 显示 `handleSelect` 比显示 `useCallback` 有用。而
 * `const rows = cond ? useProTableData(x) : []` 里 `rows` 是整个三元表达式的结果，
 * 拿它命名 hook 节点会让人以为 hook 直接产出 rows。
 */
function readBoundVariableName(call: CallExpression): string | null {
  const parent = call.getParent();
  if (!parent || !MorphNode.isVariableDeclaration(parent)) return null;
  if (parent.getInitializer()?.getStart() !== call.getStart()) return null;
  const nameNode = parent.getNameNode();
  return MorphNode.isIdentifier(nameNode) ? nameNode.getText() : null;
}

/**
 * 解构 hook 返回值的绑定名（`const { onSubmit } = useLogin()` → ["onSubmit", ...]）。
 *
 * 自定义 hook 的返回值经解构进入组件作用域，事件绑定的具名引用
 * （`onSubmit={onSubmit}`）命中的是绑定名而不是 hook 名；不登记这些名字，
 * 事件的落点就断了。只收顶层绑定；嵌套解构（`const [{ a }] = useX()`）
 * 不在一期范围内。
 */
function readDestructuredBindingNames(declaration: Node | null): string[] {
  if (!declaration || !MorphNode.isVariableDeclaration(declaration)) return [];
  const nameNode = declaration.getNameNode();
  if (!MorphNode.isObjectBindingPattern(nameNode)) return [];

  const names: string[] = [];
  for (const element of nameNode.getElements()) {
    if (element.getDotDotDotToken()) continue;
    const elementName = element.getNameNode();
    if (MorphNode.isIdentifier(elementName)) names.push(elementName.getText());
  }
  return names;
}

/**
 * 自定义 hook 定义体内「绑定到变量名的回调体」的归属登记。
 *
 * DI 架构里请求链路穿过自定义 hook：`const { onSubmit } = useLogin()` 在组件里，
 * 而 `sessionService.login(...)` 在 useLogin.ts 的 handleSubmit 回调体里。回调体
 * 位置若不归属到 hook 调用点节点，api 分析会把其中的调用归到「模块级函数」
 * 名下——它与事件落点（hook 节点）之间没有边，链条在图上断开。
 *
 * 登记语义与 useCallback 回调体完全一致：hook 调用点节点代表「这个 hook 实例
 * 在这个组件里」，其内部绑定回调的执行时机由消费者（事件）决定。
 * 只扫一层：hook 定义体的顶层语句，不递归、不追 hook 调 hook 的链。
 */
function registerCustomHookCallbackBodies(
  call: CallExpression,
  hookNodeId: string,
  rootPath: string,
  callbackOwners: Map<string, string>,
): void {
  const callee = call.getExpression();
  if (!MorphNode.isIdentifier(callee)) return;

  // 调用点标识符的符号是 import 别名（声明为 ImportSpecifier），必须穿透
  // 才能拿到 hook 定义处的 FunctionDeclaration——与 api.ts 的同名处理一致
  let symbol = callee.getSymbol();
  if (symbol?.isAlias()) {
    try {
      symbol = symbol.getAliasedSymbol();
    } catch {
      return;
    }
  }

  for (const decl of symbol?.getDeclarations() ?? []) {
    if (!MorphNode.isFunctionDeclaration(decl)) continue;
    const defFile = decl.getSourceFile();
    if (!isProjectSource(rootPath, defFile.getFilePath())) continue;
    const relativeFile = toProjectRelative(rootPath, defFile.getFilePath());
    if (relativeFile === null) continue;

    for (const inner of collectTopLevelCalls(decl)) {
      // 只登记绑定到名字的回调（`const onSubmit = handleSubmit(fn)`）：
      // 没有名字就没有节点身份可归属，与顶层 hook 的登记规则一致
      if (!readBoundVariableName(inner)) continue;
      const callback = callbackFunctionOf(inner);
      if (!callback) continue;
      callbackOwners.set(positionKey(relativeFile, callback.getStart()), hookNodeId);
    }
  }
}

interface BuildStateNodeInput {
  rootPath: string;
  component: DiscoveredComponent;
  declaration: Node;
  call: CallExpression;
  sourceFile: SourceFile;
  relativeFile: string;
  warnings: Warning[];
  edges: Map<string, IREdge>;
}

/** 构造一个 state 节点，并按引用位置补 reads / writes 边。 */
function buildStateNode({
  rootPath,
  component,
  declaration,
  call,
  sourceFile,
  relativeFile,
  warnings,
  edges,
}: BuildStateNodeInput): IRNode | null {
  const binding = readStateBinding(declaration);
  // `let x = useState()` 这类写法不是 React 的状态定义：没有值/setter 的解构
  if (!binding) return null;

  const stateName = binding.valueName.getText();
  const setterText = binding.setterName?.getText() ?? null;

  const pos = sourceFile.getLineAndColumnAtPos(declaration.getStart());
  const nodeId = nodeIdForState(component.id, stateName);

  const readRefs = findReferences(binding.valueName, warnings, `状态 ${stateName}`);
  const writeRefs = binding.setterName
    ? findReferences(binding.setterName, warnings, `setter ${setterText}`)
    : [];

  const initial = call.getArguments()[0]?.getText() ?? null;

  const stateNode: IRNode = {
    id: nodeId,
    kind: "state",
    name: stateName,
    file: relativeFile,
    loc: { line: pos.line, col: pos.column },
    attrs: {
      // 与 hook 节点同名，前端可以用同一套字段读两种节点
      hookName: STATE_HOOK,
      setter: setterText,
      typeText: extractStateTypeText(call),
      initial,
    },
    evidence: [
      {
        source: "static",
        confidence: CALL_CONFIDENCE,
        file: relativeFile,
        loc: { line: pos.line, col: pos.column },
      },
    ],
  };

  // reads / writes 各自最多一条边，多个引用位置都进 evidence。
  // 证据数组在契约里必须非空，取不到位置就不产边——宁可少一条边，也不塞一条
  // 指向不存在的证据。
  const readEvidence = toEvidence(rootPath, readRefs);
  if (readEvidence.length > 0) {
    addEdge(edges, {
      id: `e_reads_${component.id}__${nodeId}`,
      from: component.id,
      to: nodeId,
      kind: "reads",
      evidence: readEvidence as [Evidence, ...Evidence[]],
    });
  }

  const writeEvidence = toEvidence(rootPath, writeRefs);
  if (writeEvidence.length > 0) {
    addEdge(edges, {
      id: `e_writes_${component.id}__${nodeId}`,
      from: component.id,
      to: nodeId,
      kind: "writes",
      evidence: writeEvidence as [Evidence, ...Evidence[]],
    });
  }

  // 声明了却完全没读写：用一条 uses 边保住节点在图上的可达性，不至于成为孤立点。
  if (readEvidence.length === 0 && writeEvidence.length === 0) {
    addEdge(edges, {
      id: `e_uses_${component.id}__${nodeId}`,
      from: component.id,
      to: nodeId,
      kind: "uses",
      evidence: [
        {
          source: "static",
          confidence: CALL_CONFIDENCE,
          file: relativeFile,
          loc: { line: pos.line, col: pos.column },
        },
      ],
    });
  }

  return stateNode;
}

function addEdge(edges: Map<string, IREdge>, edge: IREdge): void {
  if (!edges.has(edge.id)) edges.set(edge.id, edge);
}
