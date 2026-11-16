/**
 * 路径工具：把 ts-morph 的绝对路径收敛成「相对项目根目录」的统一形式。
 *
 * IR 契约要求 node.file / evidence.file 都是相对项目根目录的路径（兼容 Windows
 * 与 POSIX），而 ts-morph 一律返回当前平台的绝对路径。所有出图的路径都必须
 * 经过这里，禁止在业务代码里各自 replace。
 */

import path from "node:path";

/** 归一化为正斜杠，用于跨平台比较与写入 IR。 */
export function toPosix(value: string): string {
  return value.replace(/\\/g, "/");
}

/**
 * 把绝对路径转成相对项目根目录的形式（正斜杠）。
 * 不在项目内的路径（如 node_modules 里的 .d.ts）返回 null。
 */
export function toProjectRelative(rootPath: string, absolutePath: string): string | null {
  const relative = toPosix(path.relative(rootPath, absolutePath));
  if (relative.startsWith("..") || path.isAbsolute(relative)) return null;
  return relative;
}

/**
 * 判定一个绝对路径是否为「项目自身源码」，即分析器应当在其上产出组件与边的文件。
 *
 * 依据两条硬规则，不做名字猜测：
 *  - 位于 node_modules / .d.ts 中 → 外部依赖（外部库的组件由适配器提供知识，不由核心遍历）
 *  - 不在 rootPath 之下 → 外部
 */
export function isProjectSource(rootPath: string, absolutePath: string): boolean {
  const posix = toPosix(absolutePath);
  if (posix.includes("/node_modules/")) return false;
  if (posix.endsWith(".d.ts")) return false;
  return toProjectRelative(rootPath, absolutePath) !== null;
}

/**
 * 从模块说明符中取出裸包名，用于归类外部依赖。
 *
 *   "antd"                -> "antd"
 *   "antd/es/table"       -> "antd"
 *   "@scope/pkg/sub"      -> "@scope/pkg"
 *   "./local" / "@/alias" -> null（不是裸包名）
 *   "node:fs"             -> "node:fs"
 */
export function getPackageName(moduleSpecifier: string): string | null {
  if (moduleSpecifier.startsWith(".") || moduleSpecifier.startsWith("/")) return null;
  if (moduleSpecifier.startsWith("node:")) return moduleSpecifier;

  const segments = moduleSpecifier.split("/");
  // 形如 @scope/name/subpath 时取前两段；形如 antd/es/table 时取第一段
  if (moduleSpecifier.startsWith("@")) {
    return segments.length >= 2 ? `${segments[0]}/${segments[1]}` : null;
  }
  return segments[0] ?? null;
}

/**
 * 判定一个模块说明符是否走别名/相对路径（即「应当解析到项目内」的说明符）。
 *
 * 用于区分两类「解析失败」：
 *  - 别名/相对路径解析不到 → 真实的 unresolved（必须告警）
 *  - 裸包名解析不到       → 未安装的外部依赖（external，通常是信息级告警）
 */
export function isLocalSpecifier(moduleSpecifier: string, aliasPrefixes: readonly string[]): boolean {
  if (moduleSpecifier.startsWith(".")) return true;
  if (moduleSpecifier.startsWith("/")) return true;
  return aliasPrefixes.some(
    (prefix) => moduleSpecifier === prefix || moduleSpecifier.startsWith(`${prefix}/`),
  );
}

/**
 * 从 tsconfig 的 paths 配置推导别名前缀（去掉末尾的 `*` 与 `/`）。
 *
 *   "@/*"          -> "@"
 *   "@components/*"-> "@components"
 *   "@"            -> "@"
 *
 * 返回的前缀用于 isLocalSpecifier，不含尾部分隔符。
 */
export function getAliasPrefixes(pathsConfig: Record<string, unknown> | undefined): string[] {
  if (!pathsConfig) return [];
  return Object.keys(pathsConfig).map((key) =>
    key.endsWith("*") ? key.slice(0, -1).replace(/\/+$/, "") : key,
  );
}
