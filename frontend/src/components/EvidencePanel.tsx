import { formatConfidence, SOURCE_META } from "@/graph/meta";
import type { Evidence } from "@uicodeanalysis/contracts";

interface EvidencePanelProps {
  evidence: readonly Evidence[];
}

export function EvidencePanel({ evidence }: EvidencePanelProps) {
  return (
    <ul className="space-y-2">
      {evidence.map((item, index) => {
        const meta = SOURCE_META[item.source];

        return (
          <li
            key={`${item.source}-${index}`}
            className={`rounded-lg border p-2.5 text-xs ${meta.panel}`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${meta.badge}`}>
                {meta.label}
              </span>
              <span className="font-mono text-[11px] text-slate-400">
                置信度 {formatConfidence(item.confidence)}
              </span>
            </div>

            {item.file && (
              <p className="mt-1.5 break-all font-mono text-[11px] text-slate-300">
                {item.file}
                {item.loc ? `:${item.loc.line}:${item.loc.col}` : ""}
              </p>
            )}
            {item.traceId && (
              <p className="mt-1.5 break-all font-mono text-[11px] text-emerald-300">
                trace: {item.traceId}
              </p>
            )}
            {item.source === "ai" && (
              <p className="mt-1.5 text-[11px] text-amber-300">推断结论，需人工确认或驳回。</p>
            )}
          </li>
        );
      })}
    </ul>
  );
}
