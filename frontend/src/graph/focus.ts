import type { IRGraph } from "@uicodeanalysis/contracts";

/** 默认聚焦半径：从选中节点沿依赖边双向各扩两跳 */
export const FOCUS_DEPTH = 2;
/** 单次渲染的节点上限：超过就截断，保证大仓库不卡顿 */
export const FOCUS_NODE_LIMIT = 120;

/**
 * 以 seed 为中心收集邻域节点，用于大图只渲染局部。
 *
 * - 沿边的两个方向扩展：既能看见「谁在用我」，也能看见「我用了谁」；
 * - `allowed` 是搜索与节点类型过滤后的集合，聚焦不会把过滤掉的节点带回来；
 * - 超出上限时保留靠近 seed 的层，结果稳定可复现。
 */
export function collectFocusNodeIds(
  graph: IRGraph,
  seedId: string,
  allowed: ReadonlySet<string>,
  depth = FOCUS_DEPTH,
  limit = FOCUS_NODE_LIMIT,
): Set<string> {
  if (!allowed.has(seedId)) {
    return new Set();
  }

  const adjacency = new Map<string, string[]>();
  const link = (from: string, to: string) => {
    const neighbors = adjacency.get(from);
    if (neighbors) {
      neighbors.push(to);
    } else {
      adjacency.set(from, [to]);
    }
  };
  for (const edge of graph.edges) {
    if (allowed.has(edge.from) && allowed.has(edge.to)) {
      link(edge.from, edge.to);
      link(edge.to, edge.from);
    }
  }

  const seen = new Set<string>([seedId]);
  let frontier = [seedId];
  for (let hop = 0; hop < depth && frontier.length > 0; hop += 1) {
    const next: string[] = [];
    for (const current of frontier) {
      for (const neighbor of adjacency.get(current) ?? []) {
        if (seen.has(neighbor)) {
          continue;
        }
        seen.add(neighbor);
        next.push(neighbor);
        if (seen.size >= limit) {
          return seen;
        }
      }
    }
    frontier = next;
  }
  return seen;
}

/** 按节点集合裁剪图；两端都在集合内的边才保留，布局只需算这一部分。 */
export function sliceGraph(graph: IRGraph, nodeIds: ReadonlySet<string>): IRGraph {
  return {
    ...graph,
    nodes: graph.nodes.filter((node) => nodeIds.has(node.id)),
    edges: graph.edges.filter(
      (edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to),
    ),
  };
}

/**
 * 计算本次要渲染的节点集合。
 *
 * 有服务端精确集合（需求匹配的 subgraph）时直接与过滤集合求交，不做邻域扩展：
 * 后端已经定义了「命中页面的证据范围」，前端再扩两跳会把它稀释回全图。
 * 交集为空说明过滤条件把证据节点全排除了，此时退回邻域聚焦，避免渲染空图。
 */
export function resolveVisibleNodeIds(
  graph: IRGraph,
  options: {
    allowed: ReadonlySet<string>;
    selectedNodeId: string | null;
    exactNodeIds: ReadonlySet<string> | null;
    focusMode: boolean;
    depth?: number;
    limit?: number;
  },
): ReadonlySet<string> {
  const { allowed, selectedNodeId, exactNodeIds, focusMode } = options;
  if (!focusMode) {
    return allowed;
  }
  if (exactNodeIds) {
    const exact = new Set([...exactNodeIds].filter((nodeId) => allowed.has(nodeId)));
    if (exact.size > 0) {
      return exact;
    }
  }
  // 选中节点被搜索/类型过滤掉时，退回过滤结果的第一个节点，保证总有可渲染的局部
  const seed = graph.nodes.find(
    (node) => node.id === selectedNodeId && allowed.has(node.id),
  )?.id;
  const center = seed ?? graph.nodes.find((node) => allowed.has(node.id))?.id;
  if (!center) {
    return allowed;
  }
  return collectFocusNodeIds(graph, center, allowed, options.depth, options.limit);
}
