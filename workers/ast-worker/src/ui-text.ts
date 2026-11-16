/**
 * UI 文案提取：把源码里「用户能在页面上看到的文字」收集成可检索的清单。
 *
 * ## 为什么这件事必须由 AST 做，而不是 LLM
 *
 * 需求文档说的是「用户列表增加状态筛选」「查询按钮」「姓名列」，代码里对应的是
 * `placeholder="搜索用户"`、`{ title: "姓名" }`、`<h1>{title}</h1>`。这些字符串
 * 全部躺在语法树里，`getText()` 就能拿到并精确到行列号。
 *
 * 让 LLM 去读代码做这件事有三个硬伤：
 *  1. **不完备**：召回依赖穷尽，LLM 是采样式的，漏掉的文案无从察觉；
 *  2. **不可追溯**：它给不出 `loc`，而 IR 的每条结论都必须能回到源码位置；
 *  3. **不可重复**：同一份代码两次结果不同，无法做增量、无法写断言。
 *
 * 所以这里只做语法事实，confidence 恒为 1.0。语义判断（"这几个字是否对得上
 * 需求里的状态筛选"）留给后续的需求映射层，那时输入的是本模块产出的候选集，
 * 而不是原始代码文件。
 *
 * ## 提取的来源（六类，全部是字面量）
 *
 * | 来源 | 例子 | kind |
 * | --- | --- | --- |
 * | JSX 文本节点 | `<p>加载中…</p>` | `text` |
 * | JSX 子节点里的字符串字面量 | `{"暂无数据"}` | `text` |
 * | 白名单属性 | `placeholder="搜索用户"` | `attr` |
 * | 对象字面量的文本类键 | `{ title: "姓名" }` | `object` |
 * | 字符串数组字面量 | `options={["正常", "禁用"]}` | `array` |
 * | 模板字符串的静态片段 | `` `共${total}条` `` → `共` `条` | `template` |
 * | i18n 调用键 | `t('user.list.name')` | `i18n` |
 *
 * ## 求不出来的表达式单独记录
 *
 * `<h1>{title}</h1>` 里的 `title` 是变量，字面量不在本文件——它不是"没有文案"，
 * 而是"文案来自别处"。这类表达式记进 `dynamic`，前端标成「动态待解析」。
 * 本期不做跨文件求值（那需要沿 props 与常量引用回溯），但把它记下来，
 * 读者才不会以为这个组件真的没有标题。
 *
 * ## 归属与边界
 *
 * 归属复用 renders / events 的同一套规则：往上找最近的组件定义节点。
 * 不归属任何组件的文案（模块级 `const columns = [...]`、普通工具函数里的对象）
 * 只计数并记 info 告警，**不猜测它属于哪个组件**。真实项目里 `columns` 常写在
 * 组件外，这是本期明确的能力边界，宁可报出来也不编造归属。
 */

import type { Node, SourceFile } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import type { Warning } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";
import { buildOwnerIndex, findOwner } from "./ownership.js";

/** 文案来源分类，前端据此分组显示。 */
export type UiTextKind = "text" | "attr" | "object" | "array" | "template" | "i18n";

/** 一条已确定的文案。 */
export interface UiTextEntry {
  /** 文案原文（JSX 空白已折叠、常见实体已解码） */
  text: string;
  kind: UiTextKind;
  /** 来自属性名或对象键名；JSX 文本节点为 null */
  via: string | null;
  loc: { line: number; col: number };
}

/** 一处字面量定不下来的文案表达式（如 `{title}`、`{t(key)}`）。 */
export interface DynamicTextEntry {
  /** 表达式的源码文本 */
  expr: string;
  loc: { line: number; col: number };
}

/** 单个组件收集到的文案。 */
export interface ComponentText {
  texts: UiTextEntry[];
  dynamic: DynamicTextEntry[];
  /** 因超过上限而未记录的条数，用于如实统计而不是静默截断 */
  truncated: number;
}

export interface UiTextAnalysisResult {
  /** 组件 id → 文案 */
  byComponent: Map<string, ComponentText>;
  warnings: Warning[];
}

export interface UiTextAnalysisInput {
  rootPath: string;
  components: readonly DiscoveredComponent[];
  declarations: ReadonlyMap<string, Node>;
  project: { getSourceFiles(): readonly SourceFile[] };
}

/** 单组件文案上限：attrs 是要顺着 IPC 传到前端的，不能让它无限膨胀。 */
const MAX_TEXTS_PER_COMPONENT = 80;
/** 单组件动态表达式上限。 */
const MAX_DYNAMIC_PER_COMPONENT = 40;
/** 动态表达式只记短表达式：过长的一般是整段 JSX 条件渲染，没有检索价值。 */
const DYNAMIC_EXPR_LIMIT = 60;

/**
 * 可见文案属性白名单。
 *
 * 用白名单而不是"所有字符串属性"：`className` / `id` / `href` / `src` /
 * `data-testid` 同样是字符串属性，却一个都不是用户能看到的文字。
 */
const TEXT_ATTRS: ReadonlySet<string> = new Set([
  "placeholder",
  "title",
  "label",
  "alt",
  "aria-label",
  "aria-description",
  "aria-roledescription",
  "aria-valuetext",
]);

/**
 * 对象字面量里承载可见文案的键。
 *
 * 覆盖表格列（`columns: [{ title }]`）、下拉选项（`options: [{ label }]`）、
 * 表单（`rules: [{ message }]`）、空状态（`emptyText` / `description`）等
 * 企业项目里最常见的声明式配置。
 */
const TEXT_OBJECT_KEYS: ReadonlySet<string> = new Set([
  "title",
  "label",
  "placeholder",
  "text",
  "content",
  "description",
  "header",
  "caption",
  "subtitle",
  "hint",
  "helperText",
  "errorMessage",
  "message",
  "tip",
  "tooltip",
  "emptyText",
  "okText",
  "cancelText",
  "confirmText",
  "prefix",
  "suffix",
  "unit",
]);

/**
 * i18n 取词函数的受控名单。
 *
 * 与 `on[A-Z]` 同性质：这是框架/库的语言级约定，对所有项目一致，不是业务名。
 * 命中的是「取词函数」，键名本身是可检索的字符串字面量。
 */
const I18N_CALLEES: ReadonlySet<string> = new Set([
  "t",
  "translate",
  "$t",
  "i18n.t",
  "intl.formatMessage",
  "formatMessage",
]);

/** 纯符号/纯数字的文本不是 UI 文案。 */
const NON_TEXT_PATTERN = /^[\d\s.,:;!?%$+\-*/=_|\\()[\]{}<>~`^@&#"'·—–…]+$/;
const CJK_PATTERN = /[一-龥]/;

/**
 * 判断一段字面量是否值得记成文案。
 *
 * `array` 来源更嘈杂（`["px", "rem", "em"]` 这种单位表在组件内很常见），
 * 因此额外要求含中文、含空格或首字母大写——宁可漏掉几个英文单词，
 * 也不要把组件内部的配置表全部灌进 attrs。
 */
function isLikelyUiText(text: string, kind: UiTextKind): boolean {
  const trimmed = text.trim();
  if (trimmed.length === 0) return false;
  if (NON_TEXT_PATTERN.test(trimmed)) return false;

  if (kind === "array") {
    return CJK_PATTERN.test(trimmed) || /\s/.test(trimmed) || /^[A-Z][A-Za-z]/.test(trimmed);
  }

  // 单个拉丁字符没有检索价值，单个汉字有（如"姓"）
  if (trimmed.length < 2 && !CJK_PATTERN.test(trimmed)) return false;
  return true;
}

/**
 * 解码 JSX 文本里的常见 HTML 实体。
 *
 * 直接用 `getText()` 拿到的是源码原文，`&amp;` 这类在渲染时才解码。
 * 检索时用户输入的是解码后的文字，所以这里统一解一遍。
 */
function decodeEntities(text: string): string {
  return text
    .replace(/&nbsp;/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/&#(\d+);/g, (_match: string, digits: string) => {
      const code = Number(digits);
      return Number.isFinite(code) ? String.fromCodePoint(code) : _match;
    });
}

/** JSX 文本节点：折叠空白（JSX 编译器对多行缩进的处理就是折叠成单空格）。 */
function normalizeJsxText(raw: string): string {
  return decodeEntities(raw).replace(/\s+/g, " ").trim();
}

/** 取字符串字面量的值；不是字面量返回 null。 */
function stringLiteralValue(node: Node): string | null {
  if (MorphNode.isStringLiteral(node)) return node.getLiteralText();
  if (MorphNode.isNoSubstitutionTemplateLiteral(node)) return node.getLiteralText();
  return null;
}

/** 取属性名或对象键名；字符串形式的键去掉引号。 */
function memberName(node: Node): string | null {
  if (MorphNode.isIdentifier(node)) return node.getText();
  if (MorphNode.isStringLiteral(node)) return node.getLiteralText();
  return null;
}

/** 调用表达式的被调用者文本（`t` / `i18n.t`），用于 i18n 判定。 */
function calleeText(expr: Node): string | null {
  if (MorphNode.isIdentifier(expr)) return expr.getText();
  if (MorphNode.isPropertyAccessExpression(expr)) return expr.getText();
  return null;
}

/** 模板字符串的静态片段（`` `共${n}条` `` → ["共", "条"]）。 */
function templateFragments(node: Node): string[] {
  const fragments: string[] = [];
  const head = node.getChildAtIndex(0);
  if (head && MorphNode.isTemplateHead(head)) {
    fragments.push(head.getLiteralText());
  }
  for (const span of node.getChildrenOfKind(SyntaxKind.TemplateSpan)) {
    const literal = span.getLiteral();
    if (MorphNode.isTemplateMiddle(literal) || MorphNode.isTemplateTail(literal)) {
      fragments.push(literal.getLiteralText());
    }
  }
  return fragments;
}

/** 单个组件内的收集器：负责去重与上限。 */
class TextCollector {
  readonly texts: UiTextEntry[] = [];
  readonly dynamic: DynamicTextEntry[] = [];
  private readonly seen = new Set<string>();
  truncated = 0;

  add(entry: UiTextEntry): void {
    // 同一组件内相同文案只留第一次出现的位置；它本来就是同一个事实
    const key = `${entry.kind}:${entry.text}`;
    if (this.seen.has(key)) return;
    if (this.texts.length >= MAX_TEXTS_PER_COMPONENT) {
      this.truncated += 1;
      return;
    }
    this.seen.add(key);
    this.texts.push(entry);
  }

  addDynamic(entry: DynamicTextEntry): void {
    if (this.dynamic.length >= MAX_DYNAMIC_PER_COMPONENT) return;
    if (this.dynamic.some((item) => item.expr === entry.expr)) return;
    this.dynamic.push(entry);
  }
}

/** 从源码位置生成 loc（1-based）。 */
function locationOf(node: Node): { line: number; col: number } {
  const sourceFile = node.getSourceFile();
  const pos = sourceFile.getLineAndColumnAtPos(node.getStart());
  return { line: pos.line, col: pos.column };
}

/**
 * 处理一个 JSX 子节点位置的表达式（`<h1>{...}</h1>` 里的大括号）。
 *
 * 三种情况分开走：
 *  - 字符串字面量 → 确定的文案
 *  - 模板字符串 → 静态片段是文案，整体记 dynamic（插值部分定不下来）
 *  - i18n 调用 → 键名是文案（它正是文案的稳定标识）
 *  - 其他 → 只是 dynamic
 */
function handleChildExpression(expr: Node, collector: TextCollector): void {
  const literal = stringLiteralValue(expr);
  if (literal !== null) {
    if (isLikelyUiText(literal, "text")) {
      collector.add({ text: literal.trim(), kind: "text", via: null, loc: locationOf(expr) });
    }
    return;
  }

  if (MorphNode.isTemplateExpression(expr)) {
    for (const fragment of templateFragments(expr)) {
      const text = fragment.trim();
      if (isLikelyUiText(text, "template")) {
        collector.add({ text, kind: "template", via: null, loc: locationOf(expr) });
      }
    }
    collector.addDynamic({ expr: expr.getText(), loc: locationOf(expr) });
    return;
  }

  if (MorphNode.isCallExpression(expr)) {
    const callee = calleeText(expr.getExpression());
    const first = expr.getArguments()[0];
    if (callee !== null && I18N_CALLEES.has(callee) && first) {
      // formatMessage({ id: "x" }) 的键在对象里，其余取词函数取第一个参数
      const keyNode = MorphNode.isObjectLiteralExpression(first)
        ? first.getProperty("id")?.asKind(SyntaxKind.PropertyAssignment)?.getInitializer()
        : first;
      const key = keyNode ? stringLiteralValue(keyNode) : null;
      if (key !== null && key.length > 0) {
        collector.add({ text: key, kind: "i18n", via: callee, loc: locationOf(keyNode ?? first) });
        return;
      }
    }
  }

  // 只记短表达式：`showFilter && <input ... />` 这种整段 JSX 没有检索价值
  const text = expr.getText();
  if (text.length > 0 && text.length <= DYNAMIC_EXPR_LIMIT && !text.includes("<")) {
    collector.addDynamic({ expr: text, loc: locationOf(expr) });
  }
}

/** 处理一个 JSX 属性的字面量值。 */
function handleAttribute(attr: Node, collector: TextCollector): void {
  if (!MorphNode.isJsxAttribute(attr)) return;
  const name = attr.getNameNode().getText();
  if (!TEXT_ATTRS.has(name)) return;

  const initializer = attr.getInitializer();
  if (!initializer) return;

  const valueNode = MorphNode.isJsxExpression(initializer)
    ? initializer.getExpression()
    : initializer;
  if (!valueNode) return;

  const literal = stringLiteralValue(valueNode);
  if (literal !== null) {
    if (isLikelyUiText(literal, "attr")) {
      collector.add({
        text: literal.trim(),
        kind: "attr",
        via: name,
        loc: locationOf(attr.getNameNode()),
      });
    }
    return;
  }

  if (MorphNode.isTemplateExpression(valueNode)) {
    for (const fragment of templateFragments(valueNode)) {
      const text = fragment.trim();
      if (isLikelyUiText(text, "template")) {
        collector.add({
          text,
          kind: "template",
          via: name,
          loc: locationOf(attr.getNameNode()),
        });
      }
    }
    collector.addDynamic({ expr: valueNode.getText(), loc: locationOf(attr.getNameNode()) });
    return;
  }

  // `placeholder={hint}` 这类：属性名本身就是位置线索，值得记下来
  const text = valueNode.getText();
  if (text.length > 0 && text.length <= DYNAMIC_EXPR_LIMIT) {
    collector.addDynamic({ expr: `${name}={${text}}`, loc: locationOf(attr.getNameNode()) });
  }
}

export function analyzeUiText(input: UiTextAnalysisInput): UiTextAnalysisResult {
  const { rootPath, components, declarations, project } = input;

  const warnings: Warning[] = [];
  const byComponent = new Map<string, TextCollector>();
  const ownerIndex = buildOwnerIndex(components, declarations);

  /** 归属：与 renders / events 同一套规则；找不到归属就只计数，不猜 */
  function collectorFor(node: Node): TextCollector | null {
    const ownerId = findOwner(node, ownerIndex, rootPath);
    if (!ownerId) return null;
    let collector = byComponent.get(ownerId);
    if (!collector) {
      collector = new TextCollector();
      byComponent.set(ownerId, collector);
    }
    return collector;
  }

  let unassigned = 0;

  for (const file of project.getSourceFiles()) {
    const candidates: { node: Node; handle: (c: TextCollector) => void }[] = [];

    // 1. JSX 文本节点
    for (const text of file.getDescendantsOfKind(SyntaxKind.JsxText)) {
      const value = normalizeJsxText(text.getText());
      if (!isLikelyUiText(value, "text")) continue;
      candidates.push({
        node: text,
        handle: (c) =>
          c.add({ text: value, kind: "text", via: null, loc: locationOf(text) }),
      });
    }

    // 2. JSX 子节点位置的大括号表达式（属性位置的 JsxExpression 由第 3 步处理）
    for (const jsxExpr of file.getDescendantsOfKind(SyntaxKind.JsxExpression)) {
      const parent = jsxExpr.getParent();
      if (parent && MorphNode.isJsxAttribute(parent)) continue;
      const expr = jsxExpr.getExpression();
      if (!expr) continue;
      candidates.push({ node: jsxExpr, handle: (c) => handleChildExpression(expr, c) });
    }

    // 3. 白名单属性
    for (const attr of file.getDescendantsOfKind(SyntaxKind.JsxAttribute)) {
      const name = attr.getNameNode().getText();
      if (!TEXT_ATTRS.has(name)) continue;
      candidates.push({ node: attr, handle: (c) => handleAttribute(attr, c) });
    }

    // 4. 对象字面量里的文本类键
    for (const obj of file.getDescendantsOfKind(SyntaxKind.ObjectLiteralExpression)) {
      for (const prop of obj.getProperties()) {
        if (!MorphNode.isPropertyAssignment(prop)) continue;
        const key = memberName(prop.getNameNode());
        if (key === null || !TEXT_OBJECT_KEYS.has(key)) continue;
        const init = prop.getInitializer();
        if (!init) continue;
        const literal = stringLiteralValue(init);
        if (literal === null) continue;
        if (!isLikelyUiText(literal, "object")) continue;
        candidates.push({
          node: prop,
          handle: (c) =>
            c.add({
              text: literal.trim(),
              kind: "object",
              via: key,
              loc: locationOf(prop.getNameNode()),
            }),
        });
      }
    }

    // 5. 字符串数组字面量（下拉选项、标签列表的简写形式）
    for (const arr of file.getDescendantsOfKind(SyntaxKind.ArrayLiteralExpression)) {
      const elements = arr.getElements();
      if (elements.length < 2) continue;
      const literals: { node: Node; value: string }[] = [];
      for (const element of elements) {
        const value = stringLiteralValue(element);
        if (value === null) break;
        literals.push({ node: element, value });
      }
      // 含非字面量项时整组不提取：配置表里混着变量说明它不是纯文案表
      if (literals.length !== elements.length) continue;
      if (!literals.every((item) => isLikelyUiText(item.value, "array"))) continue;

      candidates.push({
        node: arr,
        handle: (c) => {
          for (const item of literals) {
            c.add({
              text: item.value.trim(),
              kind: "array",
              via: null,
              loc: locationOf(item.node),
            });
          }
        },
      });
    }

    for (const candidate of candidates) {
      const collector = collectorFor(candidate.node);
      if (!collector) {
        unassigned += 1;
        continue;
      }
      candidate.handle(collector);
    }
  }

  const result: Map<string, ComponentText> = new Map();
  for (const [componentId, collector] of byComponent) {
    if (collector.texts.length === 0 && collector.dynamic.length === 0) continue;
    result.set(componentId, {
      texts: collector.texts,
      dynamic: collector.dynamic,
      truncated: collector.truncated,
    });
  }

  for (const [componentId, text] of result) {
    if (text.truncated === 0) continue;
    warnings.push({
      code: "ui_text_truncated",
      severity: "info",
      stage: "static",
      message:
        `组件 ${componentId} 超过 ${MAX_TEXTS_PER_COMPONENT} 条文案上限，` +
        `另有 ${text.truncated} 条未记录；超出部分不影响分析结果，但检索时可能漏命中`,
    });
  }

  if (unassigned > 0) {
    warnings.push({
      code: "ui_text_unassigned",
      severity: "info",
      stage: "static",
      message:
        `有 ${unassigned} 处文案位于组件之外（模块级常量或普通函数内），未归属到任何组件。` +
        "常见于写在组件外的 columns / options 常量；本期不做跨作用域求值，因此这些文案不会出现在节点 attrs 中。",
    });
  }

  return { byComponent: result, warnings };
}
