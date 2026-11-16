/**
 * 位置键：把「文件 + AST 节点起始偏移」压成可比较的字符串。
 *
 * 为什么不用组件名或导出名做键：`export function X` 与 `export default X` 指向
 * 同一节点但导出名不同；而 `Foo` 这个标签名在不同文件里可以指向完全不同的组件。
 * 只有「定义位置」能唯一标识一个声明。
 */

export function positionKey(relativeFile: string, start: number): string {
  return `${relativeFile}@${start}`;
}
