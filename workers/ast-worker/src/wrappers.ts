/**
 * 二次封装映射：把「项目内封装组件」关联到它真正包着的 UI 库组件。
 *
 * ## 为什么必须是配置而不是代码
 *
 * 项目文档 4.3 明确要求映射规则存放于配置、不写入代码。原因很实在：
 * 封装组件名是**每个企业项目自造**的（`ProTable` / `SmartTable` / `BizTable`），
 * 核心分析器一旦写 `if (tag === "ProTable")`，就只对某一个项目有效，换项目全废，
 * 而且违反 README 禁止的「按业务名硬编码判断」。
 *
 * 所以这里只做两件事：读配置、按名字查表。规则内容全部来自 `mapping.json`。
 *
 * ## 映射不新增节点与边
 *
 * IR 的 `NodeKind` / `EdgeKind` 是受控枚举，新增需升 schema 版本。而「ProTable
 * 封装了 antd.Table」这件事，现有的 `renders` 边已经能完整表达——ProTable 的
 * 函数体里就写了 `<Table />`，renders 分析器本来就会连出
 * `ProTable ──renders──> antd#Table`。映射的增量价值是**语义注解**：告诉读图的人
 * 这条边代表二次封装，以及 props 怎么改名。因此映射结果写进组件节点的 attrs。
 *
 * 这样设计的好处是不会凭空发明结构：如果 renders 分析器认不出那个标签（比如
 * 封装里用了动态组件），映射也不会伪造一条边出来，只在 attrs 里如实记录
 * `resolved: false`。
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

import type { Warning } from "@uicodeanalysis/contracts";
import type { WrapperMapping, WrapperRule } from "@uicodeanalysis/contracts";

import type { DiscoveredComponent } from "./components.js";

/** 映射格式版本。与 IR schema 版本独立演进，读入时校验。 */
const SUPPORTED_SCHEMA_VERSION = "1.0";

/** 一个已匹配到项目内组件的封装规则。 */
export interface ResolvedWrapper {
  /** 项目内封装组件的 id */
  componentId: string;
  /** 被封装的库组件名（`Table`） */
  target: string;
  /** 目标组件的来源包（`antd`），用于拼外部节点 id */
  package: string;
  /** props 映射：封装 props 名 → 目标 props 名 */
  props: Record<string, string>;
  description: string | null;
}

export interface WrapperMappingResult {
  /** 组件 id → 封装信息 */
  byComponent: Map<string, ResolvedWrapper>;
  warnings: Warning[];
}

/**
 * 校验并收窄映射文件。
 *
 * 手写校验而不是引 ajv：这份配置结构极简，且读入发生在分析进程内，
 * 一个 JSON Schema 校验器的启动成本比整个文件本身还大。校验失败一律返回
 * null 并由调用方记告警 —— 配置错了要说出来，不能当成"没有映射"。
 */
export function parseWrapperMapping(raw: unknown): WrapperMapping | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;

  const candidate = raw as Record<string, unknown>;
  if (candidate.schemaVersion !== SUPPORTED_SCHEMA_VERSION) return null;
  if (typeof candidate.pluginId !== "string" || candidate.pluginId.length === 0) return null;
  if (typeof candidate.package !== "string" || candidate.package.length === 0) return null;
  if (!Array.isArray(candidate.wrappers) || candidate.wrappers.length === 0) return null;

  const wrappers: WrapperRule[] = [];
  for (const item of candidate.wrappers) {
    if (!item || typeof item !== "object") return null;
    const rule = item as Record<string, unknown>;
    if (typeof rule.name !== "string" || rule.name.length === 0) return null;
    if (typeof rule.target !== "string" || rule.target.length === 0) return null;

    // props 有就必须是 string→string 的扁平表；嵌套结构无法直接对应到 props 名
    let props: Record<string, string> | undefined;
    if (rule.props !== undefined) {
      if (!rule.props || typeof rule.props !== "object" || Array.isArray(rule.props)) return null;
      props = {};
      for (const [key, value] of Object.entries(rule.props)) {
        if (typeof value !== "string") return null;
        props[key] = value;
      }
    }

    wrappers.push({
      name: rule.name,
      target: rule.target,
      ...(props ? { props } : {}),
      ...(typeof rule.description === "string" ? { description: rule.description } : {}),
    });
  }

  return {
    schemaVersion: candidate.schemaVersion,
    pluginId: candidate.pluginId,
    package: candidate.package,
    wrappers: wrappers as [WrapperRule, ...WrapperRule[]],
  };
}

/**
 * 把映射规则对应到实际发现的组件上。
 *
 * 按**组件名**匹配而不是按文件路径：封装组件在企业项目里会被多人复制到不同
 * 目录，按名字才覆盖得到。代价是同名不同义的组件会被一起注解，因此匹配结果
 * 带 `confidence` 之外的说明——真有歧义时以渲染边为准，attrs 只是注解。
 */
export function resolveWrappers(
  mappings: readonly WrapperMapping[],
  components: readonly DiscoveredComponent[],
  rootPath: string,
): WrapperMappingResult {
  const byComponent = new Map<string, ResolvedWrapper>();
  const warnings: Warning[] = [];

  for (const mapping of mappings) {
    for (const rule of mapping.wrappers) {
      const matches = components.filter((component) => component.name === rule.name);
      if (matches.length === 0) {
        warnings.push({
          code: "wrapper_not_found",
          severity: "info",
          stage: "static",
          message:
            `映射声明了 ${rule.name} → ${mapping.package}.${rule.target}，` +
            `但项目里没有找到叫 ${rule.name} 的组件；该规则本次未生效。`,
        });
        continue;
      }

      for (const component of matches) {
        byComponent.set(component.id, {
          componentId: component.id,
          target: rule.target,
          package: mapping.package,
          props: rule.props ?? {},
          description: rule.description ?? null,
        });
      }
    }
  }

  // rootPath 参与签名是为了将来按节点位置做优先级判定的扩展，当前按名字匹配不需要
  void rootPath;

  return { byComponent, warnings };
}

/** 封装注解写进组件节点 attrs 的形状。 */
export function serializeWrapper(
  wrapper: ResolvedWrapper,
  targetNodeId: string,
  resolved: boolean,
): Record<string, unknown> {
  return {
    // 目标组件的 IR 节点 id，与 renders 边指向的外部节点一致
    target: targetNodeId,
    display: `${wrapper.target}`,
    package: wrapper.package,
    props: wrapper.props,
    // 渲染边是否真的连到了目标组件：封装里用动态组件时可能是 false
    resolved,
    ...(wrapper.description ? { description: wrapper.description } : {}),
  };
}

/** 外部组件节点 id 的约定，与 renders 分析器保持一致。 */
export function externalNodeId(packageName: string, componentName: string): string {
  return `${packageName}#${componentName}`;
}

/** 项目根目录下的映射文件位置：`mapping.json` 与 `mappings/*.json`。 */
function mappingCandidates(rootPath: string): string[] {
  const candidates = [path.join(rootPath, "mapping.json")];
  const dir = path.join(rootPath, "mappings");
  if (existsSync(dir) && statSync(dir).isDirectory()) {
    for (const entry of readdirSync(dir)) {
      if (entry.endsWith(".json")) candidates.push(path.join(dir, entry));
    }
  }
  return candidates;
}

/**
 * 从项目根目录读取二次封装映射并解析到组件。
 *
 * 文件缺失不告警——没配映射是常态，不是错误。文件存在却读不懂才告警：
 * 那时用户以为自己配了映射，实际一条都没生效，静默会让人长期排查错方向。
 */
export function loadWrapperMappings(
  rootPath: string,
  components: readonly DiscoveredComponent[],
): WrapperMappingResult {
  const warnings: Warning[] = [];
  const mappings: WrapperMapping[] = [];

  for (const file of mappingCandidates(rootPath)) {
    if (!existsSync(file)) continue;

    let raw: unknown;
    try {
      raw = JSON.parse(readFileSync(file, "utf8"));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      warnings.push({
        code: "mapping_invalid",
        severity: "warning",
        stage: "static",
        message: `${path.relative(rootPath, file)} 不是合法 JSON，二次封装映射本次未生效：${message}`,
      });
      continue;
    }

    const mapping = parseWrapperMapping(raw);
    if (!mapping) {
      warnings.push({
        code: "mapping_invalid",
        severity: "warning",
        stage: "static",
        message:
          `${path.relative(rootPath, file)} 不符合 mapping.schema.json，二次封装映射本次未生效。` +
          `请核对 schemaVersion / pluginId / package / wrappers 字段。`,
      });
      continue;
    }
    mappings.push(mapping);
  }

  const result = resolveWrappers(mappings, components, rootPath);
  return { byComponent: result.byComponent, warnings: [...warnings, ...result.warnings] };
}

/**
 * 把解析结果整理成写进组件节点 attrs 的形状。
 *
 * `renderedTargets` 是 renders 边的 `${from} ${to}` 集合：只有渲染边真的连到了
 * 目标组件，注解才敢标 `resolved: true`。封装里用动态组件时连不上，那时
 * attrs 如实写 false，而不是替渲染分析器补一条边。
 */
export function buildWrapperAttrs(
  byComponent: ReadonlyMap<string, ResolvedWrapper>,
  renderedTargets: ReadonlySet<string>,
): Map<string, Record<string, unknown>> {
  const attrs = new Map<string, Record<string, unknown>>();
  for (const [componentId, wrapper] of byComponent) {
    const targetId = externalNodeId(wrapper.package, wrapper.target);
    attrs.set(
      componentId,
      serializeWrapper(wrapper, targetId, renderedTargets.has(`${componentId} ${targetId}`)),
    );
  }
  return attrs;
}
