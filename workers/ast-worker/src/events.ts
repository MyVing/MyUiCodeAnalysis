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
 * 2. **具名引用**（`onClick={handleSelect}`）：解析名字，命中本组件的 hook /
 *    state / 请求链函数就连边；都没命中说明它来自 props（外部注入），到这里
 *    链就断了——这是事实，不是缺口。
 * 3. **其他**（`onClick={handleSelect(id)}` 这类调用结果）：只记源码文本，
 *    不推断指向什么。
 */

import type { JsxOpeningElement, JsxSelfClosingElement, Node, SourceFile } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { Evidence, IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";
import { buildOwnerIndex, findOwner } from "./ownership.js";
import { toProjectRelative } from "./paths.js";

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
  /** 组件 → 元素（uses）、元素 → 处理节点（calls / writes） */
  edges: IREdge[];
  warnings: Warning[];
}

export interface EventsAnalysisInput {
  rootPath: string;
  components: readonly DiscoveredComponent[];
  declarations: ReadonlyMap<string, Node>;
  project: { getSourceFiles(): readonly SourceFile[] };
  /**
   * 已经产出的节点（hook / state / fn）。
   *
   * 传节点而不是重算一遍：事件分析要回答「处理函数落到谁」，而"谁"只能是
   * 图上真实存在的节点。凭空造一个 handler 节点会让同一个函数在图上出现两次。
   */
  knownNodes: readonly IRNode[];
  /** 组件 id → props 字段名；来自 props 分析结果 */
  propsFields: ReadonlyMap<string, readonly string[]>;
}

/** 一个组件内的名字解析表。 */
interface NameTable {
  /** 名字 → 已产出节点。hook 用绑定名，state 用值名与 setter 名，fn 用函数名。 */
  targets: Map<string, { id: string; kind: IRNode["kind"] }>;
  /** setter 名 → state 节点 id，用于把 setter 调用记成 writes 而不是 calls。 */
  setters: Map<string, string>;
  /** 组件的 props 字段名：命中它们说明是外部注入的回调。 */
  propNames: Set<string>;
}

/**
 * 建组件内的名字解析表。
 *
 * 只在**同一文件**内建立：`handleSelect` 这类名字是组件局部作用域的，
 * 跨文件的同名函数是另一个实体。
 */
function buildNameTable(
  component: DiscoveredComponent,
  knownNodes: readonly IRNode[],
  propsFields: ReadonlyMap<string, readonly string[]>,
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
      continue;
    }

    if (node.kind === "fn") {
      targets.set(node.name, { id: node.id, kind: node.kind });
    }
  }

  return { targets, setters, propNames };
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
 */
function collectInlineEffects(
  fn: Node,
  table: NameTable,
): { calls: string[]; writes: string[]; injected: boolean } {
  const calls: string[] = [];
  const writes: string[] = [];
  let injected = false;

  for (const call of fn.getDescendantsOfKind(SyntaxKind.CallExpression)) {
    const expr = call.getExpression();
    const name = MorphNode.isIdentifier(expr)
      ? expr.getText()
      : MorphNode.isPropertyAccessExpression(expr)
        ? expr.getName()
        : null;
    if (!name) continue;

    const stateId = table.setters.get(name);
    if (stateId) {
      if (!writes.includes(stateId)) writes.push(stateId);
      continue;
    }

    const hit = table.targets.get(name);
    // setter 已经在 targets 里登记过，这里不会再把它当 calls
    if (hit && !table.setters.has(name) && !calls.includes(hit.id)) calls.push(hit.id);
    if (!hit && table.propNames.has(name)) injected = true;
  }

  return { calls, writes, injected };
}

/** 描述一个事件绑定，并返回内联形态产生的副作用。 */
function describeEvent(
  name: string,
  value: Node,
  table: NameTable,
): { binding: EventBinding; calls: string[]; writes: string[]; injected: boolean } {
  const text = value.getText();
  const handler =
    text.length > HANDLER_TEXT_LIMIT
      ? `${text.slice(0, HANDLER_TEXT_LIMIT - 3)}...`
      : text;

  if (MorphNode.isArrowFunction(value) || MorphNode.isFunctionExpression(value)) {
    const effects = collectInlineEffects(value, table);
    // writes 一定是 state（只有 setter 会产生 writes）；calls 的种类查表得到，
    // 查不到说明不是本组件可见的节点，那种情况 targets 里就不该有它
    const kindById = new Map<string, IRNode["kind"]>();
    for (const entry of table.targets.values()) kindById.set(entry.id, entry.kind);

    const targets: EventTarget[] = [
      ...effects.writes.map((id) => ({ id, kind: "state" as const })),
      ...effects.calls.flatMap((id) => {
        const kind = kindById.get(id);
        return kind ? [{ id, kind }] : [];
      }),
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
      ...effects,
    };
  }

  if (MorphNode.isIdentifier(value)) {
    const handlerName = value.getText();
    const hit = table.targets.get(handlerName);
    return {
      binding: {
        name,
        handler,
        kind: "reference",
        handlerName,
        targets: hit ? [{ id: hit.id, kind: hit.kind }] : [],
        // 命中 props 字段 = 由父组件注入；这个标记让"没有入边"成为一条结论
        injected: !hit && table.propNames.has(handlerName),
      },
      calls: [],
      writes: [],
      injected: false,
    };
  }

  return {
    binding: { name, handler, kind: "other", handlerName: null, targets: [], injected: false },
    calls: [],
    writes: [],
    injected: false,
  };
}

/**
 * 元素节点 id 的后缀。
 *
 * 不用行列号（插一行注释就漂移）。优先用唯一的具名处理函数作后缀：
 * `jsx:div/handleSelect` 比 `jsx:div#2` 可读得多。有多个具名处理函数或没有时，
 * 用「标签 + 组件内序号」。
 */
function elementSuffix(
  tag: string,
  handlerNames: readonly string[],
  counters: Map<string, number>,
  componentId: string,
): string {
  const named = handlerNames.filter((name) => name.length > 0);
  const single = named.length === 1 ? named[0] : undefined;
  if (single) return `jsx:${tag}/${single}`;

  const key = `${componentId} jsx:${tag}`;
  const next = (counters.get(key) ?? 0) + 1;
  counters.set(key, next);
  return next === 1 ? `jsx:${tag}` : `jsx:${tag}#${next}`;
}

/** 宿主元素判定：与 renders 分析器同一套 JSX 编译语义。 */
function isHostTag(tag: string): boolean {
  if (tag === "Fragment" || tag === "React.Fragment") return true;
  const last = tag.split(".").pop() ?? tag;
  return !/^[A-Z]/.test(last);
}

export function analyzeEvents(input: EventsAnalysisInput): EventsAnalysisResult {
  const { rootPath, components, declarations, project, knownNodes, propsFields } = input;

  const warnings: Warning[] = [];
  const edges = new Map<string, IREdge>();
  const elementNodes = new Map<string, IRNode>();
  const counters = new Map<string, number>();

  const ownerIndex = buildOwnerIndex(components, declarations);

  // 一次扫描全部文件，而不是「每个组件扫一遍项目」——后者是 O(组件数 × 文件数)。
  const tables = new Map<string, NameTable>();
  for (const component of components) {
    tables.set(component.id, buildNameTable(component, knownNodes, propsFields));
  }

  for (const file of project.getSourceFiles()) {
    const relativeFile = toProjectRelative(rootPath, file.getFilePath());
    if (relativeFile === null) continue;

    const openings: (JsxOpeningElement | JsxSelfClosingElement)[] = [
      ...file.getDescendantsOfKind(SyntaxKind.JsxOpeningElement),
      ...file.getDescendantsOfKind(SyntaxKind.JsxSelfClosingElement),
    ];

    for (const opening of openings) {
      const bindings = readEventBindings(opening);
      if (bindings.length === 0) continue;

      // 归属：元素在谁的函数体里就归谁；模块顶层的 JSX 不归任何组件
      const ownerId = findOwner(opening, ownerIndex, rootPath);
      if (!ownerId) continue;

      const table = tables.get(ownerId);
      if (!table) continue;

      const tag = opening.getTagNameNode().getText();
      const events: EventBinding[] = [];
      for (const { name, value } of bindings) {
        events.push(describeEvent(name, value, table).binding);
      }

      const position = opening
        .getSourceFile()
        .getLineAndColumnAtPos(opening.getTagNameNode().getStart());
      const loc = { line: position.line, col: position.column };
      const suffix = elementSuffix(
        tag,
        events.map((event) => event.handlerName ?? ""),
        counters,
        ownerId,
      );
      const nodeId = `${ownerId}/${suffix}`;

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
            target: isHostTag(tag) ? "host" : "component",
            events,
          },
          evidence: [evidence],
        });
      }

      addEdge(edges, {
        id: `e_uses_${ownerId}__${nodeId}`,
        from: ownerId,
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

  return { elementNodes: [...elementNodes.values()], edges: [...edges.values()], warnings };
}

function addEdge(edges: Map<string, IREdge>, edge: IREdge): void {
  if (!edges.has(edge.id)) edges.set(edge.id, edge);
}
