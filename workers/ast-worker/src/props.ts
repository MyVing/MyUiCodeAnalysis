/**
 * props 分析：把组件的 props 类型展开成字段列表，供前端属性面板使用。
 *
 * 三条设计约束：
 *
 * 1. **可选性看问号，不看类型文本**。`showFilter?: boolean` 经类型检查变成
 *    `boolean | undefined`，若从类型字符串里找 `undefined` 会把
 *    `title: string | undefined`（显式可空）误判为可选。只有 `?` 才是
 *    "调用方可以省略这个属性"。
 *
 * 2. **类型文本保留源码写法，不做类型展开**。`rows: UserRowProps[]` 原样输出，
 *    不展开成 `{ userId: string; ... }[]`。展开后前端就无法显示用户自己起的类型名，
 *    而"这个 props 是哪个类型"恰恰是读代码时最需要的线索。
 *
 * 3. **默认值来自解构**。`{ showFilter = true }` 的 `true` 只有从组件签名里能拿到；
 *    类型系统不记录默认值。
 *
 * 找不到 props 类型（无参数、无类型标注）时返回空数组而不是 null：调用方可以
 * 用它区分"没有 props"和"props 分析还没做"。
 */

import type { Node, ParameterDeclaration, SourceFile, Type } from "ts-morph";
import { Node as MorphNode } from "ts-morph";

import type { Warning } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";
import { toProjectRelative } from "./paths.js";

/** 一个 props 字段。 */
export interface PropField {
  name: string;
  /** 类型文本，保留源码写法（如 `UserRowProps[]`、`(userId: string) => void`） */
  type: string;
  /** 是否带问号（调用方可省略） */
  optional: boolean;
  /** 解构默认值的源码文本；无默认值为 null */
  defaultValue: string | null;
  /** JSDoc 首行注释，用于属性面板说明 */
  description: string | null;
  /** 字段声明位置；来自内联字面量时仍指向源码 */
  loc: { line: number; col: number } | null;
  /** 字段声明的文件（相对项目根）；内联类型与接口可能不同文件 */
  file: string | null;
}

/** 单个组件的 props 分析结果。 */
export interface ComponentProps {
  componentId: string;
  /** props 类型名；内联字面量或未标注时为 null */
  typeName: string | null;
  /** props 类型的来源文件（相对项目根）；未标注时为 null */
  typeFile: string | null;
  fields: PropField[];
  /** 是否展开了泛型参数（`ProTableProps<T>` 这类） */
  generic: boolean;
}

export interface PropsAnalysisResult {
  /** 组件 id → props 分析结果 */
  byComponent: Map<string, ComponentProps>;
  warnings: Warning[];
}

export interface PropsAnalysisInput {
  rootPath: string;
  components: readonly DiscoveredComponent[];
  /** 组件 id → 定义 AST 节点 */
  declarations: ReadonlyMap<string, Node>;
}

/** 从 JSDoc 里取首行有效注释。 */
function firstJsDocLine(node: Node): string | null {
  if (!MorphNode.isJSDocable(node)) return null;
  for (const doc of node.getJsDocs()) {
    const text = doc.getCommentText()?.trim();
    if (text) return text.split("\n")[0]?.trim() ?? null;
  }
  return null;
}

/**
 * 取字段类型文本。
 *
 * 可选属性（`a?: T`）在类型系统里是 `T | undefined`。这个 `undefined` 是 `?`
 * 的产物，用户并没有写；如果原样展示，属性面板里每个可选字段都挂着一个噪音，
 * 而且与真正手写 `a: T | undefined` 的语义混在一起。
 *
 * 因此**优先用源码里的类型节点文本**（`boolean`、`() => Promise<{...}>`），
 * 它天然不含 `?` 带来的 `undefined`，也保留用户写的 `UserRowProps[]` 这类命名。
 * 只有当属性没有类型标注（`a?` 或从上下文推断）时才退回类型系统——但那时
 * 不能再做成员过滤，因为 union 成员会退化成 `false | true` 这种字面量形式。
 */
function formatFieldType(
  type: Type,
  typeNodeText: string | null,
  context: Node,
): string {
  if (typeNodeText !== null) return typeNodeText;
  return type.getText(context);
}

/**
 * 取解构默认值。
 *
 * 只有 `{ a = 1, b: alias = 2 }` 这种解构绑定带默认值。直接从参数节点的
 * 绑定模式里读，比从类型系统反推可靠——类型上根本不存在默认值这一概念。
 */
function collectDefaults(propsParam: ParameterDeclaration | undefined): Map<string, string> {
  const defaults = new Map<string, string>();
  if (!propsParam) return defaults;

  const nameNode = propsParam.getNameNode();
  if (!MorphNode.isObjectBindingPattern(nameNode)) return defaults;

  for (const element of nameNode.getElements()) {
    const initializer = element.getInitializer();
    if (!initializer) continue;

    // `{ request: loadData = fn }`：对外暴露的属性名是 `request`，不是 `loadData`
    const propertyName = element.getPropertyNameNode()?.getText() ?? element.getName();
    defaults.set(propertyName, initializer.getText());
  }
  return defaults;
}

/** 取组件声明上的 props 参数节点。 */
function getPropsParameter(decl: Node): ParameterDeclaration | undefined {
  if (
    MorphNode.isFunctionDeclaration(decl) ||
    MorphNode.isFunctionExpression(decl) ||
    MorphNode.isArrowFunction(decl)
  ) {
    return decl.getParameters()[0];
  }
  return undefined;
}

/**
 * 展开一个组件的 props。
 *
 * 类型来源分三种，都用同一套属性读取逻辑，区别只在"类型名"与"来源文件"：
 *  - 具名接口/类型别名：`UserListProps` → 类型名与定义文件可回溯
 *  - 内联类型字面量：`{ total: number }` → 无类型名，但字段仍可展开
 *  - 无参数/无标注：无字段
 */
function analyzeComponentProps(
  component: DiscoveredComponent,
  decl: Node,
  rootPath: string,
): ComponentProps {
  const propsParam = getPropsParameter(decl);
  const defaults = collectDefaults(propsParam);

  if (!propsParam) {
    return {
      componentId: component.id,
      typeName: null,
      typeFile: null,
      fields: [],
      generic: false,
    };
  }

  const propsType = propsParam.getType();
  const typeNode = propsParam.getTypeNode();

  // 类型名与来源文件：从类型节点反查，不走类型系统（后者对内联类型给出 __type）
  let typeName: string | null = null;
  let typeFile: string | null = null;
  let generic = false;

  if (typeNode && MorphNode.isTypeReference(typeNode)) {
    typeName = typeNode.getTypeName().getText();
    const typeArgs = typeNode.getTypeArguments();
    generic = typeArgs.length > 0;

    const symbol = propsType.getSymbol();
    for (const typeDecl of symbol?.getDeclarations() ?? []) {
      const relative = toProjectRelative(rootPath, typeDecl.getSourceFile().getFilePath());
      if (relative) {
        typeFile = relative;
        break;
      }
    }
  }

  const fields: PropField[] = [];
  for (const property of propsType.getProperties()) {
    const propertyDecl = property.getDeclarations()[0];
    const sourceFile: SourceFile | undefined = propertyDecl?.getSourceFile();
    const relativeFile =
      sourceFile === undefined ? null : toProjectRelative(rootPath, sourceFile.getFilePath());
    const pos =
      sourceFile && propertyDecl
        ? sourceFile.getLineAndColumnAtPos(propertyDecl.getStart())
        : null;

    // 只要字面量属性声明（PropertySignature），不把方法签名当字段
    const isPropertySignature =
      propertyDecl !== undefined && MorphNode.isPropertySignature(propertyDecl);
    const optional = isPropertySignature && propertyDecl.hasQuestionToken();

    // 属性符号本身不带类型，必须问"这个符号在声明位置上的类型"
    const propertyType = propertyDecl
      ? property.getTypeAtLocation(propertyDecl)
      : property.getTypeAtLocation(propsParam);

    // 源码里写了类型标注就原样用：既保留命名类型，也不掺入 `?` 引入的 undefined
    const typeNodeText =
      propertyDecl && MorphNode.isPropertySignature(propertyDecl)
        ? (propertyDecl.getTypeNode()?.getText() ?? null)
        : null;

    fields.push({
      name: property.getName(),
      type: formatFieldType(propertyType, typeNodeText, propertyDecl ?? propsParam),
      // 可选性只看问号；类型的 `| undefined` 是另一回事
      optional,
      defaultValue: defaults.get(property.getName()) ?? null,
      description: propertyDecl ? firstJsDocLine(propertyDecl) : null,
      loc: pos ? { line: pos.line, col: pos.column } : null,
      file: relativeFile,
    });
  }

  return { componentId: component.id, typeName, typeFile, fields, generic };
}

/**
 * 分析全部组件的 props。
 *
 * 同一 props 类型被多个组件复用时各自展开一次，不做跨组件合并——
 * 每个组件的属性面板需要独立的默认值（默认值来自各自的解构签名）。
 */
export function analyzeProps(input: PropsAnalysisInput): PropsAnalysisResult {
  const { rootPath, components, declarations } = input;

  const byComponent = new Map<string, ComponentProps>();
  const warnings: Warning[] = [];

  for (const component of components) {
    const decl = declarations.get(component.id);
    if (!decl) {
      // 组件没有声明节点属于内部不一致；记下来但继续分析其他组件
      warnings.push({
        code: "props_declaration_missing",
        severity: "warning",
        stage: "static",
        file: component.file,
        loc: component.loc,
        message: `组件 ${component.name} 缺少声明节点，跳过 props 分析`,
      });
      continue;
    }

    try {
      byComponent.set(component.id, analyzeComponentProps(component, decl, rootPath));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      warnings.push({
        code: "props_analysis_failed",
        severity: "warning",
        stage: "static",
        file: component.file,
        loc: component.loc,
        message: `展开 ${component.name} 的 props 失败：${message}`,
      });
    }
  }

  return { byComponent, warnings };
}
