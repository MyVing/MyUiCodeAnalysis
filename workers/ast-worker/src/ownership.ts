/**
 * AST 节点归属：判断一段代码属于哪个组件。
 *
 * renders 与 hooks 两个分析器都要回答同一个问题——"这个 JSX 标签 / hook 调用
 * 在谁的函数体里"。两处各写一份迟早会分叉，导致同一段代码被算到不同组件上。
 * 这里只做一件事：沿父链向上找最近的、位置命中组件定义的祖先节点。
 *
 * 为什么用「定义位置键」而不是名字：
 *  - `Foo` 在 A 文件与 B 文件里可以指向完全不同的组件；
 *  - 一行里可以有多个声明（`const A = () => 1, B = () => 2`），行号会撞在一起。
 * 字符偏移 + 文件路径才能唯一标识一个声明。
 */

import type { Node } from "ts-morph";
import { Node as MorphNode } from "ts-morph";

import type { DiscoveredComponent } from "./components.js";
import { toProjectRelative } from "./paths.js";
import { positionKey } from "./positions.js";

/**
 * 建「定义位置键 → 组件 id」索引，键为 file@charOffset。
 */
export function buildOwnerIndex(
  components: readonly DiscoveredComponent[],
  declarations: ReadonlyMap<string, Node>,
): ReadonlyMap<string, string> {
  const index = new Map<string, string>();
  for (const component of components) {
    const decl = declarations.get(component.id);
    if (!decl) continue;
    index.set(positionKey(component.file, decl.getStart()), component.id);
  }
  return index;
}

/**
 * 向上找最近的、属于某个组件的祖先节点，返回组件 id；不在任何组件内返回 null。
 *
 * 从父节点而不是自身开始：被查询的节点本身（JSX 标签、调用表达式）不是组件声明。
 *
 * 遇到 SourceFile 仍未命中就停止：模块顶层的代码不归任何组件（如 `fetchUsers`
 * 这类文件级函数），不能因为"文件里有组件"就把它算进去。
 */
export function findOwner(
  node: Node,
  ownerIndex: ReadonlyMap<string, string>,
  rootPath: string,
): string | null {
  let current: Node | undefined = node.getParent() ?? undefined;
  while (current) {
    if (MorphNode.isSourceFile(current)) return null;

    const relativeFile = toProjectRelative(rootPath, current.getSourceFile().getFilePath());
    if (relativeFile !== null) {
      const hit = ownerIndex.get(positionKey(relativeFile, current.getStart()));
      if (hit) return hit;
    }
    current = current.getParent() ?? undefined;
  }
  return null;
}
