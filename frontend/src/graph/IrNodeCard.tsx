import { Handle, Position, type NodeProps } from "@xyflow/react";
import { FileCode2, Sparkles } from "lucide-react";

import { KIND_META } from "@/graph/meta";
import type { IrFlowNode } from "@/graph/layout";

export function IrNodeCard({ data, selected }: NodeProps<IrFlowNode>) {
  const meta = KIND_META[data.kind];

  return (
    <div
      className={`w-52 rounded-xl border bg-[#1a2130] px-3 py-2 shadow-[0_14px_36px_-22px_rgba(0,0,0,0.9)] transition ${
        selected
          ? "border-indigo-400/70 shadow-[0_0_26px_-10px_rgba(99,102,241,0.9)] ring-2 ring-indigo-500/25"
          : `${meta.border} hover:border-white/25`
      }`}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!h-2 !w-2 !border-2 !border-[#121722] !bg-slate-500"
      />
      <div className="flex items-center justify-between gap-2">
        <span
          className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${meta.badge}`}
        >
          {meta.label}
        </span>
        <div className="flex items-center gap-1 text-slate-400">
          {data.hasAiEvidence && <Sparkles size={12} className="text-amber-400" />}
          <FileCode2 size={12} />
        </div>
      </div>
      <p className="mt-1.5 truncate text-sm font-semibold text-slate-100">{data.name}</p>
      <div className="mt-0.5 flex items-center justify-between gap-2">
        <p className="truncate font-mono text-[10px] text-slate-400">
          {data.file ?? "外部接口"}
        </p>
        <span className="shrink-0 text-[10px] text-slate-400">{data.evidenceCount} 证据</span>
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="!h-2 !w-2 !border-2 !border-[#121722] !bg-slate-500"
      />
    </div>
  );
}
