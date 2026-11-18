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
 *
 * solution-style tsconfig（`files: []` + `references`，create-vite 等脚手架的默认）：
 * 根配置自身不含任何源文件，直接交给 ts-morph 会得到 0 个 SourceFile，后续所有
 * 分析器都无源码可查，最终产出一张"成功但为空"的图 —— 界面上表现为点了分析没反应。
 * 因此这里会顺着 references 找到真正装载源文件的子配置，并额外补上其他引用配置
 * 的源文件（monorepo 里 app 与 ui 包分别被引用时不能只分析其中一个）。
 *
 * 退化路径必须可观测：拿不到 tsconfig 的文件列表时退回 glob，但仍产告警；
 * 一个源文件都找不到时也产告警，不允许再出现静默的空图。
 */

import { existsSync } from "node:fs";
import path from "node:path";

import type { Warning } from "@uicodeanalysis/contracts";
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

/** 全量分析的构建结果。告警随 Project 一起返回，由入口并入 IRGraph。 */
export interface FullProjectResult {
  project: Project;
  /** 是否从 tsconfig 装载；false 表示 glob 兜底，paths 别名不可用 */
  fromTsConfig: boolean;
  /** 实际装载源文件所用的 tsconfig；glob 兜底时为 null */
  tsConfigPath: string | null;
  /** 配置层诊断：no_tsconfig / solution_tsconfig / tsconfig_no_files / empty_project */
  warnings: Warning[];
}

interface ConfigCandidate {
  configPath: string;
  parsed: ts.ParsedCommandLine;
}

function staticWarning(code: string, message: string): Warning {
  return { code, severity: "warning", stage: "static", message };
}

/** 读 tsconfig；配置损坏时返回 null，由调用方退化为目录扫描。 */
function parseConfig(configPath: string): ts.ParsedCommandLine | null {
  try {
    return (
      ts.getParsedCommandLineOfConfigFile(configPath, {}, {
        ...ts.sys,
        onUnRecoverableConfigFileDiagnostic: () => {
          // 配置文件不可恢复地损坏：退化为目录扫描，缺口由调用方告警可见
        },
      }) ?? null
    );
  } catch {
    return null;
  }
}

/** references 里的 path 可能是目录、可能省略 .json；统一解析到真实文件。 */
function resolveReferencePath(configPath: string, reference: ts.ProjectReference): string | null {
  const target = path.resolve(path.dirname(configPath), reference.path);
  if (existsSync(target) && !target.toLowerCase().endsWith(".json")) {
    const nested = path.join(target, "tsconfig.json");
    return existsSync(nested) ? nested : null;
  }
  if (existsSync(target)) {
    return target;
  }
  return existsSync(`${target}.json`) ? `${target}.json` : null;
}

/** JSX 源文件数：决定哪份子配置是"应用主体"，它的 paths 用于别名解析。 */
function jsxSourceCount(parsed: ts.ParsedCommandLine): number {
  return parsed.fileNames.filter((file) => /\.(tsx|jsx)$/i.test(file)).length;
}

/**
 * 收集候选配置：普通项目只有根配置；solution-style 项目顺着 references
 * 收集全部子配置。只保留确实装载到源文件的那些。
 */
function loadConfigCandidates(rootPath: string): {
  rootConfigPath: string;
  solutionStyle: boolean;
  candidates: ConfigCandidate[];
} {
  const rootConfigPath = path.join(rootPath, "tsconfig.json");
  const rootParsed = parseConfig(rootConfigPath);

  const referenced = (rootParsed?.projectReferences ?? [])
    .map((reference) => resolveReferencePath(rootConfigPath, reference))
    .filter((configPath): configPath is string => configPath !== null);

  // solution-style：根配置自身不含文件，真正的内容由 references 指向
  const solutionStyle = (rootParsed?.fileNames.length ?? 0) === 0 && referenced.length > 0;
  const configPaths = solutionStyle ? referenced : [rootConfigPath];

  const candidates: ConfigCandidate[] = [];
  for (const configPath of configPaths) {
    const parsed = configPath === rootConfigPath ? rootParsed : parseConfig(configPath);
    if (parsed && parsed.fileNames.length > 0) {
      candidates.push({ configPath, parsed });
    }
  }

  // 主配置排第一：JSX 源文件最多者优先，其次文件总数最多者
  candidates.sort(
    (left, right) =>
      jsxSourceCount(right.parsed) - jsxSourceCount(left.parsed) ||
      right.parsed.fileNames.length - left.parsed.fileNames.length,
  );

  return { rootConfigPath, solutionStyle, candidates };
}

/** glob 兜底：没有可用 tsconfig 时仍要尽力扫出源文件。 */
function createGlobProject(rootPath: string, warnings: Warning[]): FullProjectResult {
  const project = new Project({
    skipAddingFilesFromTsConfig: true,
    compilerOptions: { ...FALLBACK_COMPILER_OPTIONS },
  });
  const root = rootPath.replace(/\\/g, "/").replace(/\/$/, "");
  project.addSourceFilesAtPaths([
    `${root}/**/*.{ts,tsx,js,jsx}`,
    ...EXCLUDED_DIRS.map((dir) => `!${root}/**/${dir}/**`),
  ]);

  if (project.getSourceFiles().length === 0) {
    warnings.push(
      staticWarning(
        "empty_project",
        `项目根目录未找到任何 .ts/.tsx/.js/.jsx 源文件：${rootPath}。本次分析产物为空图。`,
      ),
    );
  }
  return { project, fromTsConfig: false, tsConfigPath: null, warnings };
}

/**
 * 创建全量分析用的 Project。
 *
 * 优先使用项目自身的 tsconfig —— 这是正确解析 `@/*` 路径别名与 monorepo
 * workspace 包的唯一可靠来源，禁止退化为按名称匹配。solution-style 配置
 * 会改用 references 里装载源文件最多的子配置。
 */
export function createFullProject(rootPath: string): FullProjectResult {
  const warnings: Warning[] = [];
  const rootConfigPath = path.join(rootPath, "tsconfig.json");

  if (!existsSync(rootConfigPath)) {
    // 没有 tsconfig 就没有 paths，别名 import 必然全部解析失败。
    // 这不是"分析完成但没有结果"，必须让调用方看到能力缺口。
    warnings.push(
      staticWarning(
        "no_tsconfig",
        "未找到 tsconfig.json，路径别名（paths）无法解析，跨目录别名 import 将被记为未解析；组件发现仍按相对路径工作。",
      ),
    );
    return createGlobProject(rootPath, warnings);
  }

  const { solutionStyle, candidates } = loadConfigCandidates(rootPath);
  const primary = candidates[0];

  if (primary === undefined) {
    warnings.push(
      staticWarning(
        "tsconfig_no_files",
        solutionStyle
          ? "tsconfig.json 是 solution-style 且 references 指向的配置也没有装载到源文件，已退化为目录扫描。"
          : "tsconfig.json 未装载到任何源文件（检查 include / files 是否指向存在的目录），已退化为目录扫描。",
      ),
    );
    return createGlobProject(rootPath, warnings);
  }

  let project: Project;
  try {
    project = new Project({
      tsConfigFilePath: primary.configPath,
      skipAddingFilesFromTsConfig: false,
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    warnings.push(
      staticWarning(
        "tsconfig_load_failed",
        `tsconfig 装载失败（${path.basename(primary.configPath)}）：${detail}；已退化为目录扫描，别名解析不可用。`,
      ),
    );
    return createGlobProject(rootPath, warnings);
  }

  if (project.getSourceFiles().length === 0) {
    warnings.push(
      staticWarning(
        "tsconfig_no_files",
        `tsconfig 记录的文件都无法加载（${path.basename(primary.configPath)}），已退化为目录扫描。`,
      ),
    );
    return createGlobProject(rootPath, warnings);
  }

  // solution-style 属于正确处理的常态（create-vite 等模板默认如此），不产告警；
  // 实际选用的配置见入口日志，避免每张真实图都挂一条常驻提示。


  // 其他引用配置的源文件（如 monorepo 的 ui 包）一并加入：只分析主配置会漏掉它们。
  // 别名解析仍以主配置的 paths 为准；跨配置别名不一致属于已知边界。
  const extraFiles = candidates.slice(1).flatMap((candidate) => candidate.parsed.fileNames);
  if (extraFiles.length > 0) {
    project.addSourceFilesAtPaths(extraFiles);
  }

  return { project, fromTsConfig: true, tsConfigPath: primary.configPath, warnings };
}

/**
 * 创建增量分析用的 Project，只包含变更文件。
 *
 * 刻意不声明 `changedFiles` 之外的源文件：ts-morph 在需要时会按 import 自动加载
 * 闭包内的依赖文件，因此不必手动枚举。但 paths 别名依然需要 tsconfig 提供，
 * 所以这里仍读取 tsconfig 的 compilerOptions，只跳过它的文件列表 —— 且必须是
 * solution-style 情况下真正装载源文件的子配置，否则别名规则会跟分析主体对不上。
 */
export function createIncrementalProject(rootPath: string, changedFiles: string[]): Project {
  const { candidates } = loadConfigCandidates(rootPath);
  const primary = candidates[0];
  const project =
    primary !== undefined
      ? new Project({ tsConfigFilePath: primary.configPath, skipAddingFilesFromTsConfig: true })
      : new Project({
          skipAddingFilesFromTsConfig: true,
          compilerOptions: { ...FALLBACK_COMPILER_OPTIONS },
        });

  project.addSourceFilesAtPaths(
    changedFiles.map((file) => path.resolve(rootPath, file).replace(/\\/g, "/")),
  );
  return project;
}
