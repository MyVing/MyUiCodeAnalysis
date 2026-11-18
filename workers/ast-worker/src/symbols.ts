/**
 * 符号解析：把「代码里写的名字」还原成「项目里的哪个声明」。
 *
 * ## 为什么不能只按名字查表
 *
 * `events.ts` 最初用一张「同文件名字表」解析事件处理函数，代价是
 * `<Button onClick={() => saveOrder(form)}>` 里 import 进来的 `saveOrder`
 * 永远命中不了——它在另一个文件。结果不只是"少一条边"：`api.ts` 的
 * `findOwner` 会把请求记到组件名下，图上呈现成「页面加载时请求」，
 * 而事实是「点击才请求」。这是语义错误，不是精度损失。
 *
 * ## 为什么用定义位置而不是名字
 *
 * 与 `positions.ts` 同一条理由：`saveOrder` 在 A 文件和 B 文件里可以是两个
 * 完全无关的函数；名字相同的组件与工具函数也不该混为一谈。
 *
 * ## 只对标识符生效
 *
 * `obj.method()` 这类方法调用不解析：接收者指向哪个模块的对象，静态判定
 * 不了。这与 `api.ts` 的既有取舍一致，避免两处口径分叉。
 */

import type { Node, SourceFile } from "ts-morph";
import { Node as MorphNode, SyntaxKind } from "ts-morph";

import { toProjectRelative } from "./paths.js";

/**
 * 解析标识符指向的声明，穿透 import 别名。
 *
 * 调用点的符号通常指向 `import { saveOrder } from "@/services/order"` 里的
 * `ImportSpecifier`，它所在文件是**调用方**。不穿透就会在调用方文件里找
 * 一个不存在的声明，跨文件永远解析不到。
 */
export function resolveIdentifierDeclarations(node: Node): Node[] {
  if (!MorphNode.isIdentifier(node)) return [];

  let symbol = node.getSymbol();
  if (symbol?.isAlias()) {
    try {
      symbol = symbol.getAliasedSymbol();
    } catch {
      // 别名链断裂（如未解析的模块）时按「解析不到」处理，不猜
      return [];
    }
  }
  return symbol?.getDeclarations() ?? [];
}

/**
 * 从声明节点取出可执行体：函数声明本身，或变量声明里的箭头 / 函数表达式。
 *
 * 变量声明要回溯一层：`const saveOrder = (id) => { ... }` 的符号声明是
 * `VariableDeclaration`，函数体在它的初始化表达式里。
 */
export function functionNodeOf(decl: Node): Node | null {
  if (
    MorphNode.isFunctionDeclaration(decl) ||
    MorphNode.isFunctionExpression(decl) ||
    MorphNode.isArrowFunction(decl)
  ) {
    return decl;
  }

  if (MorphNode.isVariableDeclaration(decl)) {
    const init = decl.getInitializer();
    if (
      init &&
      (MorphNode.isArrowFunction(init) || MorphNode.isFunctionExpression(init))
    ) {
      return init;
    }
  }

  return null;
}

/** 函数节点的名字；匿名函数返回 null（匿名函数无法在图上稳定命名）。 */
export function functionNameOf(fn: Node): string | null {
  if (MorphNode.isFunctionDeclaration(fn)) return fn.getName() ?? null;

  const parent = fn.getParent();
  if (parent && MorphNode.isVariableDeclaration(parent)) return parent.getName();
  if (parent && MorphNode.isPropertyAssignment(parent)) return parent.getName();
  return null;
}

/** 函数在项目内的身份：id 规则与 `api.ts` 的 fn 节点一致（`文件#名字`）。 */
export interface FunctionIdentity {
  id: string;
  name: string;
  file: string;
  loc: { line: number; col: number };
  /** 函数体节点；补产 IR 节点后仍需它对声明位置建索引 */
  fn: Node;
  sourceFile: SourceFile;
}

/**
 * 把函数节点转成项目内身份；不在项目源码内、或是匿名函数时返回 null。
 *
 * 匿名函数不返回：`useEffect(() => {...})` 这类回调没有稳定名字，给它编一个
 * 序号 id 只会在源文件调整时整体漂移。
 */
export function functionIdentity(fn: Node, rootPath: string): FunctionIdentity | null {
  const name = functionNameOf(fn);
  if (!name) return null;

  const sourceFile = fn.getSourceFile();
  const relativeFile = toProjectRelative(rootPath, sourceFile.getFilePath());
  if (relativeFile === null) return null;

  const pos = sourceFile.getLineAndColumnAtPos(fn.getStart());
  return {
    id: `${relativeFile}#${name}`,
    name,
    file: relativeFile,
    loc: { line: pos.line, col: pos.column },
    fn,
    sourceFile,
  };
}

/** 项目源码内的全部「可具名函数」节点，按文档顺序。 */
export function collectProjectFunctions(
  project: { getSourceFiles(): readonly SourceFile[] },
  rootPath: string,
): Node[] {
  const fns: Node[] = [];

  for (const file of project.getSourceFiles()) {
    if (toProjectRelative(rootPath, file.getFilePath()) === null) continue;

    fns.push(...file.getFunctions());

    for (const decl of file.getDescendantsOfKind(SyntaxKind.VariableDeclaration)) {
      const init = decl.getInitializer();
      if (init && (MorphNode.isArrowFunction(init) || MorphNode.isFunctionExpression(init))) {
        fns.push(init);
      }
    }
  }

  return fns;
}
