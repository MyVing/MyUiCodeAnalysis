import * as dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import { MarkerType } from "@xyflow/react";

import { EDGE_LABEL, primarySource, SOURCE_META } from "@/graph/meta";
import type { IRGraph, NodeKind } from "@uicodeanalysis/contracts";

export const NODE_WIDTH = 208;
export const NODE_HEIGHT = 76;

export interface IrNodeData extends Record<string, unknown> {
  name: string;
  kind: NodeKind;
  file?: string;
  evidenceCount: number;
  hasAiEvidence: boolean;
}

export type IrFlowNode = Node<IrNodeData, "ir">;

export interface FlowGraph {
  nodes: IrFlowNode[];
  edges: Edge[];
}

export function buildFlowGraph(graph: IRGraph): FlowGraph {
  const layout = new dagre.graphlib.Graph();
  layout.setDefaultEdgeLabel(() => ({}));
  layout.setGraph({ rankdir: "LR", nodesep: 32, ranksep: 96, marginx: 24, marginy: 24 });

  for (const node of graph.nodes) {
    layout.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of graph.edges) {
    layout.setEdge(edge.from, edge.to);
  }
  dagre.layout(layout);

  const nodes: IrFlowNode[] = graph.nodes.map((node) => {
    const position = layout.node(node.id);
    const file = node.file;

    return {
      id: node.id,
      type: "ir",
      position: {
        x: position.x - NODE_WIDTH / 2,
        y: position.y - NODE_HEIGHT / 2,
      },
      data: {
        name: node.name,
        kind: node.kind,
        file,
        evidenceCount: node.evidence.length,
        hasAiEvidence: node.evidence.some((item) => item.source === "ai"),
      },
    };
  });

  const edges: Edge[] = graph.edges.map((edge) => {
    const source = primarySource(edge.evidence);
    const color = SOURCE_META[source].color;

    return {
      id: edge.id,
      source: edge.from,
      target: edge.to,
      type: "smoothstep",
      label: EDGE_LABEL[edge.kind],
      animated: source === "ai",
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 18, height: 18 },
      style: {
        stroke: color,
        strokeWidth: source === "runtime" ? 2 : 1.5,
        strokeDasharray: source === "ai" ? "6 4" : undefined,
      },
      labelStyle: { fill: "#cbd5e1", fontSize: 10, fontFamily: "monospace" },
      labelBgStyle: { fill: "#151b25", fillOpacity: 0.95 },
      data: { source },
    };
  });

  return { nodes, edges };
}
