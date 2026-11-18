/**
 * 视图层分析：把「页面上用户看得见、点得到的东西」还原成元素节点与 `contains` 关系。
 *
 * ## 为什么必须补这一层
 *
 * 其余分析器产出的都是**程序结构**：组件渲染了组件、组件用了 hook、组件发了请求。
 * 需求描述的却是**界面结构**：「订单列表页的工具栏有个『导出』按钮」「表格里有
 * 『姓名』列」「点开详情弹窗」。这两者之间唯一的桥梁是 JSX 视图树，而当前图上
 * 只有组件之间的 renders 边，宿主元素一个都不产（renders 明确跳过小写标签）。
 *
 * 结果就是：需求定位只能停在「哪个页面」这一档，说不清「页面里的哪块交互」。
 * 本模块补的就是这一层——**不新增 NodeKind**，复用 `element`（它的语义本来就是
 * "JSX 元素"），用 `attrs.role` 区分为控件 / 容器 / 文本 / 链接。
 *
 * ## 收录什么、不收录什么
 *
 * 全量收 JSX 会让一个页面产生几百个 `div` / `span` 节点，图变成噪音。因此只收
 * **有语义的元素**，四类来源都是可枚举的语法事实：
 *
 * | 收录条件 | role | 例子 |
 * | --- | --- | --- |
 * | 宿主控件标签 | `control` | `<button>` `<input>` `<select>` `<textarea>` |
 * | 宿主链接标签 | `link` | `<a>` |
 * | 宿主语义容器 | `container` | `<form>` `<table>` `<dialog>` `<nav>` `<header>` |
 * | 宿主文本标签 | `text` | `<h1>` `<dt>` `<dd>` `<th>` `<td>` `<label>` |
 * | 通用 UI 组件名 | `control` / `container` | `<Button>` `<Select>` `<Modal>` `<Table>` |
 * | 链接组件 | `link` | `<Link>` `<NavLink>` |
 * | 带可见文案的任意元素 | 按标签判定 | `<p>加载中…</p>` |
 * | 带字面量 `columns` 的元素 | `container` | `<ProTable columns={[{title:"姓名"}]} />` |
 *
 * 最后一条是为了业务封装组件：`ProTable` 这类名字认不出角色，但它的
 * `columns` 字面量直接说明了「这是个表格，列是姓名 / 邮箱」——需求里最常出现的
 * 恰恰是列名。用语法事实（属性里写了什么）代替对业务名的判断。
 *
 * 宿主文本标签即使文案是动态的（`<h1>{title}</h1>`）也收录：标签本身说明了
 * 「这是一级标题」，结构事实成立，文案记空即可。
 *
 * ## 组件标签不进视图层
 *
 * `<TableCard rows={rows} />` 是组件实例，它的语义由 renders 边承载（UserList
 * renders TableCard）。视图层再产一个 `<TableCard>` 元素节点是针对同一件事的
 * 第二种表达，只会让图变噪。例外是它带事件（events 层会收，按同 id 合并）或
 * 带 columns —— 那时它提供的是组件图没有的信息。
 *
 * `div` / `span` / `p` 同理：布局噪声，只有在带可见文案时才成节点。
 *
 * ## 父子关系用 contains 表达
 *
 * 从元素向上找最近的、**也在本层收录范围内**的祖先元素作为父节点；找不到就连到
 * 所属组件。中间被跳过的 `div` 不产生节点，也不会让子节点变成孤儿。
 *
 * 跨函数边界（`rows.map(() => <RowItem />)`）时父节点回退为所属组件：回调里的
 * 元素是在渲染时产生的，把外层 JSX 当成它的父级会让视图树出现一条不存在的
 * 包含关系。这与 hooks 的「遇函数即停」是同一条谨慎原则。
 *
 * ## 表格列是配置，不是 JSX
 *
 * `columns: [{ title: "姓名", dataIndex: "name" }]` 是表格的可见结构，却写在
 * 属性值里。需求里的「姓名列」只能靠它召回，所以单独提取进 `attrs.columns`。
 * 只认字面量数组 + 对象字面量；变量引用记 `columns_dynamic` 并**不猜内容**。
 */

import type { Node } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { Evidence, IREdge, IRNode, Warning } from "@uicodeanalysis/contracts";

import type { JsxElementInfo, JsxInventory } from "./jsx-inventory.js";

/** 元素结构与文案都是语法事实。 */
const STRUCTURE_CONFIDENCE = 1;
/** 父子关系跨过了被跳过的布局元素，但仍由 AST 祖先链确定。 */
const CONTAINS_CONFIDENCE = 0.9;

/** 元素在视图里的角色。 */
export type ViewRole = "control" | "container" | "text" | "link";

/**
 * 元素是否进视图层。
 *
 * 判据全部是语法事实：
 *  - 宿主标签在名单里（按钮 / 输入 / 链接 / 语义容器 / 文本标签）
 *  - 组件标签是生态层通用 UI 组件（`Button` / `Table` 这类，跨组件库一致）
 *  - 或者它带了字面量 `columns`（业务封装表格的唯一结构线索）
 *  - 或者它带可见文案（`<p>加载中…</p>`）
 *
 * 都不满足就是布局噪声或普通组件实例，交给 renders / events 承载。
 */
function decideInclude(
  info: JsxElementInfo,
  columnsResult: ReturnType<typeof columnsOf>,
  viewText: string | null,
): { include: boolean; role: ViewRole } {
  if (info.isHost) {
    const host = classifyHostTag(info.tag);
    // option 是 select 的候选项：只有带文案才值得单列
    if (host?.role === "text" && host.requireText && viewText === null) {
      return { include: false, role: "text" };
    }
    if (host) return { include: true, role: host.role };
    // 布局元素（div / span 等）：只有带可见文案才收录
    return { include: viewText !== null, role: "text" };
  }

  const component = classifyComponentTag(info.tag);
  if (component.include) return { include: true, role: component.role };

  // 业务封装组件：columns 是表结构，属于需求最常引用的界面事实
  if (columnsResult !== null) return { include: true, role: "container" };

  // 普通组件实例由 renders 边表达，视图层不重复（带事件的由事件层收录）
  return { include: false, role: component.role };
}

/** 元素内部是否有非字面量内容（表达式、嵌套元素），用于判断文案是否为动态。 */
function hasNonLiteralContent(opening: Node): boolean {
  if (!MorphNode.isJsxOpeningElement(opening)) return false;
  const element = opening.getParentIfKind(SyntaxKind.JsxElement);
  if (!element) return false;
  return element.getJsxChildren().some((child) => !MorphNode.isJsxText(child));
}

/**
 * 宿主控件标签。
 *
 * 这是 HTML 的语言级约定（`<button>` 就是按钮），与「禁止按 ProTable 这类业务名
 * 判断」是两回事：后者只对一个项目成立，前者对所有项目一致。
 */
const HOST_CONTROL_TAGS: ReadonlySet<string> = new Set([
  "button",
  "input",
  "select",
  "textarea",
  "summary",
]);

/** 宿主链接标签：跳转分析要依赖它们存在，因此独立成 role。 */
const HOST_LINK_TAGS: ReadonlySet<string> = new Set(["a"]);

/**
 * 宿主语义容器。
 *
 * 只收「页面结构上有意义」的标签。`div` / `span` / `p` 不在名单里——它们是
 * 布局噪声，全收会让图膨胀十倍而检索价值接近零。
 */
const HOST_CONTAINER_TAGS: ReadonlySet<string> = new Set([
  "form",
  "table",
  "thead",
  "tbody",
  "tfoot",
  "tr",
  "li",
  "dialog",
  "fieldset",
  "details",
  "nav",
  "header",
  "footer",
  "main",
  "aside",
  "section",
  "ul",
  "ol",
  "dl",
  "menu",
  "figure",
]);

/**
 * 宿主文本标签：标签名本身说明「这段是给人读的」。
 *
 * 收它们是因为需求里的措辞常直接对应标签语义（「标题」「状态说明」），
 * 而且这类标签数量有限、都在正文里，不会像 `div` 一样泛滥。
 *
 * 文案是动态的（`<h1>{title}</h1>`）也收：标签本身说明「这是一级标题」，
 * 结构事实成立，只是文案定不下来。
 */
const HOST_TEXT_TAGS: ReadonlySet<string> = new Set([
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "p",
  "label",
  "legend",
  "caption",
  "dt",
  "dd",
  "th",
  "td",
  "figcaption",
  "strong",
  "em",
  "b",
  "i",
  "small",
  "time",
  "code",
  "blockquote",
]);

/**
 * 必须带文案才有意义的文本标签。
 *
 * `option` 是候选项：`<option value="1" />` 在界面上是空白，没有任何结构信息；
 * 带文案时它是「可选值」，属于 text。
 */
const HOST_TRANSIENT_TEXT_TAGS: ReadonlySet<string> = new Set(["option"]);

/** 组件名里语义明确的部分：控件 / 容器 / 链接。其余组件标签一律不收录。 */
function classifyComponentTag(tag: string): { role: ViewRole; include: boolean } {
  const last = lastSegment(tag);
  if (LINK_NAMES.has(last)) return { role: "link", include: true };
  if (UI_CONTROL_NAMES.has(last)) return { role: "control", include: true };
  if (UI_CONTAINER_NAMES.has(last)) return { role: "container", include: true };
  // 名字认不出的业务封装组件：交给 renders 边表达，视图层不重复
  return { role: "container", include: false };
}

/**
 * 通用 UI 组件的控件名。
 *
 * 与 `api.ts` 的 HTTP 客户端名单同性质：这些是**生态层**的通用原语，在几乎所有
 * 组件库里都叫这个名字（Ant Design / MUI / Arco / Semi 一致）。业务封装名
 * （`ProTable` / `BizTable`）一个都不在此列，它们靠 `mapping.json` 声明。
 */
const UI_CONTROL_NAMES: ReadonlySet<string> = new Set([
  "Button",
  "Input",
  "InputNumber",
  "Select",
  "Textarea",
  "TextArea",
  "Checkbox",
  "Radio",
  "Switch",
  "Option",
  "Upload",
  "DatePicker",
  "TimePicker",
  "Search",
  "Slider",
  "Rate",
]);

/** 通用 UI 组件的容器名。 */
const UI_CONTAINER_NAMES: ReadonlySet<string> = new Set([
  "Form",
  "FormItem",
  "FormItemProps",
  "Modal",
  "Drawer",
  "Dialog",
  "Table",
  "Card",
  "Tabs",
  "TabPane",
  "Collapse",
  "Panel",
  "List",
  "Menu",
  "Dropdown",
  "Popconfirm",
  "Tooltip",
  "Descriptions",
  "Steps",
]);

/**
 * 链接类组件名：视图层把它们记为 `link`。
 *
 * 只收会渲染出可见链接的组件。`Navigate` / `Redirect` 是**声明式跳转**，不渲染
 * 任何东西，它们的语义由 `navigate.ts` 的 navigates 边表达，不进视图层。
 */
const LINK_NAMES: ReadonlySet<string> = new Set(["Link", "NavLink"]);

/** 承载可见文案的属性白名单（与 ui-text 同一套：用户看得见的才收）。 */
const VIEW_TEXT_ATTRS: readonly string[] = [
  "placeholder",
  "title",
  "label",
  "alt",
  "aria-label",
];

/** 表格列的数据键候选。 */
const COLUMN_TITLE_KEYS = ["title", "label", "header"];
const COLUMN_DATA_KEYS = ["dataIndex", "dataKey", "key", "name", "prop", "field"];

/** 单组件视图节点上限：attrs 要顺 IPC 传到前端，不能无限膨胀。 */
const MAX_VIEW_NODES_PER_COMPONENT = 120;

export interface ViewAnalysisResult {
  /** 视图层元素节点（可能与 events 产出同 id，由编排层合并 attrs） */
  elementNodes: IRNode[];
  /** 组件/元素 ─contains→ 元素 */
  edges: IREdge[];
  warnings: Warning[];
}

export interface ViewAnalysisInput {
  inventory: JsxInventory;
  /**
   * 由其他分析器（事件层）收录、但不在视图层收录范围内的元素 id。
   *
   * 这些元素已经是图上的节点，只是它们的信息来自「点了会怎样」而不是
   * 「长什么样」。它们同样需要 `contains` 父边——否则在视图树里是孤儿，
   * 用户点开一个按钮却看不出它在页面的哪一块。
   *
   * 本层只给它们补父子关系，**不产节点、不改 attrs**：节点的内容归收录方。
   */
  extraElementIds?: ReadonlySet<string>;
}

/** 每个元素在视图层的归属：父节点 id 与角色。 */
interface ViewEntry {
  info: JsxElementInfo;
  role: ViewRole;
  viewText: string | null;
  /** true 表示文案来自变量/表达式，定不下来；结构事实仍然成立 */
  textDynamic: boolean;
  columns: { title: string; dataIndex: string | null }[] | null;
  /**
   * 节点由别的分析器产出，本层只补 contains 边。
   *
   * 这类元素在视图树里必须占位（否则子元素找不到父级），但不能重复产节点。
   */
  edgeOnly: boolean;
}

/** 取标签最后一段（`Antd.Button` → `Button`）。 */
function lastSegment(tag: string): string {
  return tag.split(".").pop() ?? tag;
}

/**
 * 宿主标签的角色判定；返回 null 表示是布局元素（由文案决定是否收录）。
 */
interface HostDecision {
  role: ViewRole;
  /**
   * true 表示「没有文案就不收录」。
   *
   * 只对 `option` 这类标签成立：它没有文案就没有任何信息（`<option value="1" />`
   * 在界面上是空白）。反过来 `<span />` 这种布局元素也靠文案收录，
   * 但那是 decideInclude 的兜底分支，不走这里。
   */
  requireText: boolean;
}

function classifyHostTag(tag: string): HostDecision | null {
  if (tag === "Fragment" || tag === "React.Fragment") return null;
  if (HOST_CONTROL_TAGS.has(tag)) return { role: "control", requireText: false };
  if (HOST_LINK_TAGS.has(tag)) return { role: "link", requireText: false };
  if (HOST_CONTAINER_TAGS.has(tag)) return { role: "container", requireText: false };
  if (HOST_TRANSIENT_TEXT_TAGS.has(tag)) return { role: "text", requireText: true };
  if (HOST_TEXT_TAGS.has(tag)) return { role: "text", requireText: false };
  // div / span 等布局元素：只有带可见文案时才由调用方破例收录
  return null;
}

/** 取属性值节点：`x="y"` / `x={"y"}` 都收窄到值本身。 */
function attributeValue(opening: Node, name: string): Node | null {
  if (!MorphNode.isJsxOpeningElement(opening) && !MorphNode.isJsxSelfClosingElement(opening)) {
    return null;
  }
  const attr = opening
    .getAttributes()
    .find((item) => MorphNode.isJsxAttribute(item) && item.getNameNode().getText() === name);
  if (!attr || !MorphNode.isJsxAttribute(attr)) return null;
  const initializer = attr.getInitializer();
  if (!initializer) return null;
  if (MorphNode.isJsxExpression(initializer)) return initializer.getExpression() ?? null;
  return initializer;
}

/** 字符串字面量值；不是字面量返回 null。 */
function literalValue(node: Node | null): string | null {
  if (!node) return null;
  if (MorphNode.isStringLiteral(node)) return node.getLiteralText();
  if (MorphNode.isNoSubstitutionTemplateLiteral(node)) return node.getLiteralText();
  return null;
}

/** 外链判定：绝对地址或协议相对地址。站内路径（`/x`、`x`）不是外链。 */
const EXTERNAL_URL_PATTERN = /^(?:https?:)?\/\//i;

export function isExternalUrl(url: string): boolean {
  return EXTERNAL_URL_PATTERN.test(url.trim());
}

/**
 * 取链接元素的目标地址。
 *
 * 只对宿主 `<a>` 生效，且只认字面量——`to={href}` 这类变量引用定不下来，
 * 返回 null 而不是猜。地址写进 `attrs.href` 的原因：它不是可见文案
 * （所以不进 ui-text 的白名单），却是「点击后跳到哪」的唯一静态线索。
 * 需求说「页面上那个 GitHub 链接」时，`https://github.com/...` 里的
 * `github` 正是能把它捞出来的依据。
 *
 * 外链不产 `navigates` 边：路由表里没有对应节点，编造边就违反了
 * 「不编造」的原则。用 `attrs.external` 标出来，链路推导据此标注断点。
 */
function linkHrefOf(info: JsxElementInfo): string | null {
  if (!info.isHost || info.tag !== "a") return null;
  const value = literalValue(attributeValue(info.opening, "href"));
  if (value === null) return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * 取元素的可见文案。
 *
 * 来源与 ui-text.ts 一致（JSX 文本子节点 + 白名单属性），但用途不同：
 * ui-text 是**组件级**检索语料，这里是**元素级**标签——「这个按钮上写着什么」
 * 是交互定位的最小单位。
 *
 * 只接受「子节点全是纯文本」的情形。`<div>共 {total} 条</div>` 若取静态片段
 * 会得到「共 条」这种残缺文案，检索时反而误导——含插值就当作定不下来，
 * 记进元素的 `viewTextDynamic`，**不拼凑**。
 */
function viewTextOf(opening: Node): string | null {
  for (const name of VIEW_TEXT_ATTRS) {
    const value = literalValue(attributeValue(opening, name));
    if (value && value.trim().length > 0) return value.trim();
  }

  if (!MorphNode.isJsxOpeningElement(opening)) return null;
  const element = opening.getParentIfKind(SyntaxKind.JsxElement);
  if (!element) return null;

  const parts: string[] = [];
  for (const child of element.getJsxChildren()) {
    // 有任何非文本子节点（表达式、嵌套元素）就说明文案不是静态的
    if (!MorphNode.isJsxText(child)) return null;
    const text = child.getLiteralText().replace(/\s+/g, " ").trim();
    if (text.length > 0) parts.push(text);
  }
  if (parts.length === 0) return null;
  return parts.join(" ").trim();
}

/**
 * 提取表格列。
 *
 * 只认「数组字面量 + 对象字面量 + 字面量标题」：变量引用、`columns.map(...)`、
 * 二次加工都定不下来具体列，返回 null 并让调用方记告警——不猜。
 */
function columnsOf(
  opening: Node,
): { columns: { title: string; dataIndex: string | null }[]; dynamic: boolean } | null {
  const value = attributeValue(opening, "columns");
  if (!value) return null;
  if (!MorphNode.isArrayLiteralExpression(value)) return { columns: [], dynamic: true };

  const columns: { title: string; dataIndex: string | null }[] = [];
  for (const element of value.getElements()) {
    if (!MorphNode.isObjectLiteralExpression(element)) continue;
    let title: string | null = null;
    for (const key of COLUMN_TITLE_KEYS) {
      const prop = element.getProperty(key);
      if (prop && MorphNode.isPropertyAssignment(prop)) {
        title = literalValue(prop.getInitializer() ?? null);
        if (title) break;
      }
    }
    if (!title) continue;

    let dataIndex: string | null = null;
    for (const key of COLUMN_DATA_KEYS) {
      const prop = element.getProperty(key);
      if (prop && MorphNode.isPropertyAssignment(prop)) {
        dataIndex = literalValue(prop.getInitializer() ?? null);
        if (dataIndex) break;
      }
    }
    columns.push({ title: title.trim(), dataIndex });
  }
  return { columns, dynamic: false };
}

/**
 * 打开标签所属的元素节点本身（`JsxElement` / `JsxSelfClosingElement`）。
 *
 * 打开标签的**父节点就是它自己的元素**——这是 `nearestJsxAncestor` 必须先用它
 * 跳一层的原因，否则第一个"祖先"会是元素自己，contains 边就成了自环。
 */
function elementOfOpening(opening: Node): Node | null {
  if (MorphNode.isJsxOpeningElement(opening)) {
    return opening.getParentIfKind(SyntaxKind.JsxElement) ?? null;
  }
  if (MorphNode.isJsxSelfClosingElement(opening)) return opening;
  return null;
}

/**
 * 向上找最近的 JSX 元素祖先，**遇到函数边界就停**。
 *
 * 起点是「元素自己的父级」而不是打开标签的父级：后者就是元素本身。
 *
 * 停下来是刻意的：`render: () => <span />` 这类回调里的元素在渲染时才产生，
 * 把它算成外层表格的子节点会捏造一条不存在的包含关系。
 */
function nearestJsxAncestor(opening: Node): Node | null {
  const self = elementOfOpening(opening);
  let current: Node | undefined = self ? self.getParent() : opening.getParent();
  while (current) {
    if (
      MorphNode.isJsxElement(current) ||
      MorphNode.isJsxSelfClosingElement(current) ||
      MorphNode.isJsxFragment(current)
    ) {
      return current;
    }
    if (
      MorphNode.isArrowFunction(current) ||
      MorphNode.isFunctionExpression(current) ||
      MorphNode.isFunctionDeclaration(current) ||
      MorphNode.isSourceFile(current)
    ) {
      return null;
    }
    current = current.getParent();
  }
  return null;
}

/** 打开标签或自闭合标签的「元素节点」本身（父链上要判断的节点类型）。 */
function openingOf(node: Node): Node | null {
  if (MorphNode.isJsxElement(node)) return node.getOpeningElement();
  if (MorphNode.isJsxSelfClosingElement(node)) return node;
  return null;
}

export function analyzeView(input: ViewAnalysisInput): ViewAnalysisResult {
  const { inventory } = input;
  const extraElementIds = input.extraElementIds ?? new Set<string>();

  const warnings: Warning[] = [];
  const entries = new Map<string, ViewEntry>();
  const elementNodes: IRNode[] = [];
  const perComponent = new Map<string, number>();
  const truncated = new Set<string>();

  // 第一遍：决定哪些元素进视图层
  for (const [file, fileEntries] of inventory.byFile) {
    for (const info of fileEntries) {
      const columnsResult = columnsOf(info.opening);
      const viewText = viewTextOf(info.opening);
      const decision = decideInclude(info, columnsResult, viewText);
      const edgeOnly = !decision.include && extraElementIds.has(info.id);
      if (!decision.include && !edgeOnly) continue;

      // edgeOnly 元素不产节点，不占视图节点额度：额度是给 attrs 体积设的
      if (!edgeOnly) {
        const count = perComponent.get(info.componentId) ?? 0;
        if (count >= MAX_VIEW_NODES_PER_COMPONENT) {
          // 只告警一次：截断是整批发生的，逐元素告警会把告警面板刷满
          if (!truncated.has(info.componentId)) {
            truncated.add(info.componentId);
            warnings.push({
              code: "view_nodes_truncated",
              severity: "info",
              stage: "static",
              file,
              loc: info.loc,
              message: `组件 ${info.componentId} 的视图节点超过 ${MAX_VIEW_NODES_PER_COMPONENT} 个，后续元素未纳入视图层`,
            });
          }
          continue;
        }
        perComponent.set(info.componentId, count + 1);
      }

      // 同一 id 已被更早的元素占用：说明后缀规则撞车，保留先出现的并告警
      if (entries.has(info.id)) {
        warnings.push({
          code: "duplicate_element_id",
          severity: "warning",
          stage: "static",
          file,
          loc: info.loc,
          message: `元素节点 id 重复：${info.id}；已保留先出现的元素，请检查 id 生成规则`,
        });
        continue;
      }

      if (columnsResult?.dynamic) {
        warnings.push({
          code: "columns_dynamic",
          severity: "info",
          stage: "static",
          file,
          loc: info.loc,
          message: `${info.tag} 的 columns 不是字面量数组，列名未纳入视图层`,
        });
      }

      // 文案是动态的（`<h1>{title}</h1>`）：结构仍是事实，文案记空并如实标注
      const textDynamic = viewText === null && hasNonLiteralContent(info.opening);

      entries.set(info.id, {
        info,
        role: decision.role,
        viewText,
        textDynamic,
        // columns 是**属性**上的结构，与元素是否收录无关：ProTable 这类业务封装
        // 组件只有靠 columns 才能进视图层，所以要在 decideInclude 里先算出来
        columns: columnsResult && !columnsResult.dynamic ? columnsResult.columns : null,
        edgeOnly,
      });
    }
  }

  // 第二遍：产节点与 contains 边
  const edges = new Map<string, IREdge>();
  for (const entry of entries.values()) {
    const { info, role, viewText, columns, textDynamic, edgeOnly } = entry;
    const evidence: Evidence = {
      source: "static",
      confidence: STRUCTURE_CONFIDENCE,
      file: info.file,
      loc: info.loc,
    };

    // edgeOnly：节点由事件层产出，attrs 归它写，本层只补父子关系
    if (!edgeOnly) {
      const href = linkHrefOf(info);
      elementNodes.push({
        id: info.id,
        kind: "element",
        name: viewText ? `<${info.tag}> ${viewText}` : `<${info.tag}>`,
        file: info.file,
        loc: info.loc,
        attrs: {
          tag: info.tag,
          target: info.isHost ? "host" : "component",
          viewRole: role,
          ...(viewText ? { viewText } : {}),
          // 文案来自变量而定不下来：如实标注，别让读者以为元素没有文案
          ...(textDynamic ? { viewTextDynamic: true } : {}),
          ...(columns ? { columns } : {}),
          // 链接目标：不产 navigates 边（外链没有 route 节点），但它是
          // 「点击后到哪」的唯一静态线索，也是链路推导判断断点的依据
          ...(href ? { href, external: isExternalUrl(href) } : {}),
        },
        evidence: [evidence],
      });
    }

    const parentId = resolveParentId(info.opening, info.componentId, inventory, entries);
    edges.set(`e_contains_${parentId}__${info.id}`, {
      id: `e_contains_${parentId}__${info.id}`,
      from: parentId,
      to: info.id,
      kind: "contains",
      evidence: [
        {
          source: "static",
          confidence: parentId === info.componentId ? STRUCTURE_CONFIDENCE : CONTAINS_CONFIDENCE,
          file: info.file,
          loc: info.loc,
        },
      ],
    });
  }

  return { elementNodes, edges: [...edges.values()], warnings };
}

/**
 * 解析元素的 contains 父节点。
 *
 * 沿祖先链向上找第一个「也在视图层里」的元素；找不到就是组件的顶层元素。
 * 被跳过的布局元素（`div` 等）不产生节点，但也不打断归属——子节点接到最近的
 * 有语义祖先上，视图树依然完整。
 */
function resolveParentId(
  opening: Node,
  componentId: string,
  inventory: JsxInventory,
  entries: ReadonlyMap<string, ViewEntry>,
): string {
  let current = nearestJsxAncestor(opening);
  while (current) {
    const parentOpening = openingOf(current);
    if (parentOpening) {
      // 清单本身就是 opening → 条目的索引，不必再线性扫一遍 entries
      const parentInfo = inventory.byOpening.get(parentOpening);
      if (parentInfo && entries.has(parentInfo.id)) return parentInfo.id;
    }
    current = nearestJsxAncestor(current);
  }
  return componentId;
}
