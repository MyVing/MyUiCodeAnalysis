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
 * 根目录完全没有 tsconfig（文档站外壳 + 子应用、或子应用是唯一源码目录）：
 * 向下发现子目录配置，按"覆盖已扫描源文件数 → 目录深度 → JSX 数"排序选择，
 * 跳过 node_modules / 构建产物 / fixtures 等目录；选择结果与覆盖差异写入 warnings。
 *
 * 退化路径必须可观测：拿不到 tsconfig 的文件列表时退回 glob，但仍产告警；
 * 一个源文件都找不到时也产告警，不允许再出现静默的空图。
 */

import { existsSync, readdirSync, type Dirent } from "node:fs";
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

/**
 * 根目录没有 tsconfig 时向下发现嵌套配置时额外跳过的目录。
 *
 * 这些目录里的 tsconfig 通常属于测试夹具、示例或历史子项目，不是本次分析
 * 的主体。按目录名跳过比按文件数量猜测更可解释；真正包含业务源码的子项目
 * 仍会参与候选排序。
 */
const NESTED_CONFIG_IGNORED_DIRS: ReadonlySet<string> = new Set([
  ...EXCLUDED_DIRS.map((dir) => dir.toLowerCase()),
  ".git",
  ".docusaurus",
  ".cache",
  "fixtures",
  "__fixtures__",
  "test-fixtures",
]);

const SOURCE_FILE_PATTERN = /\.(ts|tsx|js|jsx)$/i;

/** 与 sourceGlobs 排除的范围保持一致；用于统计“实际会扫描多少源文件”。 */
const SOURCE_IGNORED_DIRS: ReadonlySet<string> = new Set([
  ...EXCLUDED_DIRS.map((dir) => dir.toLowerCase()),
  ".git",
]);

/** ts-morph 无法从 tsconfig 拿到的兜底编译选项。 */
const FALLBACK_COMPILER_OPTIONS = {
  allowJs: true,
  jsx: ts.JsxEmit.ReactJSX,
  // 显式开 skipLibCheck：被分析项目自身的 @types 冲突不应导致分析失败
  skipLibCheck: true,
} as const;

/**
 * 确保分析用的 Project 开启 allowJs。
 *
 * 分析器会主动把 `.js` / `.jsx` 加入 Project（见 sourceGlobs），但 TypeScript
 * 只有在 allowJs 打开时才会把这些文件当作外部模块绑定 symbol。否则
 * `getExportedDeclarations()` 对它们返回空 Map —— 文件里的具名导出组件
 * 一个都进不了图，而且没有任何报错。
 *
 * 典型触发场景：项目 tsconfig `extends` 的基础配置未安装（如
 * `@docusaurus/tsconfig`），解析出的 compilerOptions 直接丢掉 allowJs。
 *
 * 项目配置已开启时不改写；未开启时补开，并在项目确实包含 JS 源文件时告警，
 * 让「分析器替你改了编译选项」这件事可见。
 */
function ensureAllowJs(project: Project, configLabel: string, configDiagnostics: number): Warning | null {
  if (project.getCompilerOptions().allowJs === true) return null;

  project.compilerOptions.set({ allowJs: true });

  const jsFileCount = project
    .getSourceFiles()
    .filter((file) => /\.(js|jsx)$/i.test(file.getFilePath())).length;
  if (jsFileCount === 0) return null;

  const diagnosticsNote =
    configDiagnostics > 0
      ? `（配置解析有 ${configDiagnostics} 条诊断，常见原因是 extends 的基础配置未安装）`
      : "";
  return staticWarning(
    "allow_js_enabled",
    `${configLabel} 未开启 allowJs${diagnosticsNote}；已为本次分析补开。否则 ${jsFileCount} 个 .js/.jsx 源文件的导出无法被识别，其中的具名导出组件会静默丢失。`,
  );
}

export interface ProjectScanOptions {
  /** 相对 rootPath 的源码目录；缺省时扫描整个项目。 */
  sourceRoots?: readonly string[];
  /** 是否排除测试/Storybook 文件，默认保留旧行为。 */
  excludeTests?: boolean;
  excludeStorybook?: boolean;
}

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

/** 向下发现的嵌套 tsconfig 及其对项目源码的覆盖情况。 */
interface NestedConfigCandidate extends ConfigCandidate {
  /** 相对项目根的目录深度，用于同等覆盖时优先选更靠近根的配置。 */
  depth: number;
  /** 该配置装载的、位于项目根内的源文件数。 */
  fileCount: number;
  /** 该配置装载的、位于项目根内的 JSX 源文件数。 */
  jsxCount: number;
  /** 该配置装载的文件与项目实际扫描文件的交集数量。 */
  coveredSourceCount: number;
  /** 该配置自身的诊断数量（如 extends 未安装）。不阻断装载，但要在告警里可见。 */
  diagnosticCount: number;
}

interface ProjectTreeScan {
  configPaths: string[];
  sourceFiles: string[];
}

function staticWarning(code: string, message: string): Warning {
  return { code, severity: "warning", stage: "static", message };
}

function staticInfo(code: string, message: string): Warning {
  return { code, severity: "info", stage: "static", message };
}

function relativePath(rootPath: string, target: string): string {
  return path.relative(rootPath, target).replace(/\\/g, "/");
}

function normalizePath(filePath: string): string {
  return path.resolve(filePath).replace(/\\/g, "/").toLowerCase();
}

function isInside(rootPath: string, target: string): boolean {
  const relative = path.relative(rootPath, target);
  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

/**
 * 一次性扫描项目树：收集 tsconfig 与源文件。
 *
 * 跳过依赖/构建产物/测试夹具目录，避免把 node_modules 内的配置或无关子项目
 * 误选成主配置。不跟随符号链接，避免循环目录。
 */
function scanProjectTree(rootPath: string): ProjectTreeScan {
  const configPaths: string[] = [];
  const sourceFiles: string[] = [];

  const walk = (directory: string, allowConfig: boolean, allowSource: boolean): void => {
    let entries: Dirent[];
    try {
      entries = readdirSync(directory, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const fullPath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        const lower = entry.name.toLowerCase();
        const nextAllowConfig =
          allowConfig && !lower.startsWith(".") && !NESTED_CONFIG_IGNORED_DIRS.has(lower);
        const nextAllowSource = allowSource && !SOURCE_IGNORED_DIRS.has(lower);
        if (!nextAllowConfig && !nextAllowSource) continue;
        walk(fullPath, nextAllowConfig, nextAllowSource);
        continue;
      }
      if (!entry.isFile()) continue;
      if (allowConfig && entry.name.toLowerCase() === "tsconfig.json") {
        configPaths.push(fullPath);
      } else if (allowSource && SOURCE_FILE_PATTERN.test(entry.name)) {
        sourceFiles.push(fullPath);
      }
    }
  };

  walk(rootPath, true, true);
  return { configPaths, sourceFiles };
}

function countInProject(rootPath: string, fileNames: readonly string[], sourceKeys: Set<string>) {
  let fileCount = 0;
  let jsxCount = 0;
  let coveredSourceCount = 0;
  for (const fileName of fileNames) {
    if (!isInside(rootPath, fileName)) continue;
    fileCount += 1;
    if (/\.(tsx|jsx)$/i.test(fileName)) jsxCount += 1;
    if (sourceKeys.has(normalizePath(fileName))) coveredSourceCount += 1;
  }
  return { fileCount, jsxCount, coveredSourceCount };
}

/**
 * 根目录没有 tsconfig 时向下发现候选配置。
 *
 * 选择顺序（全部可解释）：
 *  1. 只保留实际装载到已扫描源文件的配置；
 *  2. 覆盖已扫描源文件更多的优先——避免把只覆盖一个夹具文件的浅层配置选成主体；
 *  3. 同等覆盖时目录更浅（离根更近）的优先；
 *  4. 再按 JSX 源文件数、文件数、路径排序，保证结果稳定。
 *
 * 返回的 candidates 包含全部成功解析的配置，便于告警里解释为什么没选它。
 */
function discoverNestedConfig(
  rootPath: string,
  scan: ProjectTreeScan,
): { chosen: NestedConfigCandidate | null; candidates: NestedConfigCandidate[]; scannedCount: number } {
  const sourceKeys = new Set(scan.sourceFiles.map(normalizePath));
  const candidates: NestedConfigCandidate[] = [];

  for (const configPath of scan.configPaths) {
    const parsed = parseConfig(configPath);
    if (parsed === null) continue;
    const counts = countInProject(rootPath, parsed.fileNames, sourceKeys);
    candidates.push({
      configPath,
      parsed,
      depth: relativePath(rootPath, path.dirname(configPath)).split("/").filter(Boolean).length,
      ...counts,
      diagnosticCount: parsed.errors.length,
    });
  }

  const eligible = candidates.filter((candidate) => candidate.coveredSourceCount > 0);
  eligible.sort(
    (left, right) =>
      right.coveredSourceCount - left.coveredSourceCount ||
      left.depth - right.depth ||
      right.jsxCount - left.jsxCount ||
      right.fileCount - left.fileCount ||
      left.configPath.localeCompare(right.configPath),
  );

  return {
    chosen: eligible[0] ?? null,
    candidates,
    scannedCount: scan.sourceFiles.length,
  };
}

function describeNestedCandidates(rootPath: string, candidates: readonly NestedConfigCandidate[]): string {
  return candidates
    .slice()
    .sort((left, right) => left.depth - right.depth || left.configPath.localeCompare(right.configPath))
    .slice(0, 6)
    .map(
      (candidate) =>
        `${relativePath(rootPath, candidate.configPath)}（覆盖 ${candidate.coveredSourceCount}、JSX ${candidate.jsxCount}、深度 ${candidate.depth}）`,
    )
    .join("；");
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
function createGlobProject(rootPath: string, warnings: Warning[], options: ProjectScanOptions = {}): FullProjectResult {
  const project = new Project({
    skipAddingFilesFromTsConfig: true,
    compilerOptions: { ...FALLBACK_COMPILER_OPTIONS },
  });
  project.addSourceFilesAtPaths(sourceGlobs(rootPath, options));

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

/** 全量与兜底路径共用的源码 glob，保证两条路径扫描范围一致。 */
function sourceGlobs(rootPath: string, options: ProjectScanOptions = {}): string[] {
  const root = rootPath.replace(/\\/g, "/").replace(/\/$/, "");
  const roots = options.sourceRoots?.length
    ? options.sourceRoots.map((sourceRoot) => `${root}/${sourceRoot.replace(/\\/g, "/").replace(/^\//, "").replace(/\/$/, "")}`)
    : [root];
  const globs = roots.map((sourceRoot) => `${sourceRoot}/**/*.{ts,tsx,js,jsx}`);
  const excluded = [...EXCLUDED_DIRS].map((dir) => `!${root}/**/${dir}/**`);
  if (options.excludeTests) {
    excluded.push(`!${root}/**/__tests__/**`, `!${root}/**/*.test.*`, `!${root}/**/*.spec.*`);
  }
  if (options.excludeStorybook) {
    excluded.push(`!${root}/**/*.stories.*`, `!${root}/**/storybook/**`);
  }
  return [...globs, ...excluded];
}

/**
 * 创建全量分析用的 Project。
 *
 * 优先使用项目自身的 tsconfig —— 这是正确解析 `@/*` 路径别名与 monorepo
 * workspace 包的唯一可靠来源，禁止退化为按名称匹配。solution-style 配置
 * 会改用 references 里装载源文件最多的子配置。
 */
export function createFullProject(rootPath: string, options: ProjectScanOptions = {}): FullProjectResult {
  const warnings: Warning[] = [];
  const rootConfigPath = path.join(rootPath, "tsconfig.json");

  if (!existsSync(rootConfigPath)) {
    // 根目录没有 tsconfig 时先向下发现子项目配置（如 website/tsconfig.json）。
    // 若确实一份都没有，才退回原来的 no_tsconfig + glob 行为。
    const scan = scanProjectTree(rootPath);
    if (scan.configPaths.length === 0) {
      warnings.push(
        staticWarning(
          "no_tsconfig",
          "未找到 tsconfig.json（含子目录），路径别名（paths）无法解析，跨目录别名 import 将被记为未解析；组件发现仍按相对路径工作。",
        ),
      );
      return createGlobProject(rootPath, warnings, options);
    }

    const { chosen, candidates, scannedCount } = discoverNestedConfig(rootPath, scan);
    if (chosen === null) {
      warnings.push(
        staticWarning(
          "nested_tsconfig_no_coverage",
          `发现 ${candidates.length} 份子目录 tsconfig，但没有任何一份装载到已扫描的源文件（候选：${describeNestedCandidates(rootPath, candidates)}）；已退化为目录扫描。`,
        ),
      );
      return createGlobProject(rootPath, warnings, options);
    }

    const chosenPath = relativePath(rootPath, chosen.configPath);
    const eligibleCount = candidates.filter((candidate) => candidate.coveredSourceCount > 0).length;
    if (eligibleCount > 1) {
      warnings.push(
        staticInfo(
          "nested_tsconfig_selected",
          `根目录无 tsconfig，发现 ${eligibleCount} 份覆盖源文件的子目录配置；选用 ${chosenPath}（覆盖 ${chosen.coveredSourceCount}/${scannedCount}、JSX ${chosen.jsxCount}）。候选：${describeNestedCandidates(rootPath, candidates)}。`,
        ),
      );
    } else {
      warnings.push(
        staticInfo(
          "nested_tsconfig_used",
          `根目录无 tsconfig，选用子目录配置 ${chosenPath}（覆盖 ${chosen.coveredSourceCount}/${scannedCount} 个源文件、JSX ${chosen.jsxCount}；配置诊断 ${chosen.diagnosticCount} 条）。`,
        ),
      );
    }
    if (chosen.coveredSourceCount < scannedCount) {
      warnings.push(
        staticWarning(
          "nested_tsconfig_partial_coverage",
          `选用的子目录配置 ${chosenPath} 只覆盖 ${chosen.coveredSourceCount}/${scannedCount} 个已扫描源文件；未覆盖的 ${scannedCount - chosen.coveredSourceCount} 个仍按目录扫描加入，别名/类型解析可能不适用于它们。`,
        ),
      );
    }

    let nestedProject: Project;
    try {
      nestedProject = new Project({
        tsConfigFilePath: chosen.configPath,
        skipAddingFilesFromTsConfig: true,
      });
      nestedProject.addSourceFilesAtPaths(sourceGlobs(rootPath));
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      warnings.push(
        staticWarning(
          "nested_tsconfig_load_failed",
          `子目录配置装载失败（${chosenPath}）：${detail}；已退化为目录扫描，别名解析不可用。`,
        ),
      );
      return createGlobProject(rootPath, warnings, options);
    }

    if (nestedProject.getSourceFiles().length === 0) {
      warnings.push(
        staticWarning(
          "nested_tsconfig_no_files",
          `子目录配置 ${chosenPath} 装载后没有任何可分析源文件，已退化为目录扫描。`,
        ),
      );
      return createGlobProject(rootPath, warnings, options);
    }

    // 嵌套配置常见于「外壳仓库 + website 子项目」，其 tsconfig 往往 extends
    // 未安装在当前目录的基础配置，allowJs 会在解析时丢失。必须在建图前补开，
    // 否则 .jsx 具名导出组件会静默消失。
    const allowJsWarning = ensureAllowJs(nestedProject, chosenPath, chosen.parsed.errors.length);
    if (allowJsWarning) warnings.push(allowJsWarning);

    return {
      project: nestedProject,
      fromTsConfig: true,
      tsConfigPath: chosen.configPath,
      warnings,
    };
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

  const allowJsWarning = ensureAllowJs(project, path.basename(primary.configPath), primary.parsed.errors.length);
  if (allowJsWarning) warnings.push(allowJsWarning);

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
  let primary = candidates[0];

  // 全量路径已支持根目录无 tsconfig 时向下发现；增量路径沿用同一份配置，
  // 否则同一项目在增量分析时会突然失去 paths/baseUrl。
  if (primary === undefined && !existsSync(path.join(rootPath, "tsconfig.json"))) {
    const scan = scanProjectTree(rootPath);
    const { chosen } = discoverNestedConfig(rootPath, scan);
    if (chosen !== null) primary = chosen;
  }

  const project =
    primary !== undefined
      ? new Project({
          tsConfigFilePath: primary.configPath,
          skipAddingFilesFromTsConfig: true,
          // 增量路径没有告警通道，与全量路径保持一致直接补开：否则 .jsx
          // 具名导出组件在增量重算时会再次静默消失
          compilerOptions: { allowJs: true },
        })
      : new Project({
          skipAddingFilesFromTsConfig: true,
          compilerOptions: { ...FALLBACK_COMPILER_OPTIONS },
        });

  project.addSourceFilesAtPaths(
    changedFiles.map((file) => path.resolve(rootPath, file).replace(/\\/g, "/")),
  );
  return project;
}
