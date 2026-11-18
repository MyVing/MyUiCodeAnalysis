/**
 * API 链路分析：把「谁在什么时候发了什么请求」还原成 api 节点与 requests 边。
 *
 * ## 核心难点：请求往往不直接写在组件里
 *
 * 企业项目几乎不会在组件里直接 `fetch()`，而是
 *
 *   UserList → fetchUsers() → request({ url }) → fetch(`${url}${query}`)
 *
 * URL 字面量出现在**调用封装器的地方**，而不是 `fetch` 所在的地方。所以不能
 * 只扫 `fetch(...)`：那样只能拿到 `` `${url}${query}` `` 这种毫无信息的模板。
 *
 * ## 因此分三类节点判定，每类只回答自己能确定的事
 *
 * 1. **直接请求点**：调用已知 HTTP 客户端（`fetch` / `axios` / `ky` ...），且
 *    URL 实参能确定为字面量。此时 URL 是确定的事实。
 *
 * 2. **请求封装器**：项目内函数，体内有直接请求点，但 URL 来自函数参数。
 *    记下「第几个参数、哪个属性是 URL」（`request` → 参数 0 的 `url`）。
 *    它自己**不产** api 节点——真正的 URL 在调用点。
 *
 * 3. **封装器的调用点**：按槽位从实参里取 URL，产出 api 节点。
 *
 * ## 为什么「已知 HTTP 客户端」是一份受控名单而不是名字猜测
 *
 * README 明确禁止 `if (tag === "ProTable")` 这类业务名判断。名单里是
 * `fetch` / `axios` / `ky` 这类**生态层客户端**，且额外要求符号来自项目外
 * （lib.d.ts 或 node_modules）——项目里自己写一个叫 `request` 的函数不会被
 * 误认成客户端，它的符号在项目内。
 */

import type { CallExpression, Node, Project, SourceFile } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { Evidence, IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import { findOwner } from "./ownership.js";
import { isProjectSource, toProjectRelative } from "./paths.js";
import { positionKey } from "./positions.js";

/**
 * 已知的 HTTP 客户端调用名。
 *
 * 只放生态层的通用客户端，不放任何业务化的封装名（`request` / `http` /
 * `ajax` 之类）。业务封装由"请求封装器"机制识别，用不着列举。
 */
const HTTP_CLIENTS = new Set(["fetch", "axios", "ky", "got"]);

/** 从实参对象里提取 URL 的候选属性名（按优先级）。 */
const URL_PROPERTY_NAMES = ["url", "uri", "endpoint", "path"];

/** 从实参对象里提取 HTTP 方法的候选属性名。 */
const METHOD_PROPERTY_NAMES = ["method", "type", "verb"];

/** URL 直接是字面量：语法事实。 */
const DIRECT_CONFIDENCE = 1;
/** 经封装器转一手：URL 与请求点的对应关系靠参数流转推出来。 */
const INDIRECT_CONFIDENCE = 0.9;
/** 通过局部变量间接赋值（`const u = "/api/x"; request({ url: u })`）。 */
const LOCAL_VAR_CONFIDENCE = 0.8;

/**
 * 封装链深度上限。
 *
 * 不动点循环每轮最多发现一层，理论上限是链长；设一个上界是为了防止
 * 互相转发的两个函数（A→B、B→A）让循环空转。真实项目里三四层封顶。
 */
const MAX_WRAPPER_ROUNDS = 8;

/** 无任何显式方法信息时的事实默认值（HTTP 客户端不写 method 即 GET）。 */
const DEFAULT_METHOD = "GET";

/** 一个已定位的请求。 */
interface RequestSite {
  /** URL 文本（已拼成规范化形式，不含 query） */
  url: string;
  /**
   * HTTP 方法。总会有值：显式写了就是写的那个，没写就是 GET——
   * 这是 HTTP 的语义默认值，不是猜测。所以不存在 "ANY" 这种不确定态。
   */
  method: string;
  /** 该请求的全部证据 */
  evidence: Evidence[];
  /** URL 来自源码里的原始写法；与 url 不同时前端可以并排展示 */
  urlRaw?: string;
}

/** 请求封装器的签名：怎么从调用实参里取出 URL 与 method。 */
interface WrapperSignature {
  /** 封装器所在文件（相对路径） */
  file: string;
  /** 封装器函数名 */
  name: string;
  urlSlot: { paramIndex: number; propertyName: string | null };
  methodSlot: { paramIndex: number; propertyName: string | null; fallback: string | null } | null;
}

export interface ApiAnalysisResult {
  /** `kind: "api"` 节点，按 method + url 去重 */
  apiNodes: IRNode[];
  /** `kind: "fn"` 节点：既不是组件、又处在请求链路上的函数（如 fetchUsers） */
  fnNodes: IRNode[];
  edges: IREdge[];
  warnings: Warning[];
}

export interface ApiAnalysisInput {
  rootPath: string;
  project: Project;
  /** 定义位置键 → 组件 id，与 renders 共用 */
  definitionIndex: ReadonlyMap<string, string>;
}

/** 去重后的 URL 文本；空串视为无法确定。 */
function normalizeUrlText(text: string): string | null {
  const trimmed = text.trim();
  if (trimmed.length === 0) return null;
  // 模板里可能有前导空白，但 URL 内部空格不是合法字符
  const compact = trimmed.replace(/\s+/g, "");
  return compact.length === 0 ? null : compact;
}

/**
 * 从一个表达式节点提取 URL 文本。
 *
 * 只认三种形态：
 *  - 字符串字面量 `"/api/users"`
 *  - 无插值模板 `` `/api/users` ``
 *  - 带插值的模板，字面量骨架非空时保留插值形态
 *
 * 带插值时的两条规则彼此不同，很容易写错：
 *
 *  - **路径参数**（`` `/api/users/${id}` ``）保留原样：只有这样 `/users/1` 与
 *    `/users/2` 才会归到同一个接口，反查时也才能按段归一化成 `:param`。
 *  - **前缀常量**（`` `${API_PREFIX}/orders` ``）必须在读取阶段被求值：它决定
 *    的是"这是哪个接口"，不是"哪一个资源实例"。求不出来时**不猜**——见
 *    `resolveUrlExpression`。
 *
 * 认不出来的（变量、函数调用、拼接）一律返回 null，由上层退回符号追踪——
 * 禁止把变量名当成 URL 写进图里。
 */
function readLiteralUrl(node: Node): string | null {
  return readUrl(node).text;
}

/** URL 读取结果：除了文本，还要告诉调用方前缀有没有求出来。 */
interface UrlReadResult {
  /** 规范化后的 URL；无法确定时为 null */
  text: string | null;
  /** 模板里存在未能求值的插值，且位置在路径参数之前（URL 主体不确定） */
  unresolvedPrefix: boolean;
  /** 原始源码文本，供 attrs 回显（求值失败时用户能看到真实写法） */
  raw: string;
}

function readUrl(node: Node): UrlReadResult {
  if (MorphNode.isStringLiteral(node) || MorphNode.isNoSubstitutionTemplateLiteral(node)) {
    return { text: normalizeUrlText(node.getLiteralText()), unresolvedPrefix: false, raw: node.getText() };
  }

  if (MorphNode.isTemplateExpression(node)) {
    return readTemplateUrl(node);
  }

  return { text: null, unresolvedPrefix: false, raw: node.getText() };
}

/**
 * 模板表达式 → URL。
 *
 * 把模板拆成「字面量段 + 插值段」后逐个插值尝试求值：
 *
 *  - 求出来的值拼进字面量骨架（`` `${API_PREFIX}/orders` `` → `/api/orders`）；
 *  - **路径参数**原样保留成插值占位（`` ${id} `` → `${…}`），不展开也不丢弃；
 *  - 求不出来且**位于路径参数之前**的插值视为前缀未解析：URL 主体不确定，
 *    整个模板判为无法确定（返回 null），由调用方记告警。宁可缺一个接口节点，
 *    也不能把 `${…}/orders` 当成一个真实接口参与反查——那会系统性误匹配。
 */
function readTemplateUrl(node: Node): UrlReadResult {
  if (!MorphNode.isTemplateExpression(node)) {
    return { text: null, unresolvedPrefix: false, raw: node.getText() };
  }

  const head = node.getHead().getLiteralText();
  const spans = node.getTemplateSpans();

  const parts: string[] = [];
  let unresolvedPrefix = false;
  // 尚未遇到任何「路径参数」插值：此前的插值都按前缀处理
  let beforePathParam = true;

  if (head.length > 0) parts.push(head);

  for (const span of spans) {
    const expression = span.getExpression();
    const literal = span.getLiteral().getLiteralText();

    const resolved = tryEvaluatePrefixExpression(expression);
    if (resolved !== null) {
      parts.push(resolved);
    } else if (isUrlParameter(expression)) {
      // 路径 / 查询参数：保留插值形态，后面的字面量一起拼回骨架
      parts.push("${…}");
      beforePathParam = false;
    } else {
      // 既不是路径参数、也求不出来：它就是 URL 里的一段未知前缀
      if (beforePathParam) unresolvedPrefix = true;
      parts.push(`\${${expression.getText()}}`);
    }

    if (literal.length > 0) parts.push(literal);
  }

  const skeleton = parts.join("");
  if (!normalizeUrlText(skeleton)) {
    return { text: null, unresolvedPrefix, raw: node.getText() };
  }

  // 前缀没求出来 → 整个 URL 主体不确定，不产节点
  if (unresolvedPrefix) {
    return { text: null, unresolvedPrefix: true, raw: node.getText() };
  }

  return {
    text: normalizeUrlText(skeleton),
    unresolvedPrefix: false,
    raw: node.getText(),
  };
}

/**
 * 判断插值是不是「URL 里的一段动态值」，而不是「URL 主体」。
 *
 * 包括两种：路径参数（`/users/${id}`）与查询参数（`/users?status=${status}`）。
 * 它们的共同点是**前一段字面量里已经有 `/`**，说明地址主体已经写死在源码里，
 * 插值只是指向某个具体资源或筛选条件。
 *
 * 判据不是变量名（`id` / `userId` / `key` 这类名字全是业务约定），而是**位置与
 * 上下文**。这条判据同时用在两个地方，含义必须一致：
 *  - `readTemplateUrl`：这类插值保留为占位，不展开也不丢弃；
 *  - `traceUrlToParameter`：这类插值**不能**让函数被误判成 URL 封装器。
 */
function isUrlParameter(expression: Node): boolean {
  if (!MorphNode.isIdentifier(expression) && !MorphNode.isPropertyAccessExpression(expression)) {
    return false;
  }

  // 取它在模板中的前一段字面量：有 `/` 才算地址主体已写死
  const span = expression.getParent();
  if (!span) return false;

  const previousLiteral = previousTemplateLiteral(span);
  return previousLiteral !== null && previousLiteral.includes("/");
}

/** 模板片段之前最近的一段字面量文本；在 head 处返回 head 文本。 */
function previousTemplateLiteral(span: Node): string | null {
  const template = span.getParent();
  if (!template || !MorphNode.isTemplateExpression(template)) return null;

  const spans = template.getTemplateSpans();
  const index = spans.indexOf(span as (typeof spans)[number]);
  if (index < 0) return null;
  if (index === 0) return template.getHead().getLiteralText();
  return spans[index - 1]?.getLiteral().getLiteralText() ?? null;
}

/**
 * 尝试把插值表达式静态求值成字符串（用于 URL 前缀）。
 *
 * 支持三种企业项目里的常见形态，**只认能确定的值**：
 *  - 字符串字面量：`const BASE = "/api"`（含跨文件 import，靠符号解析回溯）
 *  - `import.meta.env.X ?? "/api"`：取 fallback 值，env 本身运行时才知道
 *  - 纯字面量拼接：`const BASE = "/api" + "/v1"`
 *
 * 函数返回值、运行时计算、模板里嵌套的未解析变量一律返回 null——那是数据流
 * 分析范畴，一期不做，也不该用猜测糊过去。
 */
function tryEvaluatePrefixExpression(node: Node, depth = 0): string | null {
  if (depth > 3) return null;

  if (MorphNode.isStringLiteral(node) || MorphNode.isNoSubstitutionTemplateLiteral(node)) {
    return node.getLiteralText();
  }

  if (MorphNode.isParenthesizedExpression(node)) {
    return tryEvaluatePrefixExpression(node.getExpression(), depth + 1);
  }

  // import.meta.env.X ?? "/api" —— env 运行时才知道，取显式 fallback
  if (MorphNode.isBinaryExpression(node)) {
    const operator = node.getOperatorToken().getText();
    if (operator === "??" || operator === "||") {
      const right = tryEvaluatePrefixExpression(node.getRight(), depth + 1);
      if (right !== null) return right;
      return tryEvaluatePrefixExpression(node.getLeft(), depth + 1);
    }
    if (operator === "+") {
      const left = tryEvaluatePrefixExpression(node.getLeft(), depth + 1);
      const right = tryEvaluatePrefixExpression(node.getRight(), depth + 1);
      if (left !== null && right !== null) return left + right;
      return null;
    }
  }

  if (MorphNode.isIdentifier(node) || MorphNode.isPropertyAccessExpression(node)) {
    return evaluateSymbol(node);
  }

  return null;
}

/**
 * 沿符号声明回溯跨文件 / 同文件的前缀常量。
 *
 * 与 `resolveIdentifier` 的区别：那个只用于「找 URL 字面量节点」，这个要返回
 * **求值后的字符串**，因此必须递归穿透中间变量与 import 别名。
 *
 * 两种声明形态都要认，它们在真实项目里同样常见：
 *  - `const BASE = "/api"`         → VariableDeclaration
 *  - `API_URLS.JSON_PLACEHOLDER`   → PropertyAssignment（`as const` 常量表）
 *
 * 常量表那条必须走符号解析而不是按名字猜：`API_URLS.FAKE_STORE` 里的
 * `API_URLS` 可能来自任意一个文件，只有 TS 自己解析出的声明位置才可靠。
 */
function evaluateSymbol(node: Node): string | null {
  const symbol = node.getSymbol();
  if (!symbol) return null;

  let target = symbol;
  if (target.isAlias()) {
    try {
      const aliased = target.getAliasedSymbol();
      // 别名链断裂（模块未解析）时 aliased 可能为 undefined：按「求不出来」处理
      if (!aliased) return null;
      target = aliased;
    } catch {
      return null;
    }
  }

  for (const decl of target.getDeclarations() ?? []) {
    // 只取初始化表达式：声明类型（`const x: string`）不含值
    const init = MorphNode.isVariableDeclaration(decl)
      ? decl.getInitializer()
      : MorphNode.isPropertyAssignment(decl)
        ? decl.getInitializer()
        : undefined;
    if (!init) continue;

    // 防止自引用造成的无限递归：`const a = a`
    if (init === node || init.containsRange(node.getStart(), node.getEnd())) continue;

    const value = tryEvaluatePrefixExpression(init, 1);
    if (value !== null) return value;
  }
  return null;
}

/**
 * 沿符号声明回溯：把标识符还原成它绑定的表达式。
 *
 * 只沿变量初始化表达式回溯，最多三层。再深的链路（跨函数返回值、跨模块常量）
 * 一期不追——追下去就是完整的数据流分析，成本与收益不成比例。
 */
function resolveIdentifier(node: Node, depth = 0): { node: Node; indirect: boolean } | null {
  if (depth > 3) return null;
  if (!MorphNode.isIdentifier(node)) return null;

  const symbol = node.getSymbol();
  for (const decl of symbol?.getDeclarations() ?? []) {
    // `const url = "/api/x"`
    if (MorphNode.isVariableDeclaration(decl)) {
      const init = decl.getInitializer();
      if (!init) continue;
      if (MorphNode.isStringLiteral(init) || MorphNode.isTemplateExpression(init) ||
          MorphNode.isNoSubstitutionTemplateLiteral(init)) {
        return { node: init, indirect: true };
      }
      const deeper = resolveIdentifier(init, depth + 1);
      if (deeper) return { node: deeper.node, indirect: true };
      continue;
    }
  }
  return null;
}

/** 从对象字面量实参里按候选属性名取属性的初始化表达式。 */
function findPropertyInitializer(
  objectLiteral: Node,
  names: readonly string[],
): { name: string; node: Node } | null {
  if (!MorphNode.isObjectLiteralExpression(objectLiteral)) return null;

  for (const name of names) {
    for (const prop of objectLiteral.getProperties()) {
      if (MorphNode.isPropertyAssignment(prop) && prop.getName() === name) {
        const init = prop.getInitializer();
        if (init) return { name, node: init };
      }
      // 简写属性 `{ url }`
      if (MorphNode.isShorthandPropertyAssignment(prop) && prop.getName() === name) {
        return { name, node: prop.getNameNode() };
      }
    }
  }
  return null;
}

/**
 * 判定调用是否指向已知 HTTP 客户端。
 *
 * 两道条件同时满足才算：名字在受控名单里，且符号声明在项目外。
 * 只看名字会把项目内自定义的 `fetch` 包装误判；只看位置又会把任何第三方
 * 函数（如 lodash 的 `get`）都当成客户端。
 */
function isHttpClientCall(call: CallExpression, rootPath: string): boolean {
  const expr = call.getExpression();

  // `axios.get(...)` 取方法名；`fetch(...)` 取标识符
  if (!MorphNode.isIdentifier(expr) && !MorphNode.isPropertyAccessExpression(expr)) return false;

  const name = MorphNode.isIdentifier(expr) ? expr.getText() : expr.getName();

  const symbol = expr.getSymbol();
  const decls: Node[] = symbol?.getDeclarations() ?? [];
  if (decls.length === 0) return false;

  // 不能用 toProjectRelative 判空：node_modules 就在 rootPath 之下，
  // 相对路径非空但它显然不是项目源码。isProjectSource 同时排除依赖与 .d.ts。
  const allExternal = decls.every(
    (decl) => !isProjectSource(rootPath, decl.getSourceFile().getFilePath()),
  );
  if (!allExternal) return false;

  // `axios.get` 的根标识符是 axios；`fetch` 本身就是客户端
  if (MorphNode.isPropertyAccessExpression(expr)) {
    const rootName = expr.getExpression().getText().split(".")[0] ?? "";
    return HTTP_CLIENTS.has(rootName);
  }
  return HTTP_CLIENTS.has(name);
}

/**
 * 从一次 HTTP 客户端调用里取 URL 实参。
 *
 * `axios.get("/api/x")` 与 `fetch("/api/x", opts)` 的第一个实参都是 URL；
 * `axios({ url: "/api/x" })` 则是对象的 url 属性。
 */
function getUrlArgumentNode(call: CallExpression): Node | null {
  const [first] = call.getArguments();
  if (!first) return null;
  if (MorphNode.isObjectLiteralExpression(first)) {
    return findPropertyInitializer(first, URL_PROPERTY_NAMES)?.node ?? null;
  }
  return first;
}

/** 从 HTTP 客户端的第二个实参（options 对象）里取 method。 */
function getMethodFromOptions(call: CallExpression): string | null {
  const [, options] = call.getArguments();
  if (!options) return null;
  const found = findPropertyInitializer(options, METHOD_PROPERTY_NAMES);
  if (!found) return null;
  return readLiteralUrl(found.node);
}

/** 一个 AST 节点所属的最内层函数节点；不在函数内返回 null。 */
function findEnclosingFunction(node: Node): Node | null {
  let current: Node | undefined = node.getParent();
  while (current) {
    if (
      MorphNode.isFunctionDeclaration(current) ||
      MorphNode.isFunctionExpression(current) ||
      MorphNode.isArrowFunction(current) ||
      MorphNode.isMethodDeclaration(current)
    ) {
      return current;
    }
    current = current.getParent();
  }
  return null;
}

/**
 * 函数节点的名字；匿名函数返回 null。
 *
 * 匿名内联回调是常见写法，但它在图里必须有个稳定身份，否则「请求发生在哪个
 * 函数里」无从记录，整条接口链路会静默丢失：
 *
 *   const fetchPosts = useCallback(async () => { await fetch(...) }, [])
 *
 * 这里的 `useCallback(...)` 首参就是那个回调，语义上属于 `fetchPosts`。
 * 判据是「函数是某个调用表达式的第一个实参，且该调用被赋给变量/属性」——
 * 这是把回调绑定到名字的通用形态，不依赖 useCallback 这类具体 API 名。
 */
function getFunctionName(fn: Node): string | null {
  if (MorphNode.isFunctionDeclaration(fn)) return fn.getName() ?? null;
  const parent = fn.getParent();
  if (parent && MorphNode.isVariableDeclaration(parent)) return parent.getName();
  if (parent && MorphNode.isPropertyAssignment(parent)) return parent.getName();

  // `const name = useCallback(fn)` / `const name = useMemo(() => ..., [])`
  if (parent && MorphNode.isCallExpression(parent) && parent.getArguments()[0] === fn) {
    const grandParent = parent.getParent();
    if (grandParent && MorphNode.isVariableDeclaration(grandParent)) return grandParent.getName();
    if (grandParent && MorphNode.isPropertyAssignment(grandParent)) return grandParent.getName();
  }
  return null;
}

/**
 * 找出「函数参数的哪个位置承载 URL」。
 *
 * 用于把 `function request(options) { ... fetch(`${url}${query}`) ... }`
 * 里的 `url` 回溯到 `options` 这个参数，进而知道调用方应当传
 * `request({ url: ... })`。
 *
 * URL 实参常常是模板表达式（`` `${url}${query}` ``），参数在插值里而不是
 * 实参本身。先拆到插值，再逐个回溯；有任何一段来自参数就认定是封装器——
 * 封装器的本质就是"URL 主体由调用方给"。
 */
function traceUrlToParameter(
  urlNode: Node,
  enclosing: Node,
): { paramIndex: number; propertyName: string | null } | null {
  if (MorphNode.isTemplateExpression(urlNode) || MorphNode.isBinaryExpression(urlNode)) {
    for (const operand of getUrlOperands(urlNode)) {
      // 路径 / 查询参数不是 URL 主体：`fetch(`/api/users/${id}`)` 里的 `id`
      // 之于这个 URL，就像 `/users/1` 里的 `1`。把它当成「URL 由调用方给」
      // 会让这个函数被误判成封装器，真正的请求点被整条跳过——连告警都没有。
      if (isUrlParameter(operand)) continue;

      const hit = traceUrlToParameter(operand, enclosing);
      if (hit) return hit;
    }
    return null;
  }

  // 形如 `options.path`：属性直接挂在参数对象上
  if (MorphNode.isPropertyAccessExpression(urlNode)) {
    const target = urlNode.getExpression();
    if (!MorphNode.isIdentifier(target)) return null;
    const param = findParameterIndex(enclosing, target);
    if (param === null) return null;
    return { paramIndex: param, propertyName: urlNode.getName() };
  }

  if (!MorphNode.isIdentifier(urlNode)) return null;

  const symbol = urlNode.getSymbol();
  for (const decl of symbol?.getDeclarations() ?? []) {
    // 形如 `const { url } = options`：url 是 BindingElement
    if (MorphNode.isBindingElement(decl)) {
      const pattern = decl.getParent();
      if (!MorphNode.isObjectBindingPattern(pattern)) continue;
      const varDecl = pattern.getParent();
      if (!MorphNode.isVariableDeclaration(varDecl)) continue;
      const init = varDecl.getInitializer();
      if (!init || !MorphNode.isIdentifier(init)) continue;

      const param = findParameterIndex(enclosing, init);
      if (param !== null) {
        return { paramIndex: param, propertyName: decl.getName() };
      }
      continue;
    }

    // 形如 `function request(url: string)`：url 是参数本身
    if (MorphNode.isParameterDeclaration(decl)) {
      const param = findParameterIndex(enclosing, decl.getNameNode());
      if (param !== null) return { paramIndex: param, propertyName: null };
    }
  }
  return null;
}

/** 拆出 URL 节点的组成部分：模板插值表达式、字符串拼接的操作数。 */
function getUrlOperands(node: Node): Node[] {
  if (MorphNode.isTemplateExpression(node)) {
    return node.getTemplateSpans().map((span) => span.getExpression());
  }
  if (MorphNode.isBinaryExpression(node)) {
    return [node.getLeft(), node.getRight()];
  }
  return [node];
}

/**
 * 参数名节点对应在函数参数列表里的下标。
 *
 * 按**名字**比较，不按位置：`nameNode` 可能是参数声明本身，也可能是
 * `{ url } = options` 里对参数的**引用**，两者的字符偏移不同（一个在签名里，
 * 一个在函数体里）。引用与声明指向同一符号，名字必然相同。
 */
function findParameterIndex(fn: Node, nameNode: Node): number | null {
  const name = MorphNode.isIdentifier(nameNode) ? nameNode.getText() : null;
  if (!name) return null;

  const params = getParameterList(fn);
  const index = params.findIndex((p) => p.getName() === name);
  return index === -1 ? null : index;
}

function getParameterList(fn: Node) {
  if (
    MorphNode.isFunctionDeclaration(fn) ||
    MorphNode.isFunctionExpression(fn) ||
    MorphNode.isArrowFunction(fn) ||
    MorphNode.isMethodDeclaration(fn)
  ) {
    return fn.getParameters();
  }
  return [];
}

/**
 * 找函数里 method 的解构默认值（`const { method = "GET" } = options`）。
 *
 * 取不到就返回 null，由调用点决定是否用 GET 兜底——不能在这里假设默认值。
 */
function traceMethodDefault(fn: Node): { paramIndex: number; propertyName: string | null; fallback: string | null } | null {
  for (const decl of fn.getDescendantsOfKind(SyntaxKind.BindingElement)) {
    const name = decl.getName();
    if (!METHOD_PROPERTY_NAMES.includes(name)) continue;

    const initializer = decl.getInitializer();
    const fallback = initializer ? readLiteralUrl(initializer) : null;
    if (!fallback) continue;

    const pattern = decl.getParent();
    if (!MorphNode.isObjectBindingPattern(pattern)) continue;
    const varDecl = pattern.getParent();
    if (!MorphNode.isVariableDeclaration(varDecl)) continue;
    const init = varDecl.getInitializer();
    if (!init || !MorphNode.isIdentifier(init)) continue;

    const paramIndex = findParameterIndex(fn, init);
    if (paramIndex === null) continue;

    return { paramIndex, propertyName: name, fallback };
  }
  return null;
}

/**
 * 从函数体推导封装器签名，两种情形都算：
 *
 *  - 体内直接调用 HTTP 客户端，且 URL 追溯到自己某个参数
 *  - 体内调用**已知的**封装器，且传给它的 URL 追溯到自己某个参数
 *
 * 第二种让 `getUser → send → fetch` 这类两层封装也能识别。它天然是迭代的：
 * 内层封装器必须已经被识别，外层才认得出来，因此调用方要做不动点循环。
 */
function buildWrapperSignature(
  fn: Node,
  wrappers: ReadonlyMap<string, WrapperSignature>,
  rootPath: string,
): WrapperSignature | null {
  const relativeFile = toProjectRelative(rootPath, fn.getSourceFile().getFilePath());
  if (relativeFile === null) return null;

  const name = getFunctionName(fn);
  if (!name) return null;

  const self: WrapperSignature = {
    file: relativeFile,
    name,
    urlSlot: { paramIndex: 0, propertyName: null },
    methodSlot: traceMethodDefault(fn),
  };

  // 只看直属语句，不跨越嵌套函数：嵌套函数里的请求属于它自己
  for (const call of findCallsExcludingNestedFunctions(fn)) {
    // 情形一：直接请求点
    if (isHttpClientCall(call, rootPath)) {
      const urlArg = getUrlArgumentNode(call);
      if (!urlArg) continue;

      const slot = traceUrlToParameter(urlArg, fn);
      if (!slot) continue;

      return { ...self, urlSlot: slot, methodSlot: traceMethodDefault(fn) };
    }

    // 情形二：转发给已知封装器
    const calleeName = getCalleeName(call);
    if (!calleeName) continue;
    const inner = findWrapperForCall(call, calleeName, wrappers, rootPath);
    if (!inner) continue;

    const fwdArg = call.getArguments()[inner.urlSlot.paramIndex];
    if (!fwdArg) continue;
    const fwdTarget = inner.urlSlot.propertyName
      ? findPropertyInitializer(fwdArg, [inner.urlSlot.propertyName])?.node ?? null
      : fwdArg;
    if (!fwdTarget) continue;

    const slot = traceUrlToParameter(fwdTarget, fn);
    if (!slot) continue;

    // 内层的默认方法原样继承：外层没给 method 时，真正的请求仍用内层那个默认值
    const methodSlot = inner.methodSlot
      ? {
          paramIndex: slot.paramIndex,
          propertyName: inner.methodSlot.propertyName,
          fallback: inner.methodSlot.fallback,
        }
      : traceMethodDefault(fn);

    return { ...self, urlSlot: slot, methodSlot };
  }
  return null;
}

/** 收集函数体内的调用，不进入嵌套函数（与 hooks 的判定保持一致）。 */
function findCallsExcludingNestedFunctions(fn: Node): CallExpression[] {
  const body =
    MorphNode.isArrowFunction(fn) ||
    MorphNode.isFunctionExpression(fn) ||
    MorphNode.isFunctionDeclaration(fn) ||
    MorphNode.isMethodDeclaration(fn)
      ? fn.getBody()
      : undefined;
  if (!body) return [];

  const calls: CallExpression[] = [];
  const visit = (node: Node): void => {
    if (MorphNode.isCallExpression(node)) calls.push(node);
    if (
      MorphNode.isFunctionDeclaration(node) ||
      MorphNode.isFunctionExpression(node) ||
      MorphNode.isArrowFunction(node) ||
      MorphNode.isMethodDeclaration(node)
    ) {
      return;
    }
    for (const child of node.getChildren()) visit(child);
  };
  visit(body);
  return calls;
}

/** 从封装器调用的实参里取 URL 与 method。 */
function readWrapperCall(
  call: CallExpression,
  signature: WrapperSignature,
): { url: string; urlRaw?: string; node: Node; indirect: boolean; method: string } | null {
  const args = call.getArguments();
  const urlArg = args[signature.urlSlot.paramIndex];
  if (!urlArg) return null;

  // 槽位带属性名时，实参应当是对象字面量，取其属性
  let target: Node | null = null;
  if (signature.urlSlot.propertyName) {
    target = findPropertyInitializer(urlArg, [signature.urlSlot.propertyName])?.node ?? null;
  } else {
    target = urlArg;
  }
  if (!target) return null;

  let indirect = false;
  let read = readUrl(target);
  let urlNode = target;

  if (!read.text) {
    const resolved = resolveIdentifier(target);
    if (resolved) {
      read = readUrl(resolved.node);
      urlNode = resolved.node;
      indirect = true;
    }
  }
  if (!read.text) return null;
  const url = read.text;

  // method：优先取调用点显式传入的，其次取封装器的解构默认值，
  // 最后落到 HTTP 语义的默认值 GET（不写 method 的请求就是 GET，这是事实不是猜测）
  let method: string | null = null;
  const methodSlot = signature.methodSlot;
  if (methodSlot) {
    const methodArg = args[methodSlot.paramIndex];
    if (methodArg && methodSlot.propertyName) {
      const found = findPropertyInitializer(methodArg, [methodSlot.propertyName]);
      if (found) method = readLiteralUrl(found.node);
    }
  }
  const resolvedMethod = method ?? methodSlot?.fallback ?? DEFAULT_METHOD;

  return {
    url,
    ...(read.raw !== url ? { urlRaw: read.raw } : {}),
    node: urlNode,
    indirect,
    method: resolvedMethod,
  };
}

/** api 节点 id：同一接口被多处调用时合并成一个节点。 */
function apiNodeId(method: string, url: string): string {
  return `api:${method}:${url}`;
}

/**
 * 分析入口。
 *
 * 分成三步，每步只做一件事：
 *  1. 找出项目内所有「请求封装器」（体内有直接请求点、URL 来自自己参数的函数）
 *  2. 扫全部调用点，把请求归到「组件」或「模块级函数」名下
 *  3. 为出现在请求链上的模块级函数补 fn 节点，以及「谁调用了它」的 calls 边
 *
 * 第 3 步单独做，是因为第一遍扫描时还不知道哪些函数最终连到了请求上；
 * 边只能在信息齐了之后连，而不是边扫边猜。
 */
export function analyzeApi(input: ApiAnalysisInput): ApiAnalysisResult {
  const { rootPath, project, definitionIndex } = input;

  const warnings: Warning[] = [];
  const apiNodes = new Map<string, IRNode>();
  const fnNodes = new Map<string, IRNode>();
  const fnDeclarations = new Map<string, Node>();
  const edges = new Map<string, IREdge>();

  // --- 第一步：找出所有请求封装器 ---
  //
  // 不动点循环：封装器可能层层转发（getUser → send → fetch），内层必须先被识别
  // 外层才认得出来，而源文件顺序与依赖方向无关（字母序里 api.ts 排在 http.ts 前）。
  // 每轮最多发现一层，所以轮数上界是封装链深度；直到某轮没有任何新增为止。
  const wrappers = new Map<string, WrapperSignature>();
  const sourceFiles = project
    .getSourceFiles()
    .map((file) => ({ file, relativeFile: toProjectRelative(rootPath, file.getFilePath()) }))
    .filter(
      (item): item is { file: SourceFile; relativeFile: string } => item.relativeFile !== null,
    );

  for (let round = 0; round < MAX_WRAPPER_ROUNDS; round += 1) {
    let discovered = 0;
    for (const { file, relativeFile } of sourceFiles) {
      for (const fn of collectFunctions(file)) {
        const signature = buildWrapperSignature(fn, wrappers, rootPath);
        if (!signature) continue;

        const key = `${relativeFile}#${signature.name}`;
        if (wrappers.has(key)) continue;
        wrappers.set(key, signature);
        discovered += 1;
      }
    }
    if (discovered === 0) break;
  }

  /** 按 method + url 建 api 节点，并为 fromId 连一条 requests 边。 */
  const recordRequest = (fromId: string, site: RequestSite): void => {
    const nodeId = apiNodeId(site.method, site.url);

    // 请求点一定至少带一条证据；这里是一次显式断言而不是兜底编造
    const [first, ...rest] = site.evidence as [Evidence, ...Evidence[]];

    if (!apiNodes.has(nodeId)) {
      apiNodes.set(nodeId, {
        id: nodeId,
        kind: "api",
        name: `${site.method} ${site.url}`,
        // 接口是「端点」而不是源码实体：位置来自首次发现它的调用点，
        // 证据里没有位置时宁可缺省，也不编一个。
        ...(first.file ? { file: first.file } : {}),
        ...(first.loc ? { loc: first.loc } : {}),
        // 只放数据。诸如「位置只是首次发现处」这类说明对所有 api 节点都一样，
        // 是展示逻辑，交给前端固定文案，不重复塞进每个节点的 attrs。
        //
        // urlRaw 只在「源码写法与解析结果不同」时才写：用户点开接口想核对的是
        // `\`${API_PREFIX}/orders\`` 这个原文，而不是被求值后的 `/api/orders`。
        // 两者相同时不必重复存一份。
        attrs: {
          method: site.method,
          url: site.url,
          ...(site.urlRaw ? { urlRaw: site.urlRaw } : {}),
        },
        // 节点自身只承载一条证据；其余调用位置都进对应边的 evidence
        evidence: [first],
      });
    }

    const edgeId = `e_requests_${fromId}__${nodeId}`;
    const existing = edges.get(edgeId);
    if (existing) {
      // 同一个调用方重复请求同一接口：合并证据，不产生多条边
      existing.evidence.push(...site.evidence);
      return;
    }

    edges.set(edgeId, {
      id: edgeId,
      from: fromId,
      to: nodeId,
      kind: "requests",
      evidence: [first, ...rest],
    });
  };

  /** 记下「某个模块级函数发了请求」，稍后为它补节点。 */
  const registerFn = (fnId: string, fnNode: Node): void => {
    if (fnNodes.has(fnId)) return;
    const relativeFile = toProjectRelative(rootPath, fnNode.getSourceFile().getFilePath());
    if (relativeFile === null) return;

    const name = getFunctionName(fnNode);
    if (!name) return;

    const pos = fnNode.getSourceFile().getLineAndColumnAtPos(fnNode.getStart());
    fnNodes.set(fnId, {
      id: fnId,
      kind: "fn",
      name,
      file: relativeFile,
      loc: { line: pos.line, col: pos.column },
      attrs: { role: "请求链路上的函数" },
      evidence: [
        {
          source: "static",
          confidence: DIRECT_CONFIDENCE,
          file: relativeFile,
          loc: { line: pos.line, col: pos.column },
        },
      ],
    });
    fnDeclarations.set(fnId, fnNode);
  };

  // --- 第二步：扫全部调用点 ---
  for (const file of project.getSourceFiles()) {
    const callFile = toProjectRelative(rootPath, file.getFilePath());
    if (callFile === null) continue;

    for (const call of file.getDescendantsOfKind(SyntaxKind.CallExpression)) {
      const result = readRequestAtCall(call, rootPath, wrappers);

      if (result.kind === "unresolved") {
        // 前缀求不出来 → 不产 api 节点，但必须让用户看到"这里有个接口没解析出来"。
        // 静默跳过会让"该页面不调用此接口"变成一条错误结论。
        const pos = file.getLineAndColumnAtPos(call.getStart());
        warnings.push({
          code: "api_url_prefix_unresolved",
          severity: "warning",
          stage: "static",
          file: callFile,
          loc: { line: pos.line, col: pos.column },
          message:
            `${result.method} ${result.raw} 的 URL 前缀无法静态求值，未计入接口清单；` +
            "该页面可能存在未被记录的接口",
        });
        continue;
      }
      if (result.kind === "none") continue;

      const site = result.site;
      const ownerComponent = findOwner(call, definitionIndex, rootPath);
      const fromId = ownerComponent ?? moduleFunctionIdOf(call, rootPath, file);
      if (!fromId) continue;

      if (!ownerComponent) {
        const enclosing = findEnclosingFunction(call);
        if (enclosing) registerFn(fromId, enclosing);
      }

      recordRequest(fromId, site);
    }
  }

  // --- 第三步：为请求链路上的函数补 calls 边 ---
  //
  // 一次扫描全部调用点，而不是「每个函数扫一遍项目」——后者是
  // O(函数数 × 文件数)，项目一大会直接从秒级掉到分钟级。
  if (fnDeclarations.size > 0) {
    // 声明位置 → 函数 id。函数表达式与箭头函数挂在变量声明上，调用点的符号
    // 解析出的声明是那个变量而不是函数节点本身，所以两个位置都要登记。
    const callTargets = new Map<string, string>();
    for (const [fnId, fnNode] of fnDeclarations) {
      const fnFile = toProjectRelative(rootPath, fnNode.getSourceFile().getFilePath());
      if (fnFile === null) continue;
      callTargets.set(positionKey(fnFile, fnNode.getStart()), fnId);

      const parent = fnNode.getParent();
      if (
        parent &&
        (MorphNode.isVariableDeclaration(parent) || MorphNode.isPropertyAssignment(parent))
      ) {
        callTargets.set(positionKey(fnFile, parent.getStart()), fnId);
      }
    }

    for (const file of project.getSourceFiles()) {
      const relativeFile = toProjectRelative(rootPath, file.getFilePath());
      if (relativeFile === null) continue;

      for (const call of file.getDescendantsOfKind(SyntaxKind.CallExpression)) {
        const fnId = resolveCalledFunctionId(call, callTargets, rootPath);
        if (!fnId) continue;

        const callerId =
          findOwner(call, definitionIndex, rootPath) ??
          moduleFunctionIdOf(call, rootPath, file);
        // 自调用（递归）不产边；调用方不认识时也不编造
        if (!callerId || callerId === fnId) continue;

        const pos = file.getLineAndColumnAtPos(call.getStart());
        const evidence: Evidence = {
          source: "static",
          confidence: INDIRECT_CONFIDENCE,
          file: relativeFile,
          loc: { line: pos.line, col: pos.column },
        };

        const edgeId = `e_calls_${callerId}__${fnId}`;
        const existing = edges.get(edgeId);
        if (existing) {
          // 同一调用方在同一位置重复调用同一函数：合并证据，不产生多条边
          existing.evidence.push(evidence);
          continue;
        }

        edges.set(edgeId, {
          id: edgeId,
          from: callerId,
          to: fnId,
          kind: "calls",
          evidence: [evidence],
        });
      }
    }
  }

  return {
    apiNodes: [...apiNodes.values()],
    fnNodes: [...fnNodes.values()],
    edges: [...edges.values()],
    warnings,
  };
}

/** 取被调用名：`fetch` 取标识符；`axios.get` 取方法名；其他返回 null。 */
function getCalleeName(call: CallExpression): string | null {
  const expr = call.getExpression();
  if (MorphNode.isIdentifier(expr)) return expr.getText();
  if (MorphNode.isPropertyAccessExpression(expr)) return expr.getName();
  return null;
}

/**
 * 判断一次调用是否产出请求。
 *
 * 两种情形：
 *  - 直接请求点（调用已知 HTTP 客户端）。若它落在某个封装器体内，跳过——
 *    URL 在那里只是参数，由封装器的调用点负责产出真正的接口。
 *  - 封装器调用点，按签名从实参里取 URL。
 */
/** 请求点读取结果：要么是一个可建模的请求，要么是一条「前缀未解析」记录。 */
type RequestReadResult =
  | { kind: "site"; site: RequestSite }
  | { kind: "unresolved"; call: CallExpression; method: string; raw: string }
  | { kind: "none" };

function readRequestAtCall(
  call: CallExpression,
  rootPath: string,
  wrappers: ReadonlyMap<string, WrapperSignature>,
): RequestReadResult {
  if (isHttpClientCall(call, rootPath)) {
    const urlArg = getUrlArgumentNode(call);
    if (!urlArg) return { kind: "none" };

    // 封装器体内的请求点：URL 来自参数，此刻还定不下来
    const enclosing = findEnclosingFunction(urlArg);
    if (enclosing) {
      const enclosingFile = toProjectRelative(rootPath, enclosing.getSourceFile().getFilePath());
      const enclosingName = getFunctionName(enclosing);
      if (enclosingFile && enclosingName && wrappers.has(`${enclosingFile}#${enclosingName}`)) {
        return { kind: "none" };
      }
    }

    const site = buildDirectRequestSite(call, urlArg, rootPath);
    if (site) return { kind: "site", site };

    // URL 定不下来：区分「前缀求不出来」与「压根没写 URL」。
    // 前者是可解释的覆盖缺口（跨模块常量 / 运行时拼装），必须让用户看见；
    // 后者是正常跳过（如 `fetch(input)` 把整个 Request 对象传进来）。
    const resolved = resolveIdentifier(urlArg);
    const read = readUrl(resolved?.node ?? urlArg);
    if (read.unresolvedPrefix) {
      return {
        kind: "unresolved",
        call,
        method: getMethodFromOptions(call) ?? inferMethodFromCallee(call) ?? DEFAULT_METHOD,
        raw: read.raw,
      };
    }
    return { kind: "none" };
  }

  const calleeName = getCalleeName(call);
  if (!calleeName) return { kind: "none" };

  const signature = findWrapperForCall(call, calleeName, wrappers, rootPath);
  if (!signature) return { kind: "none" };

  const read = readWrapperCall(call, signature);
  if (!read) return { kind: "none" };

  const urlFile = toProjectRelative(rootPath, read.node.getSourceFile().getFilePath());
  if (urlFile === null) return { kind: "none" };
  const pos = read.node.getSourceFile().getLineAndColumnAtPos(read.node.getStart());

  return {
    kind: "site",
    site: {
      url: read.url,
      method: read.method,
      ...(read.urlRaw ? { urlRaw: read.urlRaw } : {}),
      evidence: [
        {
          source: "static",
          confidence: read.indirect ? LOCAL_VAR_CONFIDENCE : INDIRECT_CONFIDENCE,
          file: urlFile,
          loc: { line: pos.line, col: pos.column },
        },
      ],
    },
  };
}

/**
 * 调用目标函数的 id；不是对已登记函数的调用时返回 null。
 *
 * 比对声明位置而不是名字：同名函数在不同文件里是不同实体，`fetchUsers`
 * 在 A 文件和 B 文件里可以完全是两回事。也必须穿透 import 别名——调用点的
 * 符号指向的是调用方文件里的 ImportSpecifier。
 */
function resolveCalledFunctionId(
  call: CallExpression,
  callTargets: ReadonlyMap<string, string>,
  rootPath: string,
): string | null {
  const expr = call.getExpression();
  // 方法调用（`obj.fn()`）不在本期范围：静态判定不了 obj 指向哪个模块的函数
  if (!MorphNode.isIdentifier(expr)) return null;

  let symbol = expr.getSymbol();
  if (symbol?.isAlias()) {
    try {
      symbol = symbol.getAliasedSymbol();
    } catch {
      return null;
    }
  }

  for (const decl of symbol?.getDeclarations() ?? []) {
    const relativeFile = toProjectRelative(rootPath, decl.getSourceFile().getFilePath());
    if (relativeFile === null) continue;
    const hit = callTargets.get(positionKey(relativeFile, decl.getStart()));
    if (hit) return hit;
  }
  return null;
}

/** 直接从 HTTP 客户端调用构造请求点。 */
function buildDirectRequestSite(
  call: CallExpression,
  urlArg: Node,
  rootPath: string,
): RequestSite | null {
  const resolved = resolveIdentifier(urlArg);
  const urlNode = resolved?.node ?? urlArg;
  const read = readUrl(urlNode);
  if (!read.text) return null;

  const urlFile = toProjectRelative(rootPath, urlNode.getSourceFile().getFilePath());
  if (urlFile === null) return null;
  const pos = urlNode.getSourceFile().getLineAndColumnAtPos(urlNode.getStart());

  const method = getMethodFromOptions(call) ?? inferMethodFromCallee(call) ?? DEFAULT_METHOD;

  // 一条证据、一个置信度：置信度描述的是「这条结论有多确定」，不是「调用点是不是
  // 真写了」。URL 经变量转手时结论弱一档，位置也指向变量初始化处而非调用处，
  // 这样点开证据看到的就是真正决定 URL 的那一行。
  return {
    url: read.text,
    method,
    evidence: [
      {
        source: "static",
        confidence: resolved ? LOCAL_VAR_CONFIDENCE : DIRECT_CONFIDENCE,
        file: urlFile,
        loc: { line: pos.line, col: pos.column },
      },
    ],
    ...(read.raw !== read.text ? { urlRaw: read.raw } : {}),
  };
}

/** `axios.post(...)` 这类调用从方法名推断 HTTP 方法。 */
function inferMethodFromCallee(call: CallExpression): string | null {
  const expr = call.getExpression();
  if (!MorphNode.isPropertyAccessExpression(expr)) return null;
  const name = expr.getName().toUpperCase();
  return ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"].includes(name) ? name : null;
}

/**
 * 找调用点对应的封装器签名；同名但不同文件的封装器不算。
 *
 * 必须穿透 import 别名：调用点的符号指向的是 `import { request } from "@/services/request"`
 * 里的 ImportSpecifier，它所在文件是**调用方**，而不是定义封装器的文件。
 * 不穿透的话永远只会在调用方文件里找封装器，一个都找不到。
 */
function findWrapperForCall(
  call: CallExpression,
  calleeName: string,
  wrappers: ReadonlyMap<string, WrapperSignature>,
  rootPath: string,
): WrapperSignature | null {
  const expr = call.getExpression();
  let symbol = MorphNode.isPropertyAccessExpression(expr)
    ? expr.getNameNode().getSymbol()
    : expr.getSymbol();

  if (symbol?.isAlias()) {
    try {
      symbol = symbol.getAliasedSymbol();
    } catch {
      // 别名解析失败（依赖未安装）时退回原符号，按未命中处理
    }
  }

  for (const decl of symbol?.getDeclarations() ?? []) {
    const relativeFile = toProjectRelative(rootPath, decl.getSourceFile().getFilePath());
    if (relativeFile === null) continue;

    // 声明是变量（`export const request = ...`）时名字在外层
    const name =
      MorphNode.isFunctionDeclaration(decl) ? decl.getName() : getFunctionName(decl) ?? calleeName;
    if (!name) continue;

    const hit = wrappers.get(`${relativeFile}#${name}`);
    if (hit) return hit;
  }
  return null;
}

/** 列出文件内的所有函数（含变量形式的箭头函数）。 */
function collectFunctions(file: SourceFile): Node[] {
  const fns: Node[] = [...file.getFunctions()];

  for (const decl of file.getDescendantsOfKind(SyntaxKind.VariableDeclaration)) {
    const init = decl.getInitializer();
    if (init && (MorphNode.isArrowFunction(init) || MorphNode.isFunctionExpression(init))) {
      fns.push(init);
    }
  }
  return fns;
}

/** 调用点所在模块级函数的 id；不在模块级函数内返回 null。 */
function moduleFunctionIdOf(
  call: CallExpression,
  rootPath: string,
  file: SourceFile,
): string | null {
  const enclosing = findEnclosingFunction(call);
  if (!enclosing) return null;
  const name = getFunctionName(enclosing);
  if (!name) return null;
  const relativeFile = toProjectRelative(rootPath, file.getFilePath());
  if (relativeFile === null) return null;
  return `${relativeFile}#${name}`;
}
