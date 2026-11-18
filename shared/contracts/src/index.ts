/**
 * 跨语言契约的 TypeScript 统一出口。
 *
 * ir / plugin / runtime / mapping / requirement / chain 六个文件由
 * shared/scripts/generate_contracts.py 生成，不要手改；需要新类型时改
 * shared/schemas/*.schema.json 后重新生成。
 *
 * 链路类型（ActionChain 等）从 chain.js 导出：requirement.ts 里也有一份
 * $ref 内联副本，但以 chain.schema.json 为唯一真源的这份为准。
 *
 * 本文件手写且稳定，保证上层 import 路径不随生成文件变化。
 */
export type {
  EdgeKind,
  Evidence,
  GraphMeta,
  IREdge,
  IRGraph,
  IRNode,
  Location,
  NodeKind,
  Severity,
  Source,
  Stage,
  Warning,
} from "./ir.js";

export type {
  Capability,
  PluginDependency,
  PluginManifest,
} from "./plugin.js";

export type {
  WrapperMapping,
  WrapperRule,
} from "./mapping.js";

export type {
  ConsoleEvent,
  Correlation,
  Interaction,
  NetworkEvent,
  PageError,
  PageInfo,
  RuntimeTrace,
  Viewport,
} from "./runtime.js";

export type {
  ActionChain,
  ChainPath,
  ChainRole,
  ChainStep,
} from "./chain.js";

export type {
  Candidate,
  Dropped,
  Feature,
  Match,
  RequirementMap,
  RequirementWarning,
  Signal,
  SignalKind,
  Subgraph,
  SubgraphCounts,
  Unmatched,
  VerifiedEvidence,
} from "./requirement.js";
