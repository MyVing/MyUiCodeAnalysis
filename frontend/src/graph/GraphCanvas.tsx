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
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useRef, useState } from "react";

import { IrNodeCard } from "@/graph/IrNodeCard";
import { buildFlowGraph, type IrFlowNode } from "@/graph/layout";
import { KIND_META, SOURCE_META } from "@/graph/meta";
import type { IRGraph } from "@uicodeanalysis/contracts";

const nodeTypes = { ir: IrNodeCard } satisfies NodeTypes;

interface GraphCanvasProps {
  /** 已裁剪好的渲染子图：布局与 React Flow 只持有这些节点 */
  graph: IRGraph;
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string | null) => void;
}

export function GraphCanvas({ graph, selectedNodeId, onSelectNode }: GraphCanvasProps) {
  const [nodes, setNodes] = useState<IrFlowNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const flowRef = useRef<ReactFlowInstance<IrFlowNode, Edge> | null>(null);

  useEffect(() => {
    const next = buildFlowGraph(graph);
    setNodes(next.nodes);
    setEdges(next.edges);
    // 子图切换后坐标整体变化，必须重新 fit，否则视口会停在旧位置
    requestAnimationFrame(() => {
      void flowRef.current?.fitView({ padding: 0.24, duration: 180 });
    });
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

  const renderedNodes: IrFlowNode[] = nodes.map((node) => ({
    ...node,
    selected: node.id === selectedNodeId,
  }));

  return (
    <div className="relative h-full w-full">
      <ReactFlow
        nodes={renderedNodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onInit={(instance) => {
          flowRef.current = instance;
        }}
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
        <Background color="#1e2533" gap={20} size={1.2} />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          maskColor="rgba(6, 7, 11, 0.72)"
          nodeColor={(node) => {
            const kind = (node.data as { kind?: keyof typeof KIND_META }).kind;
            return kind ? KIND_META[kind].color : "#475569";
          }}
        />
      </ReactFlow>

      <div className="pointer-events-none absolute bottom-4 left-4 flex flex-wrap gap-3 rounded-xl border border-white/[0.12] bg-[#151b25]/90 px-3 py-2 text-[11px] text-slate-200 shadow-lg shadow-black/40 backdrop-blur">
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
