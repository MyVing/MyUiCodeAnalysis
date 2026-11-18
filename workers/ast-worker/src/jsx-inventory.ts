/**
 * JSX 元素清单：为**每个** JSX 开标签分配唯一、稳定的元素节点 id。
 *
 * ## 为什么需要一份统一清单
 *
 * 视图层（`view.ts`）要回答「页面上有什么」，事件层（`events.ts`）要回答
 * 「点了会发生什么」——两者描述的是同一个 `<button>`。如果各自生成 id，
 * 同一个元素会在图上变成两个节点，视图树与事件链就断开了。
 *
 * 因此 id 只在这里计算一次，两个分析器都按「元素节点」查表复用。**任何
 * 一方改动遍历顺序、计数器语义，都会让另一方的 id 漂移**，这是本模块存在的
 * 全部理由：把这条约束收敛到一个文件里。
 *
 * ## id 形状
 *
 * `src/pages/UserList.tsx#UserList/jsx:button/handleExport`
 *                └─ 所属组件 id ─┘ └── 元素后缀 ──┘
 *
 * - 不用行列号：组件上方插一行注释就会让所有元素 id 漂移；
 * - 具名处理函数恰好一个时用它作后缀：稳定且可读；
 * - 否则用「标签 + 组件内同标签序号」：`jsx:div`、`jsx:div#2`。
 *
 * ## 计数器覆盖全部元素，而不只是产出节点的元素
 *
 * 序号按「同一组件内同一标签的出现顺序」计算，与某个元素最终是否被视图层
 * 选中无关。否则「先加一个带事件的按钮」会把后面所有按钮的序号推后，id 随
 * 分析器的筛选规则漂移——那是最难查的一类不稳定。
 *
 * ## 归属
 *
 * 只登记归属于某个组件的元素。模块顶层 JSX（路由配置、常量）不归任何组件，
 * 从清单里缺席——它本来就不该出现在组件视图树里。
 */

import type { Node, SourceFile } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { DiscoveredComponent } from "./components.js";
import { buildOwnerIndex, findOwner } from "./ownership.js";
import { toProjectRelative } from "./paths.js";

/** React 事件属性约定：on + 大写字母（对所有项目一致，不是业务名）。 */
const EVENT_ATTR_PATTERN = /^on[A-Z]/;

/** 一个 JSX 开标签的清单条目。 */
export interface JsxElementInfo {
  /** 完整元素节点 id（含所属组件前缀） */
  id: string;
  /** 所属组件 id；清单里只存在有归属的元素 */
  componentId: string;
  /** 标签源码文本（`div` / `TableCard` / `Antd.Button`） */
  tag: string;
  /** 是否宿主元素（小写开头，编译成 DOM 标签） */
  isHost: boolean;
  /** 项目相对路径 */
  file: string;
  loc: { line: number; col: number };
  /** 事件属性上的具名处理函数（`onClick={handleSelect}` → handleSelect） */
  handlerNames: string[];
  /** 打开标签节点本身 */
  opening: Node;
}

export interface JsxInventory {
  /** 打开标签节点 → 清单条目 */
  byOpening: ReadonlyMap<Node, JsxElementInfo>;
  /** 元素节点 id → 条目（同一 id 只会有一条） */
  byId: ReadonlyMap<string, JsxElementInfo>;
  /**
   * 按文件分组的条目，保持文档顺序。
   *
   * 视图层需要「按文档顺序」建立父子关系与兄弟序号；从 byId 取再排序会
   * 丢掉跨文件的遍历顺序。
   */
  byFile: ReadonlyMap<string, JsxElementInfo[]>;
}

/** 宿主元素判定：与 renders / routes 共用同一套 JSX 编译语义。 */
export function isHostTag(tag: string): boolean {
  if (tag === "Fragment" || tag === "React.Fragment") return true;
  const last = tag.split(".").pop() ?? tag;
  return !/^[A-Z]/.test(last);
}

/** 打开标签的标签名节点。 */
function tagNameNodeOf(opening: Node): Node | null {
  if (MorphNode.isJsxOpeningElement(opening) || MorphNode.isJsxSelfClosingElement(opening)) {
    return opening.getTagNameNode();
  }
  return null;
}

/** 收集事件属性上的具名处理函数：只认 `onClick={identifier}` 形态。 */
function readHandlerNames(opening: Node): string[] {
  if (!MorphNode.isJsxOpeningElement(opening) && !MorphNode.isJsxSelfClosingElement(opening)) {
    return [];
  }
  const names: string[] = [];
  for (const attr of opening.getAttributes()) {
    if (!MorphNode.isJsxAttribute(attr)) continue;
    if (!EVENT_ATTR_PATTERN.test(attr.getNameNode().getText())) continue;
    const initializer = attr.getInitializer();
    if (!initializer || !MorphNode.isJsxExpression(initializer)) continue;
    const expression = initializer.getExpression();
    if (expression && MorphNode.isIdentifier(expression)) names.push(expression.getText());
  }
  return names;
}

/** 元素后缀：唯一具名处理函数优先，否则「标签 + 组件内序号」。 */
function elementSuffix(
  tag: string,
  handlerNames: readonly string[],
  key: string,
  counters: Map<string, number>,
): string {
  if (handlerNames.length === 1) return `jsx:${tag}/${handlerNames[0]}`;
  const next = (counters.get(key) ?? 0) + 1;
  counters.set(key, next);
  return next === 1 ? `jsx:${tag}` : `jsx:${tag}#${next}`;
}

/**
 * 建立 JSX 元素清单。
 *
 * 遍历域：项目源码里的全部文件；每个文件内的 JSX 开标签按**字符偏移排序**，
 * 保证文档顺序与源文件书写顺序一致（`getDescendantsOfKind` 两次拼接后的
 * 顺序不可依赖）。
 */
export function buildJsxInventory(
  rootPath: string,
  project: { getSourceFiles(): readonly SourceFile[] },
  components: readonly DiscoveredComponent[],
  declarations: ReadonlyMap<string, Node>,
): JsxInventory {
  const ownerIndex = buildOwnerIndex(components, declarations);
  const byOpening = new Map<Node, JsxElementInfo>();
  const byId = new Map<string, JsxElementInfo>();
  const byFile = new Map<string, JsxElementInfo[]>();
  // 计数键是「组件 id + 标签」：不同组件的 div 互不干扰
  const counters = new Map<string, number>();

  for (const file of project.getSourceFiles()) {
    const relativeFile = toProjectRelative(rootPath, file.getFilePath());
    if (relativeFile === null) continue;

    const openings = [
      ...file.getDescendantsOfKind(SyntaxKind.JsxOpeningElement),
      ...file.getDescendantsOfKind(SyntaxKind.JsxSelfClosingElement),
    ].sort((a, b) => a.getStart() - b.getStart());

    const entries: JsxElementInfo[] = [];
    for (const opening of openings) {
      const tagNode = tagNameNodeOf(opening);
      if (!tagNode) continue;

      const componentId = findOwner(opening, ownerIndex, rootPath);
      if (!componentId) continue;

      const tag = tagNode.getText();
      const handlerNames = readHandlerNames(opening);
      const suffix = elementSuffix(tag, handlerNames, `${componentId} jsx:${tag}`, counters);
      const id = `${componentId}/${suffix}`;

      const position = file.getLineAndColumnAtPos(tagNode.getStart());
      const info: JsxElementInfo = {
        id,
        componentId,
        tag,
        isHost: isHostTag(tag),
        file: relativeFile,
        loc: { line: position.line, col: position.column },
        handlerNames,
        opening,
      };

      byOpening.set(opening, info);
      entries.push(info);
      // 同一 id 撞车说明后缀规则有缺陷：保留先出现的，后一个由调用方告警
      if (!byId.has(id)) byId.set(id, info);
    }

    if (entries.length > 0) byFile.set(relativeFile, entries);
  }

  return { byOpening, byId, byFile };
}
