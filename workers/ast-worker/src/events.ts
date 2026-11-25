/**
 * 事件分析：把「组件上挂了什么事件、事件落到谁」还原成 element 节点与边。
 *
 * ## 事件在 IR 里用什么表达
 *
 * `NodeKind` 是受控枚举，没有 `event` 这一项；但已有 `element`（"JSX 元素"）且
 * 至今未被任何分析器使用。事件不是独立实体——它总是挂在某个 JSX 元素上，
 * 所以一个「带事件处理的元素」正好就是一个 element 节点，事件清单进 attrs。
 * 这样既不新增枚举，也不会让图上出现一堆脱离宿主元素的浮空事件点。
 *
 * ## 只认 `on[A-Z]`，但那不是业务名硬编码
 *
 * React 的事件属性约定是 `on` + 大写字母（`onClick` / `onChange`，自定义组件的
 * `onRowClick` 同理）。这是框架语言级约定，和「禁止按 ProTable 这类业务名判断」
 * 是两回事：前者对所有项目一致，后者只对一个项目成立。
 *
 * 它同时覆盖两类完全不同的东西，两者都要收：
 *  - **宿主元素事件**：`<div onClick>`，浏览器事件
 *  - **组件回调 prop**：`<RowItem onClick={...}>`，父组件往子组件注入回调，
 *    子组件内部再决定何时调用
 *
 * ## 「落到谁」三层判定
 *
 * 1. **内联函数**（`onClick={() => setOpen(true)}`）：逻辑就在眼前，收集体内
 *    对已知节点的调用（setter → writes，函数/hook → calls）。但如果体内调用的
 *    名字是 props 字段（`onClick={() => onClick(userId)}`），真正的逻辑仍在
 *    外部——内联只是转一手，同样标 `injected`。
 * 2. **具名引用**（`onClick={handleSelect}`）：先在本组件的名字表里找 hook /
 *    state / 同文件函数，命中就连边；未命中再走**符号解析**穿透 import 找到
 *    跨文件函数。两者都没有、而名字又在 props 字段里，才判定为外部注入——
 *    到这里链就断了，这是事实，不是缺口。
 * 3. **其他**（`onClick={handleSelect(id)}` 这类调用结果）：只记源码文本，
 *    不推断指向什么。
 *
 * ## 为什么必须有符号解析这一路
 *
 * 只按「同文件名字表」解析时，`<Button onClick={() => saveOrder(form)}>` 里的
 * `saveOrder` 从另一个文件 import 进来，永远命中不了。代价不只是少一条边：
 * `api.ts` 的 `findOwner` 会把该请求记到组件名下，图上呈现成「页面加载时请求」，
 * 而事实是「点击才请求」——这是语义错误。
 *
 * 跨文件命中的函数如果还不在图上，由本模块补一个 `fn` 节点（`fnNodes` 返回值）：
 * 边必须连到真实存在的节点，凭空连一条边会让前端点不开目标。
 *
 * ## 元素 id 不由本模块计算
 *
 * 视图层（`view.ts`）描述的 `<button>` 与本模块描述的是同一个元素。id 统一由
 * `jsx-inventory.ts` 分配，两个分析器查同一份清单——各算各的必然漂移，视图树
 * 与事件链就会断开。本模块只消费清单，不修改 id 规则。
 */

import type {
  Identifier,
  JsxOpeningElement,
  JsxSelfClosingElement,
  Node,
} from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { Evidence, IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";
import type { JsxInventory } from "./jsx-inventory.js";
import {
  functionIdentity,
  functionNodeOf,
  resolveIdentifierDeclarations,
  type FunctionIdentity,
} from "./symbols.js";

/** 元素结构是语法事实：标签与事件属性都直接写在源码里。 */
const STRUCTURE_CONFIDENCE = 1;
/** 处理函数解析到具体节点：靠符号/名字追到已产出节点，依赖分析完整度。 */
const HANDLER_CONFIDENCE = 0.9;

/** React 事件属性约定：on + 大写字母。 */
const EVENT_ATTR_PATTERN = /^on[A-Z]/;

/** attrs 里的处理函数文本上限：它是给人看的，过长反而看不出重点。 */
const HANDLER_TEXT_LIMIT = 80;

/** 事件最终落到的节点。 */
export interface EventTarget {
  id: string;
  kind: IRNode["kind"];
}

/** 一个事件绑定。 */
export interface EventBinding {
  /** 属性名，如 onClick / onRowClick */
  name: string;
  /** 处理函数的源码文本（超长截断） */
  handler: string;
  /** 处理函数的形态 */
  kind: "inline-function" | "reference" | "other";
  /** 引用形态时的名字（`handleSelect`）；内联与 other 为 null */
  handlerName: string | null;
  /**
   * 这条事件最终落到哪些已产出的节点。
   *
   * 引用形态最多一项；内联形态是函数体内调用/写入到的全部节点。
   * 为空有明确含义：处理逻辑来自 props（由父组件注入）或无法静态判定——
   * 前端据此显示"回调由外部注入"，而不是显示成"链路缺失"。
   */
  targets: EventTarget[];
  /**
   * true 表示处理函数来自 props（`<div onClick={onSelect}>` 里 onSelect 是
   * 父组件注入的）。这不是"没解析出来"，而是静态分析的真实边界：逻辑在父组件，
   * 子组件只负责在何时调用。标出来读者才不会以为链路断了。
   */
  injected: boolean;
}

export interface EventsAnalysisResult {
  /** 带事件的 JSX 元素节点 */
  elementNodes: IRNode[];
  /**
   * 事件引用到、但图上原本不存在的跨文件函数节点。
   *
   * 只收录「被事件引用」的：给所有被 import 的业务函数建节点会让大项目多出
   * 大量孤立节点，而没有被任何事件引用的函数也没有边可挂。调用方负责把它们
   * 并进图（与 hook / state / fn 同表）。
   */
  fnNodes: IRNode[];
  /** 组件 → 元素（uses）、元素 → 处理节点（calls / writes） */
  edges: IREdge[];
  warnings: Warning[];
}

export interface EventsAnalysisInput {
  /** 统一元素清单：id、归属、file/loc 都取自这里，不再自行计算 */
  inventory: JsxInventory;
  components: readonly DiscoveredComponent[];
  /** 已经产出的 hook / state / fn 节点。 */
  knownNodes: readonly IRNode[];
  /** 组件级 mutation result 变量 → 唯一静态 mutationFn 函数节点。 */
  mutationTargets?: ReadonlyMap<string, string>;
  /** 组件 id → props 字段名；来自 props 分析结果 */
  propsFields: ReadonlyMap<string, readonly string[]>;
  /** 组件 id → 定义节点；用于解析 JSX 中的子组件标签 */
  declarations?: ReadonlyMap<string, Node>;
  /** 项目根目录，符号解析要把绝对路径收敛成 IR 用的相对路径 */
  rootPath: string;
}

/** 一个组件内的名字解析表。 */
interface NameTable {
  /** 名字 → 已产出节点。hook 用绑定名，state 用值名与 setter 名，fn 用函数名。 */
  targets: Map<string, { id: string; kind: IRNode["kind"] }>;
  /** setter 名 → state 节点 id，用于把 setter 调用记成 writes 而不是 calls。 */
  setters: Map<string, string>;
  /** 组件的 props 字段名：命中它们说明是外部注入的回调。 */
  propNames: Set<string>;
  mutationTargets: Map<string, string>;
}

/** 局部名字命中时的落点。 */
interface NameHit {
  id: string;
  kind: IRNode["kind"];
  /** 命中的是 state 的 setter（`setOpen`）——这种调用算 writes 而不是 calls */
  isSetter: boolean;
}

type PropTargets = ReadonlyMap<string, readonly NameHit[]>;

/**
 * 事件处理的落点解析器。
 *
 * 名字表负责「局部名字」（组件内的 hook 绑定名、state 与 setter、同文件函数），
 * 符号解析负责「跨文件引用」。两条路分开的理由：
 *  - 局部名字不能用符号解析替代：`const [open, setOpen] = useState()` 的 setter
 *    符号会指向解构处的 `VariableDeclaration`，反查不到 state 节点；
 *  - 跨文件引用不能靠名字：同名函数在不同文件是不同实体。
 */
interface HandlerResolver {
  /** 局部名字表 → 命中返回落点；未命中返回 null */
  byName(name: string): NameHit | null;
  /** 标识符 → 跨文件函数身份；局部名字或解析不到都返回 null */
  bySymbol(identifier: Identifier): FunctionIdentity | null;
  /** 父组件传入的 props 回调 → 父组件真实目标 */
  byPropTarget(name: string): NameHit | null;
  isPropCallback(name: string): boolean;
  mutationTarget(name: string): string | null;
}

/**
 * 建组件内的名字解析表。
 *
 * 只在**同一文件**内建立：`handleSelect` 这类名字是组件局部作用域的，
 * 跨文件的同名函数是另一个实体（那一类走符号解析）。
 */
function buildNameTable(
  component: DiscoveredComponent,
  knownNodes: readonly IRNode[],
  propsFields: ReadonlyMap<string, readonly string[]>,
  mutationTargets: ReadonlyMap<string, string> = new Map(),
): NameTable {
  const targets = new Map<string, { id: string; kind: IRNode["kind"] }>();
  const setters = new Map<string, string>();
  const propNames = new Set(propsFields.get(component.id) ?? []);

  for (const node of knownNodes) {
    if (node.file !== undefined && node.file !== component.file) continue;

    if (node.kind === "state") {
      targets.set(node.name, { id: node.id, kind: node.kind });
      const setter = node.attrs.setter;
      if (typeof setter === "string" && setter.length > 0) {
        setters.set(setter, node.id);
        targets.set(setter, { id: node.id, kind: node.kind });
      }
      continue;
    }

    if (node.kind === "hook") {
      // useCallback 绑定的变量名就是处理函数名（`const handleSelect = useCallback(...)`）
      const binding = node.attrs.binding;
      const name = typeof binding === "string" && binding.length > 0 ? binding : node.name;
      targets.set(name, { id: node.id, kind: node.kind });
      // 解构 hook 返回值的绑定名（`const { onSubmit } = useLogin()`）同样落到
      // 该 hook 节点。静态近似：不区分 hook 返回对象的具体键——绑定到的是
      // hook 而不是它内部的具体函数，多个事件可能落到同一 hook 节点上。
      const destructured = node.attrs.destructured;
      if (Array.isArray(destructured)) {
        for (const item of destructured) {
          if (typeof item === "string" && item.length > 0) {
            targets.set(item, { id: node.id, kind: node.kind });
          }
        }
      }
      continue;
    }

    if (node.kind === "fn") {
      targets.set(node.name, { id: node.id, kind: node.kind });
    }
  }

  const scopedMutationTargets = new Map<string, string>();
  const prefix = `${component.id}:`;
  for (const [key, value] of mutationTargets) {
    if (key.startsWith(prefix)) scopedMutationTargets.set(key.slice(prefix.length), value);
  }
  return { targets, setters, propNames, mutationTargets: scopedMutationTargets };
}

/**
 * 组装落点解析器：名字表优先（局部作用域），符号解析兜底（跨文件）。
 *
 * 「名字表优先」不是性能考虑而是语义要求：组件内的 `handleSelect` 遮蔽同名的
 * 跨文件导出函数，按 JS 作用域规则应该连到局部那个。反过来先查符号会把边连到
 * 一个用户根本没有调用到的函数上。
 */
function buildResolver(
  table: NameTable,
  rootPath: string,
  registerFn: (identity: FunctionIdentity) => void,
  propTargets: PropTargets = new Map(),
  componentId = "",
): HandlerResolver {
  return {
    byName(name) {
      const hit = table.targets.get(name);
      if (!hit) return null;
      return { ...hit, isSetter: table.setters.has(name) };
    },
    bySymbol(identifier) {
      for (const decl of resolveIdentifierDeclarations(identifier)) {
        const fn = functionNodeOf(decl);
        if (!fn) continue;

        const identity = functionIdentity(fn, rootPath);
        if (!identity) continue;

        // 组件本身不是「处理函数」：`onClick={UserList}` 这类写法没有可执行语义
        registerFn(identity);
        return identity;
      }
      return null;
    },
  byPropTarget(name) {
      const hits = propTargets.get(`${componentId}:${name}`) ?? [];
      return hits.length === 1 ? (hits[0] ?? null) : null;
    },
    isPropCallback(name) {
      return table.propNames.has(name);
    },
    mutationTarget(name) {
      return table.mutationTargets.get(name) ?? null;
    },
  };
}

/** 收集元素上的事件属性（属性名节点 + 值表达式）。 */
function readEventBindings(
  opening: JsxOpeningElement | JsxSelfClosingElement,
): { name: string; value: Node }[] {
  const result: { name: string; value: Node }[] = [];
  for (const attr of opening.getAttributes()) {
    if (!MorphNode.isJsxAttribute(attr)) continue;

    const name = attr.getNameNode().getText();
    if (!EVENT_ATTR_PATTERN.test(name)) continue;

    const init = attr.getInitializer();
    // 单独的 `onClick`（无 `={}`）在 JSX 里等价于 true，不是事件绑定
    if (!init || !MorphNode.isJsxExpression(init)) continue;

    const value = init.getExpression();
    if (!value) continue;

    result.push({ name, value });
  }
  return result;
}

/**
 * 收集内联处理函数体里对已知节点的使用。
 *
 * 与 hooks 归属的"遇函数即停"是相反的：事件处理在**运行期触发**，`arr.map(() => setX())`
 * 里的 setX 仍会在点击时执行，所以这里要下探全部嵌套层级。React 的顶层调用
 * 规则只约束 hook，不约束事件回调。
 *
 * 解析顺序：局部名字表 → 符号解析 → props 回调。跨文件函数经符号解析命中后
 * 同样要等它成为图上节点才连边，因此 identity 由调用方登记。
 */
function collectInlineEffects(
  fn: Node,
  resolver: HandlerResolver,
): {
  calls: { id: string; kind: IRNode["kind"] }[];
  writes: string[];
  injected: boolean;
} {
  const calls: { id: string; kind: IRNode["kind"] }[] = [];
  const writes: string[] = [];
  let injected = false;

  for (const call of fn.getDescendantsOfKind(SyntaxKind.CallExpression)) {
    const expr = call.getExpression();
    if (MorphNode.isPropertyAccessExpression(expr) && ["mutate", "mutateAsync"].includes(expr.getName())) {
      const receiver = expr.getExpression();
      if (MorphNode.isIdentifier(receiver)) {
        const targetId = resolver.mutationTarget(receiver.getText());
        if (targetId && !calls.some((entry) => entry.id === targetId)) {
          calls.push({ id: targetId, kind: "fn" });
        }
      }
      continue;
    }
    const name = MorphNode.isIdentifier(expr)
      ? expr.getText()
      : MorphNode.isPropertyAccessExpression(expr)
        ? expr.getName()
        : null;
    if (!name) continue;

    const local = resolver.byName(name);
    if (local) {
      // setter 调用记 writes，其余（hook / 同文件函数）记 calls
      if (local.isSetter) {
        if (!writes.includes(local.id)) writes.push(local.id);
      } else if (!calls.some((entry) => entry.id === local.id)) {
        calls.push({ id: local.id, kind: local.kind });
      }
      continue;
    }

    // 局部名字没有 → 可能是 import 进来的跨文件函数
    if (MorphNode.isIdentifier(expr)) {
      const identity = resolver.bySymbol(expr);
      if (identity) {
        if (!calls.some((entry) => entry.id === identity.id)) {
          calls.push({ id: identity.id, kind: "fn" });
        }
        continue;
      }
    }

    // 两条路都没命中 → 只有名字在 props 里才是「父组件注入」
    if (resolver.isPropCallback(name)) injected = true;
  }

  return { calls, writes, injected };
}

/** 描述一个事件绑定，并返回内联形态产生的副作用。 */
function describeEvent(
  name: string,
  value: Node,
  resolver: HandlerResolver,
): { binding: EventBinding; targets: EventTarget[] } {
  const text = value.getText();
  const handler =
    text.length > HANDLER_TEXT_LIMIT
      ? `${text.slice(0, HANDLER_TEXT_LIMIT - 3)}...`
      : text;

  if (MorphNode.isArrowFunction(value) || MorphNode.isFunctionExpression(value)) {
    const effects = collectInlineEffects(value, resolver);
    // writes 一定是 state（只有 setter 会产生 writes）；calls 自带 kind
    const targets: EventTarget[] = [
      ...effects.writes.map((id) => ({ id, kind: "state" as const })),
      ...effects.calls,
    ];
    return {
      binding: {
        name,
        handler,
        kind: "inline-function",
        handlerName: null,
        targets,
        // 内联体里调用的是 props 回调 → 逻辑仍在父组件，这条边本来就该断
        injected: effects.injected,
      },
      targets,
    };
  }

  if (MorphNode.isIdentifier(value)) {
    const handlerName = value.getText();

    // 局部作用域优先：组件内的 hook / 同文件函数遮蔽跨文件同名导出
    const local = resolver.byName(handlerName);
    if (local) {
      const targets = [
        { id: local.id, kind: local.kind },
      ];
      return {
        binding: {
          name,
          handler,
          kind: "reference",
          handlerName,
          targets,
          injected: false,
        },
        targets,
      };
    }

    // 跨文件函数：穿透 import 别名找到真实声明
    const identity = resolver.bySymbol(value);
    if (identity) {
      const targets = [{ id: identity.id, kind: "fn" as const }];
      return {
        binding: {
          name,
          handler,
          kind: "reference",
          handlerName,
          targets,
          injected: false,
        },
        targets,
      };
    }

    // 两条路都没有时，若这是子组件 props，则尝试回溯父组件传入的表达式。
    // 即使成功，injected 仍保留为 true：逻辑来自父组件，只是现在能确认其落点。
    const propTarget = resolver.byPropTarget(handlerName);
    if (propTarget) {
      const targets = [{ id: propTarget.id, kind: propTarget.kind }];
      return {
        binding: {
          name,
          handler,
          kind: "reference",
          handlerName,
          targets,
          injected: true,
        },
        targets,
      };
    }

    // 两条路都没有，而名字在 props 字段里 = 由父组件注入。
    // 这个标记让"没有入边"成为一条结论，而不是看起来像链路缺失。
    return {
      binding: {
        name,
        handler,
        kind: "reference",
        handlerName,
        targets: [],
        injected: resolver.isPropCallback(handlerName),
      },
      targets: [],
    };
  }

  return {
    binding: { name, handler, kind: "other", handlerName: null, targets: [], injected: false },
    targets: [],
  };
}

export function analyzeEvents(input: EventsAnalysisInput): EventsAnalysisResult {
  const { inventory, components, knownNodes, mutationTargets = new Map(), propsFields, declarations, rootPath } = input;

  const warnings: Warning[] = [];
  const edges = new Map<string, IREdge>();
  const elementNodes = new Map<string, IRNode>();

  // 已有的 fn 节点 id：符号解析命中这些时不必再补节点
  const knownFnIds = new Set(
    knownNodes.filter((node) => node.kind === "fn").map((node) => node.id),
  );
  // 被事件引用、但图上原本不存在的跨文件函数
  const newFnNodes = new Map<string, IRNode>();

  const registerFn = (identity: FunctionIdentity): void => {
    if (knownFnIds.has(identity.id) || newFnNodes.has(identity.id)) return;

    newFnNodes.set(identity.id, {
      id: identity.id,
      kind: "fn",
      name: identity.name,
      file: identity.file,
      loc: identity.loc,
      // role 说明它为什么进图：不是在请求链路上，而是被事件直接引用
      attrs: { role: "事件处理函数", endLine: identity.endLine },
      evidence: [
        {
          source: "static",
          confidence: STRUCTURE_CONFIDENCE,
          file: identity.file,
          loc: identity.loc,
        },
      ],
    });
  };

  // 一次扫描全部文件，而不是「每个组件扫一遍项目」——后者是 O(组件数 × 文件数)。
  const propTargets = new Map<string, NameHit[]>();
  const resolvers = new Map<string, HandlerResolver>();
  for (const component of components) {
    resolvers.set(
      component.id,
      buildResolver(
        buildNameTable(component, knownNodes, propsFields, mutationTargets),
        rootPath,
        registerFn,
        propTargets,
        component.id,
      ),
    );
  }

  // 子组件的事件 prop 需要回到父组件 JSX 实例解析：
  // `<RegisterView onSubmit={onSubmit} />` → RegisterView 的 `onSubmit`
  // → Register 父组件里的 hook 绑定。只收唯一、可静态解析的目标。
  const componentByDeclaration = new Map<number, string>();
  const componentByName = new Map<string, string>();
  for (const component of components) {
    const declaration = declarations?.get(component.id);
    if (declaration) componentByDeclaration.set(declaration.getStart(), component.id);
    componentByName.set(component.name, component.id);
  }
  for (const fileEntries of inventory.byFile.values()) {
    for (const info of fileEntries) {
      const opening = info.opening;
      if (!MorphNode.isJsxOpeningElement(opening) && !MorphNode.isJsxSelfClosingElement(opening)) {
        continue;
      }
      const tagNode = opening.getTagNameNode();
      const childDeclarations = tagNode
        ? resolveIdentifierDeclarations(tagNode)
        : [];
      const childId = childDeclarations
        .map((decl) => componentByDeclaration.get(decl.getStart()))
        .find((id): id is string => id !== undefined)
        ?? componentByName.get(tagNode?.getText() ?? "");
      if (!childId || childId === info.componentId) continue;

      const parentResolver = resolvers.get(info.componentId);
      if (!parentResolver) continue;
      for (const attr of opening.getAttributes()) {
        if (!MorphNode.isJsxAttribute(attr)) continue;
        const propName = attr.getNameNode().getText();
        if (!EVENT_ATTR_PATTERN.test(propName)) continue;
        const initializer = attr.getInitializer();
        if (!initializer || !MorphNode.isJsxExpression(initializer)) continue;
        const expression = initializer.getExpression();
        if (!expression) continue;

        const hits: NameHit[] = [];
        if (MorphNode.isIdentifier(expression)) {
          // 只有父子组件使用同名事件 prop 时才回溯：`onSubmit={onSubmit}`。
          // 名称不同（如 `onClick={onSelect}`）可能只是 props 转发，
          // 在没有完整别名数据流时保持 injected，避免误连。
          if (propName !== expression.getText()) continue;
          const local = parentResolver.byName(expression.getText());
          if (local) hits.push(local);
          else {
            const identity = parentResolver.bySymbol(expression);
            if (identity) hits.push({ id: identity.id, kind: "fn", isSetter: false });
          }
        } else if (MorphNode.isArrowFunction(expression) || MorphNode.isFunctionExpression(expression)) {
          const effects = collectInlineEffects(expression, parentResolver);
          for (const write of effects.writes) hits.push({ id: write, kind: "state", isSetter: true });
          for (const call of effects.calls) hits.push({ id: call.id, kind: call.kind, isSetter: false });
        }
        const unique = [...new Map(hits.map((hit) => [hit.id, hit])).values()];
        const only = unique[0];
        if (only && unique.length === 1) {
          const key = `${childId}:${propName}`;
          const existing = propTargets.get(key) ?? [];
          if (!existing.some((hit) => hit.id === only.id)) existing.push(only);
          propTargets.set(key, existing);
        }
      }
    }
  }

  for (const fileEntries of inventory.byFile.values()) {
    for (const info of fileEntries) {
      const opening = info.opening;
      if (!MorphNode.isJsxOpeningElement(opening) && !MorphNode.isJsxSelfClosingElement(opening)) {
        continue;
      }

      const bindings = readEventBindings(opening);
      if (bindings.length === 0) continue;

      const resolver = resolvers.get(info.componentId);
      if (!resolver) continue;

      const events: EventBinding[] = [];
      for (const { name, value } of bindings) {
        events.push(describeEvent(name, value, resolver).binding);
      }

      const { tag, file: relativeFile, loc } = info;
      // id 来自清单：视图层与事件层描述的必须是同一个节点
      const nodeId = info.id;

      const evidence: Evidence = {
        source: "static",
        confidence: STRUCTURE_CONFIDENCE,
        file: relativeFile,
        loc,
      };

      // 同名元素在同一组件里出现多次（id 撞车）时合并事件清单并记告警，
      // 而不是让后一个悄悄覆盖前一个
      const existing = elementNodes.get(nodeId);
      if (existing) {
        const merged = [...(existing.attrs.events as EventBinding[]), ...events];
        existing.attrs = { ...existing.attrs, events: merged };
        warnings.push({
          code: "duplicate_element_id",
          severity: "warning",
          stage: "static",
          file: relativeFile,
          loc,
          message: `元素节点 id 重复：${nodeId}；已合并事件清单，请检查 id 生成规则`,
        });
      } else {
        elementNodes.set(nodeId, {
          id: nodeId,
          kind: "element",
          name: `<${tag}>`,
          file: relativeFile,
          loc,
          attrs: {
            tag,
            // host 是有明确语义的：浏览器元素；其余按 JSX 语义是组件
            target: info.isHost ? "host" : "component",
            events,
          },
          evidence: [evidence],
        });
      }

      addEdge(edges, {
        id: `e_uses_${info.componentId}__${nodeId}`,
        from: info.componentId,
        to: nodeId,
        kind: "uses",
        evidence: [evidence],
      });

      const handlerEvidence: Evidence = {
        source: "static",
        confidence: HANDLER_CONFIDENCE,
        file: relativeFile,
        loc,
      };

      // 事件落到的节点：引用形态与内联形态统一按 targets 连边
      for (const event of events) {
        for (const target of event.targets) {
          const kind: IREdge["kind"] = target.kind === "state" ? "writes" : "calls";
          addEdge(edges, {
            id: `e_${kind}_${nodeId}__${target.id}`,
            from: nodeId,
            to: target.id,
            kind,
            evidence: [handlerEvidence],
          });
        }
      }
    }
  }

  return {
    elementNodes: [...elementNodes.values()],
    fnNodes: [...newFnNodes.values()],
    edges: [...edges.values()],
    warnings,
  };
}

function addEdge(edges: Map<string, IREdge>, edge: IREdge): void {
  if (!edges.has(edge.id)) edges.set(edge.id, edge);
}
