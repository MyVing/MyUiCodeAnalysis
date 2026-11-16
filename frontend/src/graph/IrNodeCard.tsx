import { Handle, Position, type NodeProps } from "@xyflow/react";
import { FileCode2, Sparkles } from "lucide-react";

import { KIND_META } from "@/graph/meta";
import type { IrFlowNode } from "@/graph/layout";

export function IrNodeCard({ data, selected }: NodeProps<IrFlowNode>) {
  const meta = KIND_META[data.kind];

  return (
    <div
      className={`w-52 rounded-xl border bg-white px-3 py-2 shadow-sm transition ${
        selected ? "border-indigo-400 ring-2 ring-indigo-200" : meta.border
      }`}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !bg-slate-300" />
      <div className="flex items-center justify-between gap-2">
        <span
          className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${meta.badge}`}
        >
          {meta.label}
        </span>
        <div className="flex items-center gap-1 text-slate-400">
          {data.hasAiEvidence && <Sparkles size={12} className="text-amber-500" />}
          <FileCode2 size={12} />
        </div>
      </div>
      <p className="mt-1.5 truncate text-sm font-semibold text-slate-800">{data.name}</p>
      <div className="mt-0.5 flex items-center justify-between gap-2">
        <p className="truncate font-mono text-[10px] text-slate-400">{data.file ?? "外部接口"}</p>
        <span className="shrink-0 text-[10px] text-slate-400">{data.evidenceCount} 证据</span>
      </div>
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !bg-slate-300" />
    </div>
  );
}
