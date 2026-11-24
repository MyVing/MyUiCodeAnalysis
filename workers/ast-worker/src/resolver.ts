/**
 * Module Resolver：把源码里的模块说明符解析成「项目内 / 外部依赖 / 无法解析」三态。
 *
 * 设计要点（依据对样本仓的实测，见 tests/resolver.test.ts）：
 *
 * 1. **不自己实现 paths 匹配**。读入 tsconfig 后 ts-morph 已按项目真实配置解析
 *    `@/*` 等别名，自己再写一遍 glob 匹配只会在 monorepo / extends / 多 baseUrl
 *    场景下与 TypeScript 的真实语义分叉。这里只消费 `getModuleSpecifierSourceFile()`。
 *
 * 2. **无法解析不等于失败**。必须区分两种「解析不到」：
 *    - 相对路径或别名前缀解析不到 → 项目内部断链，属于真实缺陷，报 error
 *    - 裸包名（antd / lodash）解析不到 → 依赖未安装，属环境事实，报 info
 *    把两者混为一谈会让「依赖没装」淹没「代码写错」。
 *
 * 3. **barrel 必须穿透**。`getExportedDeclarations()` 会把 `export * from` 展开到
 *    最终定义位置，天然去重。组件 id 必须基于最终定义位置生成，否则同一组件经
 *    barrel 再导出会变成两个节点。
 *
 * 本模块只回答「这个说明符指向哪里」，不判断它是不是组件——那是 components.ts 的事。
 */

import { Node as MorphNode, SyntaxKind, ts, type CallExpression, type Project, type SourceFile } from "ts-morph";

import type { Warning } from "@uicodeanalysis/contracts";

import {
  getAliasPrefixes,
  getPackageName,
  isAssetSpecifier,
  isLocalSpecifier,
  isNodeBuiltin,
  isProjectSource,
  toPosix,
  toProjectRelative,
} from "./paths.js";

/** 单个模块说明符的解析结果。 */
export type Resolution =
  | {
      status: "internal";
      /** 解析到的源文件绝对路径 */
      filePath: string;
      sourceFile: SourceFile;
    }
  | {
      status: "external";
      /** 裸包名，如 antd / @scope/pkg */
      packageName: string;
      /** 依赖已安装且有类型声明时，指向其 .d.ts */
      declarationFile?: string;
    }
  | {
      /** Node 内置模块（`path` / `node:fs`）：运行时提供，既没装也不需要装 */
      status: "builtin";
      packageName: string;
    }
  | {
      /** 非脚本资源（`.css` / `.png` 等）：由打包器处理，不是 TS 模块 */
      status: "asset";
      moduleSpecifier: string;
    }
  | {
      status: "unresolved";
      moduleSpecifier: string;
      /** true 表示这是本地说明符（相对路径/别名），属于真实断链 */
      isLocal: boolean;
    };

/** 逐说明符的解析索引，按源文件组织，供组件发现与后续图分析复用。 */
export interface ResolutionIndex {
  /** 解析某个说明符；同一 (文件, 说明符) 结果恒定，内部缓存 */
  resolve(fromFile: SourceFile, moduleSpecifier: string): Resolution;
  /** 该文件所有说明符（import + export from）的解析结果 */
  forFile(file: SourceFile): Map<string, Resolution>;
  /** 别名前缀，供其他模块判断「本地说明符」时复用同一套规则 */
  aliasPrefixes: readonly string[];
}

export interface ResolutionIndexResult {
  index: ResolutionIndex;
  warnings: Warning[];
}

/**
 * 构造解析索引，并一次性收齐解析失败相关的告警。
 *
 * @param rootPath 项目根目录（绝对路径），用于把告警里的文件转成相对路径
 * @param project  已完成源文件加载的 ts-morph Project
 */
export function createResolutionIndex(rootPath: string, project: Project): ResolutionIndexResult {
  const aliasPrefixes = getAliasPrefixes(project.getCompilerOptions().paths);
  const warnings: Warning[] = [];
  const reports = new Map<string, Warning>();
  const cache = new Map<SourceFile, Map<string, Resolution>>();

  function resolveOne(fromFile: SourceFile, moduleSpecifier: string): Resolution {
    // ts-morph 的 getModuleSpecifierSourceFile() 在 allowJs 打开后仍会对纯 .js 目标返回
    // undefined；用项目自身的 compilerOptions 走 TypeScript 原生解析兜底，两边结果同源。
    const target =
      findTargetSourceFile(fromFile, moduleSpecifier) ??
      findTargetSourceFileViaCompiler(fromFile, moduleSpecifier);
    const isLocal = isLocalSpecifier(moduleSpecifier, aliasPrefixes);

    if (target) {
      if (isProjectSource(rootPath, target.getFilePath())) {
        return { status: "internal", filePath: target.getFilePath(), sourceFile: target };
      }
      // 解析到了 node_modules 里的 .d.ts：外部依赖且类型可用
      const packageName = getPackageName(moduleSpecifier) ?? moduleSpecifier;
      return { status: "external", packageName, declarationFile: target.getFilePath() };
    }

    // 资源导入与 Node 内置模块在 TS 里必然解析不到，但它们不是缺陷。
    // 必须放在 target 判定之后：装了类型声明的内置模块（如 @types/node 的 path）
    // 是能解析到的，那种情况下按外部依赖处理更准确。
    if (isAssetSpecifier(moduleSpecifier)) {
      return { status: "asset", moduleSpecifier };
    }
    if (isNodeBuiltin(moduleSpecifier)) {
      return { status: "builtin", packageName: getPackageName(moduleSpecifier) ?? moduleSpecifier };
    }

    return { status: "unresolved", moduleSpecifier, isLocal };
  }

  const index: ResolutionIndex = {
    aliasPrefixes,
    resolve(fromFile, moduleSpecifier) {
      let perFile = cache.get(fromFile);
      if (!perFile) {
        perFile = new Map();
        cache.set(fromFile, perFile);
      }
      const cached = perFile.get(moduleSpecifier);
      if (cached) return cached;

      const resolution = resolveOne(fromFile, moduleSpecifier);
      perFile.set(moduleSpecifier, resolution);
      return resolution;
    },
    forFile(file) {
      const result = new Map<string, Resolution>();
      for (const moduleSpecifier of collectModuleSpecifiers(file)) {
        result.set(moduleSpecifier, index.resolve(file, moduleSpecifier));
      }
      return result;
    },
  };

  // 预跑全量解析：告警必须一次性收齐。同一文件同一说明符只报一次。
  for (const file of project.getSourceFiles()) {
    const relativeFile = toProjectRelative(rootPath, file.getFilePath());
    // 外部 .d.ts 不需要解析其自身 import，也不应产生告警
    if (relativeFile === null) continue;

    for (const [moduleSpecifier, resolution] of index.forFile(file)) {
      if (resolution.status !== "unresolved") continue;

      const key = `${relativeFile}::${moduleSpecifier}`;
      if (reports.has(key)) continue;

      const warning: Warning =
        resolution.isLocal
          ? {
              code: "unresolved_import",
              severity: "error",
              stage: "static",
              file: relativeFile,
              message: `无法解析本地模块 "${moduleSpecifier}"：路径别名或相对路径指向的文件不存在`,
            }
          : {
              code: "dependency_not_installed",
              severity: "info",
              stage: "static",
              file: relativeFile,
              message: `外部依赖 "${moduleSpecifier}" 未安装或缺少类型声明；其内部实现不在静态分析范围内`,
            };
      reports.set(key, warning);
    }
  }

  warnings.push(...reports.values());
  return { index, warnings };
}

/** 收集一个文件里所有需要解析的模块说明符（import + export from + 动态 import）。 */
function collectModuleSpecifiers(file: SourceFile): string[] {
  const specifiers = new Set<string>();
  for (const imp of file.getImportDeclarations()) {
    specifiers.add(imp.getModuleSpecifierValue());
  }
  for (const exp of file.getExportDeclarations()) {
    const value = exp.getModuleSpecifierValue();
    if (value) specifiers.add(value);
  }
  for (const call of file.getDescendantsOfKind(SyntaxKind.CallExpression)) {
    if (call.getExpression().getKind() !== SyntaxKind.ImportKeyword) continue;
    const argument = call.getArguments()[0];
    if (argument && MorphNode.isStringLiteral(argument)) {
      specifiers.add(argument.getLiteralValue());
    }
  }
  return [...specifiers];
}

/**
 * 找出说明符对应的源文件。
 *
 * 复用 ts-morph 在各自 AST 节点上已完成的解析结果，不自己实现模块解析——
 * 这正是「不得退化为按名称匹配」的落点。
 */
function findTargetSourceFile(
  fromFile: SourceFile,
  moduleSpecifier: string,
): SourceFile | undefined {
  for (const imp of fromFile.getImportDeclarations()) {
    if (imp.getModuleSpecifierValue() === moduleSpecifier) {
      return imp.getModuleSpecifierSourceFile();
    }
  }
  for (const exp of fromFile.getExportDeclarations()) {
    if (exp.getModuleSpecifierValue() === moduleSpecifier) {
      return exp.getModuleSpecifierSourceFile();
    }
  }
  for (const call of fromFile.getDescendantsOfKind(SyntaxKind.CallExpression)) {
    if (call.getExpression().getKind() !== SyntaxKind.ImportKeyword) continue;
    const argument = call.getArguments()[0];
    if (argument && MorphNode.isStringLiteral(argument) && argument.getLiteralValue() === moduleSpecifier) {
      return projectSourceFileForDynamicImport(call);
    }
  }
  return undefined;
}

/** 动态 import 的源文件由 TypeScript 的模块解析结果提供。 */
function projectSourceFileForDynamicImport(call: CallExpression): SourceFile | undefined {
  const argument = call.getArguments()[0];
  if (!argument || !MorphNode.isStringLiteral(argument)) return undefined;
  return findTargetSourceFileViaCompiler(call.getSourceFile(), argument.getLiteralValue());
}

/**
 * 用 **项目当前的 compilerOptions**（含 moduleResolution / paths / baseUrl / allowJs）
 * 走 TypeScript 原生模块解析，再映射回 Project 里已加载的 SourceFile。
 *
 * 这是静态与动态 import 共用的兜底：不允许另造一套默认选项，否则会与 tsconfig 分叉。
 */
function findTargetSourceFileViaCompiler(
  fromFile: SourceFile,
  moduleSpecifier: string,
): SourceFile | undefined {
  const resolvedFileName = resolveModulePath(fromFile, moduleSpecifier);
  if (!resolvedFileName) return undefined;
  return fromFile.getProject().getSourceFile(resolvedFileName);
}

/** 只做路径解析，返回 TypeScript 给出的绝对文件名（可能不在 Project 内）。 */
function resolveModulePath(fromFile: SourceFile, moduleSpecifier: string): string | undefined {
  return ts.resolveModuleName(
    moduleSpecifier,
    fromFile.getFilePath(),
    fromFile.getProject().getCompilerOptions(),
    ts.sys,
  ).resolvedModule?.resolvedFileName;
}

/** 便于测试与调试：把解析结果格式化成一行。 */
export function formatResolution(resolution: Resolution, rootPath: string): string {
  switch (resolution.status) {
    case "internal":
      return `internal:${toProjectRelative(rootPath, resolution.filePath) ?? toPosix(resolution.filePath)}`;
    case "external":
      return `external:${resolution.packageName}`;
    case "builtin":
      return `builtin:${resolution.packageName}`;
    case "asset":
      return `asset:${resolution.moduleSpecifier}`;
    case "unresolved":
      return `unresolved:${resolution.moduleSpecifier}`;
  }
}
