/**
 * ts-morph Project 的构建策略。
 *
 * 两条路径必须在编译器选项层面分开，不能混用：
 *
 *  - 全量路径：读项目自带 tsconfig.json，复用其中的 paths / baseUrl / strict 等配置，
 *    跨文件类型解析走完整 type checker。用于首次分析。
 *
 *  - 增量路径：跳过 tsconfig 的文件列表，只加入变更文件与其可达的 import 闭包。
 *    用于文件级增量重算。
 *
 * 关键约束（来自 ReactION 的实测结论）：
 * 单次分析内必须复用同一个 Project 实例。`getExportedDeclarations()` 会强制构建
 * 底层 ts.Program 并加载 lib.d.ts，实测单文件重建约 90ms/次；复用实例后约 1ms/次。
 * 因此本模块只暴露"创建一次"的工厂，不提供 per-file 的快捷方法。
 */

import { existsSync } from "node:fs";
import path from "node:path";

import { Project, ts } from "ts-morph";

/** 分析时始终排除的目录名，避免扫描依赖与构建产物。 */
export const EXCLUDED_DIRS = [
  "node_modules",
  "dist",
  "build",
  "out",
  "coverage",
  ".next",
  ".turbo",
] as const;

/** ts-morph 无法从 tsconfig 拿到的兜底编译选项。 */
const FALLBACK_COMPILER_OPTIONS = {
  allowJs: true,
  jsx: ts.JsxEmit.ReactJSX,
  // 显式开 skipLibCheck：被分析项目自身的 @types 冲突不应导致分析失败
  skipLibCheck: true,
} as const;

function hasTsConfig(rootPath: string): boolean {
  return existsSync(path.join(rootPath, "tsconfig.json"));
}

/**
 * 创建全量分析用的 Project。
 *
 * 优先使用项目自身的 tsconfig.json —— 这是正确解析 `@/*` 路径别名与
 * monorepo workspace 包的唯一可靠来源，禁止退化为按名称匹配。
 * 项目没有 tsconfig.json 时退回 glob 扫描，此时别名解析能力不可用，
 * 调用方会收到一条 warning。
 */
export function createFullProject(rootPath: string): { project: Project; fromTsConfig: boolean } {
  if (hasTsConfig(rootPath)) {
    return {
      project: new Project({
        tsConfigFilePath: path.join(rootPath, "tsconfig.json"),
        skipAddingFilesFromTsConfig: false,
      }),
      fromTsConfig: true,
    };
  }

  const project = new Project({
    skipAddingFilesFromTsConfig: true,
    compilerOptions: { ...FALLBACK_COMPILER_OPTIONS },
  });
  const root = rootPath.replace(/\\/g, "/").replace(/\/$/, "");
  project.addSourceFilesAtPaths([
    `${root}/**/*.{ts,tsx,js,jsx}`,
    ...EXCLUDED_DIRS.map((dir) => `!${root}/**/${dir}/**`),
  ]);
  return { project, fromTsConfig: false };
}

/**
 * 创建增量分析用的 Project，只包含变更文件。
 *
 * 刻意不声明 `changedFiles` 之外的源文件：ts-morph 在需要时会按 import 自动加载
 * 闭包内的依赖文件，因此不必手动枚举。但 paths 别名依然需要 tsconfig 提供，
 * 所以这里仍读取 tsconfig 的 compilerOptions，只跳过它的文件列表。
 */
export function createIncrementalProject(rootPath: string, changedFiles: string[]): Project {
  const tsConfigFilePath = path.join(rootPath, "tsconfig.json");
  const project = existsSync(tsConfigFilePath)
    ? new Project({ tsConfigFilePath, skipAddingFilesFromTsConfig: true })
    : new Project({
        skipAddingFilesFromTsConfig: true,
        compilerOptions: { ...FALLBACK_COMPILER_OPTIONS },
      });

  project.addSourceFilesAtPaths(
    changedFiles.map((file) => path.resolve(rootPath, file).replace(/\\/g, "/")),
  );
  return project;
}
