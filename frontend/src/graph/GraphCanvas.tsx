import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  applyEdgeChanges,
  applyNodeChanges,
  type Edge,
  type EdgeChange,
  type NodeChange,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useState } from "react";

import { IrNodeCard } from "@/graph/IrNodeCard";
import { buildFlowGraph, type IrFlowNode } from "@/graph/layout";
import { KIND_META, SOURCE_META } from "@/graph/meta";
import type { IRGraph } from "@uicodeanalysis/contracts";

const nodeTypes = { ir: IrNodeCard } satisfies NodeTypes;

interface GraphCanvasProps {
  graph: IRGraph;
  selectedNodeId: string | null;
  visibleNodeIds: Set<string>;
  onSelectNode: (nodeId: string | null) => void;
}

export function GraphCanvas({
  graph,
  selectedNodeId,
  visibleNodeIds,
  onSelectNode,
}: GraphCanvasProps) {
  const [nodes, setNodes] = useState<IrFlowNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);

  useEffect(() => {
    const next = buildFlowGraph(graph);
    setNodes(next.nodes);
    setEdges(next.edges);
  }, [graph]);

  const onNodesChange = useCallback(
    (changes: NodeChange<IrFlowNode>[]) =>
      setNodes((current) => applyNodeChanges(changes, current)),
    [],
  );
  const onEdgesChange = useCallback(
    (changes: EdgeChange<Edge>[]) => setEdges((current) => applyEdgeChanges(changes, current)),
    [],
  );

  const renderedNodes = useMemo(
    () =>
      nodes.map((node) => ({
        ...node,
        selected: node.id === selectedNodeId,
        hidden: !visibleNodeIds.has(node.id),
      })),
    [nodes, selectedNodeId, visibleNodeIds],
  );

  const renderedEdges = useMemo(
    () =>
      edges.map((edge) => ({
        ...edge,
        hidden: !visibleNodeIds.has(edge.source) || !visibleNodeIds.has(edge.target),
      })),
    [edges, visibleNodeIds],
  );

  return (
    <div className="relative h-full w-full">
      <ReactFlow
        nodes={renderedNodes}
        edges={renderedEdges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, node) => onSelectNode(node.id)}
        onPaneClick={() => onSelectNode(null)}
        fitView
        fitViewOptions={{ padding: 0.24 }}
        minZoom={0.25}
        maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#cbd5e1" gap={18} size={1} />
        <Controls showInteractive={false} className="!rounded-lg !border !border-slate-200" />
        <MiniMap
          pannable
          zoomable
          className="!rounded-lg !border !border-slate-200"
          nodeColor={(node) => {
            const kind = (node.data as { kind?: keyof typeof KIND_META }).kind;
            return kind ? KIND_META[kind].color : "#cbd5e1";
          }}
        />
      </ReactFlow>

      <div className="pointer-events-none absolute bottom-4 left-4 flex flex-wrap gap-3 rounded-lg border border-slate-200 bg-white/90 px-3 py-2 text-[11px] text-slate-600 shadow-sm backdrop-blur">
        {(["static", "runtime", "ai"] as const).map((source) => (
          <span key={source} className="flex items-center gap-1.5">
            <span
              className="inline-block h-0.5 w-5 rounded"
              style={{
                backgroundColor: SOURCE_META[source].color,
                borderTop:
                  source === "ai" ? `2px dashed ${SOURCE_META[source].color}` : undefined,
              }}
            />
            {SOURCE_META[source].label}
          </span>
        ))}
      </div>
    </div>
  );
}
