/**
 * IR 组装：把分析结果收敛成符合 shared/schemas/ir.schema.json 的图。
 *
 * 职责边界：本模块只做「领域结果 → IR 节点」的映射，不做任何 AST 判断。
 * 组件是什么、props 叫什么，由 components.ts 决定；这里只负责 id、证据与
 * 置信度这些契约层的事。
 *
 * 置信度约定（文档 2.2）：
 *  - 静态语法事实（组件确实定义在此文件此位置）→ 1.0
 *  - 静态推断（如别名解析、props 类型绑定）→ 0.6–0.9
 * 这里产出的组件节点全部是语法事实，因此恒为 1.0。推断类证据留给后续
 * renders / calls 边。
 */

import type { Evidence, IREdge, IRGraph, IRNode, Warning } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";
import type { ComponentProps } from "./props.js";
import type { WorkerRequest } from "./protocol.js";
import type { ComponentText } from "./ui-text.js";

/** 当前 Worker 产出的 IR schema 版本，必须与 shared/schemas 的演进同步。 */
export const SCHEMA_VERSION = "1.1";

/** 组件节点是语法事实，置信度取满。 */
const STATIC_FACT_CONFIDENCE = 1;

function componentToNode(
  component: DiscoveredComponent,
  props?: ComponentProps,
  wrapper?: Record<string, unknown>,
  text?: ComponentText,
): IRNode {
  const evidence: Evidence = {
    source: "static",
    confidence: STATIC_FACT_CONFIDENCE,
    file: component.file,
    loc: component.loc,
  };

  return {
    id: component.id,
    kind: "component",
    name: component.name,
    file: component.file,
    loc: component.loc,
    // attrs 只放插件私有扩展；file / loc 已是一等字段，不再重复
    attrs: {
      form: component.form,
      // 只有起始行时无法按函数体取代码窗口；endLine 是分析产物元数据，
      // 不改变 Location 契约，随 attrs 扩展下发
      endLine: component.endLine,
      ...(component.propsTypeName ? { propsTypeName: component.propsTypeName } : {}),
      exportNames: component.exportNames,
      // props 字段完整展开，前端属性面板直接消费，不必二次请求
      ...(props ? { props: serializeProps(props) } : {}),
      // 二次封装注解：来自 mapping.json，不是分析器内联判断
      ...(wrapper ? { wrapper } : {}),
      // 可见文案：语法事实，需求检索的召回入口
      ...(text ? { uiText: serializeUiText(text) } : {}),
    },
    evidence: [evidence],
  };
}

/**
 * props 序列化进 attrs。
 *
 * 字段名保持 camelCase（IR 契约的既有风格），类型文本原样保留：
 * 前端要显示的是用户写的 `UserRowProps[]`，而不是展开后的匿名结构。
 */
function serializeProps(props: ComponentProps): Record<string, unknown> {
  return {
    typeName: props.typeName,
    typeFile: props.typeFile,
    generic: props.generic,
    fields: props.fields.map((field) => ({
      name: field.name,
      type: field.type,
      optional: field.optional,
      defaultValue: field.defaultValue,
      description: field.description,
      file: field.file,
      loc: field.loc,
    })),
  };
}

/**
 * 文案序列化进 attrs。
 *
 * 形状刻意扁平（`{ text, kind, via, loc }` 的数组），不按 kind 分组：
 * 检索端拿到的是一份线性清单，分组只影响展示；而前端需要的是「按出现顺序读」。
 *
 * `dynamic` 单列：那是"文案来自变量"的位置记录，不是文案本身。混进 texts
 * 会让检索把 `{title}` 这种表达式当真命中。
 */
function serializeUiText(text: ComponentText): Record<string, unknown> {
  return {
    texts: text.texts.map((entry) => ({
      text: entry.text,
      kind: entry.kind,
      via: entry.via,
      loc: entry.loc,
    })),
    ...(text.dynamic.length > 0 ? { dynamic: text.dynamic } : {}),
    ...(text.truncated > 0 ? { truncated: text.truncated } : {}),
  };
}

export interface BuildGraphInput {
  request: WorkerRequest;
  components: readonly DiscoveredComponent[];
  /** 组件 id → props 展开结果；缺省表示尚未做 props 分析 */
  props?: ReadonlyMap<string, ComponentProps>;
  /** 组件 id → 二次封装注解（attrs.wrapper）；来自 mapping.json */
  wrappers?: ReadonlyMap<string, Record<string, unknown>>;
  /** 组件 id → 可见文案（attrs.uiText）；来自 ui-text 分析 */
  texts?: ReadonlyMap<string, ComponentText>;
  edges?: readonly IREdge[];
  /** 外部组件薄节点（如 antd#Table），与项目内组件节点同表产出 */
  externalNodes?: readonly IRNode[];
  /**
   * 由分析器直接构造的项目内节点（hook / state / element / route）。
   *
   * 与 components 分开传而不是让调用方拼一个大数组：组件节点要走 props 组装，
   * 这些节点已经是最终形态，混在一起会让人以为它们也需要 props。
   *
   * **同 id 节点会合并 attrs**：视图层与事件层描述的是同一个 `<button>`，
   * 各自产出节点再合并，而不是让后一个覆盖前一个。
   */
  extraNodes?: readonly IRNode[];
  warnings?: readonly Warning[];
}

/**
 * 合并同 id 节点。
 *
 * 两个分析器命中同一个元素是正常情况（视图层管"页面上有什么"，事件层管
 * "点了会发生什么"）。id 相同就必须合并成一个节点——图上不能有两个 `<button>`。
 *
 * 合并规则：attrs 浅合并，后者覆盖前者的同名键（事件层的 events 不会被视图层
 * 的 tag / viewRole 覆盖，因为键不重叠）；evidence 追加去重；file / loc 取
 * 先出现的（同一元素在同一位置，不该有分歧）。
 *
 * 合并后仍要保证契约的 `evidence` 非空（minItems: 1）——两边都至少有一条。
 */
function mergeNodes(nodes: readonly IRNode[]): IRNode[] {
  const merged = new Map<string, IRNode>();
  for (const node of nodes) {
    const existing = merged.get(node.id);
    if (!existing) {
      merged.set(node.id, { ...node, attrs: { ...node.attrs }, evidence: [...node.evidence] });
      continue;
    }

    const seen = new Set(existing.evidence.map((item) => evidenceKey(item)));
    for (const item of node.evidence) {
      if (!seen.has(evidenceKey(item))) {
        existing.evidence.push(item);
        seen.add(evidenceKey(item));
      }
    }
    existing.attrs = { ...existing.attrs, ...node.attrs };
  }
  return [...merged.values()];
}

/** 证据去重键：来源 + 文件 + 位置 + 置信度。 */
function evidenceKey(item: Evidence): string {
  return `${item.source}|${item.file ?? ""}|${item.loc?.line ?? 0}:${item.loc?.col ?? 0}|${item.confidence}`;
}

/** 节点排序键：有 file 的按 file + 行 + 列 + id，无 file 的排最后。 */
function compareNodes(a: IRNode, b: IRNode): number {
  if (a.file !== b.file) {
    if (a.file === undefined) return 1;
    if (b.file === undefined) return -1;
    return a.file < b.file ? -1 : 1;
  }
  const lineA = a.loc?.line ?? 0;
  const lineB = b.loc?.line ?? 0;
  if (lineA !== lineB) return lineA - lineB;
  const colA = a.loc?.col ?? 0;
  const colB = b.loc?.col ?? 0;
  if (colA !== colB) return colA - colB;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

/**
 * 组装完整 IR 图。
 *
 * 节点按 file + loc 排序，保证同一份源码的多次分析产物稳定：否则前端与测试
 * 会因顺序抖动产生无意义的 diff，而节点顺序本身不携带任何语义。
 * 外部组件节点没有 file，统一排在最后，避免 undefined 参与比较。
 */
export function buildGraph({
  request,
  components,
  props,
  wrappers,
  texts,
  edges = [],
  externalNodes = [],
  extraNodes = [],
  warnings = [],
}: BuildGraphInput): IRGraph {
  const internalNodes = [
    ...components.map((component) =>
      componentToNode(
        component,
        props?.get(component.id),
        wrappers?.get(component.id),
        texts?.get(component.id),
      ),
    ),
    // 视图层与事件层会各自产出同一个元素节点，这里按 id 收敛成一个
    ...mergeNodes(extraNodes),
  ].sort(compareNodes);

  const sortedExternal = [...externalNodes].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));

  return {
    version: SCHEMA_VERSION,
    analysisId: request.analysisId,
    nodes: [...internalNodes, ...sortedExternal],
    edges: [...edges].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)),
    warnings: [...warnings],
    meta: {
      projectId: request.projectId,
      sample: false,
      ...(request.framework ? { framework: request.framework } : {}),
    },
  };
}
