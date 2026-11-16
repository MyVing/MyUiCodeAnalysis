import { Search } from "lucide-react";

import { KIND_META } from "@/graph/meta";
import type { IRGraph, NodeKind } from "@uicodeanalysis/contracts";

interface GraphSidebarProps {
  graph: IRGraph;
  search: string;
  onSearchChange: (value: string) => void;
  activeKinds: Set<NodeKind>;
  onToggleKind: (kind: NodeKind) => void;
  onSelectAllKinds: () => void;
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string) => void;
}

export function GraphSidebar({
  graph,
  search,
  onSearchChange,
  activeKinds,
  onToggleKind,
  onSelectAllKinds,
  selectedNodeId,
  onSelectNode,
}: GraphSidebarProps) {
  const counts = new Map<NodeKind, number>();
  for (const node of graph.nodes) {
    counts.set(node.kind, (counts.get(node.kind) ?? 0) + 1);
  }

  const keyword = search.trim().toLowerCase();
  const filtered = graph.nodes.filter((node) => {
    if (!activeKinds.has(node.kind)) {
      return false;
    }
    if (!keyword) {
      return true;
    }
    const file = node.file ?? "";
    return [node.name, node.id, file, node.kind].join(" ").toLowerCase().includes(keyword);
  });

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-slate-200 p-3">
        <label className="relative block">
          <Search size={14} className="absolute left-2.5 top-2.5 text-slate-400" />
          <input
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
            placeholder="搜索组件、文件或 ID"
            className="w-full rounded-lg border border-slate-200 bg-white py-1.5 pl-8 pr-2 text-xs outline-none transition focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
          />
        </label>

        <div className="mt-3 flex items-center justify-between">
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
            节点类型
          </span>
          <button
            type="button"
            onClick={onSelectAllKinds}
            className="text-[11px] text-indigo-600 hover:text-indigo-700"
          >
            全部
          </button>
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {[...counts.entries()].map(([kind, count]) => {
            const active = activeKinds.has(kind);
            return (
              <button
                key={kind}
                type="button"
                onClick={() => onToggleKind(kind)}
                className={`rounded-full border px-2 py-0.5 text-[10px] transition ${
                  active
                    ? KIND_META[kind].badge
                    : "border-slate-200 bg-white text-slate-400 hover:text-slate-600"
                }`}
              >
                {KIND_META[kind].label} {count}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex items-center justify-between px-3 py-2 text-[11px] text-slate-400">
        <span>节点 {filtered.length}</span>
        <span>共 {graph.nodes.length}</span>
      </div>

      <div className="flex-1 space-y-1 overflow-y-auto px-2 pb-3">
        {filtered.map((node) => {
          const file = node.file;
          const selected = node.id === selectedNodeId;

          return (
            <button
              key={node.id}
              type="button"
              onClick={() => onSelectNode(node.id)}
              className={`w-full rounded-lg border px-2.5 py-2 text-left transition ${
                selected
                  ? "border-indigo-300 bg-indigo-50"
                  : "border-transparent hover:border-slate-200 hover:bg-slate-50"
              }`}
            >
              <span
                className={`inline-block rounded border px-1 py-0.5 text-[9px] ${KIND_META[node.kind].badge}`}
              >
                {KIND_META[node.kind].label}
              </span>
              <p className="mt-1 truncate text-xs font-medium text-slate-800">{node.name}</p>
              <p className="truncate font-mono text-[10px] text-slate-400">{file ?? node.id}</p>
            </button>
          );
        })}

        {filtered.length === 0 && (
          <p className="px-2 py-6 text-center text-xs text-slate-400">没有匹配的节点</p>
        )}
      </div>
    </div>
  );
}
