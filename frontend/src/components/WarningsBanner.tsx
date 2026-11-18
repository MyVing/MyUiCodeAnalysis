import { ChevronDown, ChevronRight, OctagonAlert, TriangleAlert, Info } from "lucide-react";
import { useState } from "react";

import type { Severity, Warning } from "@uicodeanalysis/contracts";

interface WarningsBannerProps {
  warnings: readonly Warning[];
}

const SEVERITY_META: Record<
  Severity,
  { label: string; icon: typeof Info; container: string; iconClass: string }
> = {
  error: {
    label: "错误",
    icon: OctagonAlert,
    container: "border-rose-400/20 bg-rose-400/[0.08] text-rose-200",
    iconClass: "text-rose-400",
  },
  warning: {
    label: "警告",
    icon: TriangleAlert,
    container: "border-amber-400/20 bg-amber-400/[0.08] text-amber-200",
    iconClass: "text-amber-400",
  },
  info: {
    label: "提示",
    icon: Info,
    container: "border-white/[0.08] bg-white/[0.03] text-slate-200",
    iconClass: "text-slate-400",
  },
};

/** 按严重程度排序，保证 error 永远在最上方。 */
const SEVERITY_ORDER: Record<Severity, number> = { error: 0, warning: 1, info: 2 };

export function WarningsBanner({ warnings }: WarningsBannerProps) {
  const [expanded, setExpanded] = useState(false);

  if (warnings.length === 0) {
    return null;
  }

  const sorted = [...warnings].sort(
    (left, right) => SEVERITY_ORDER[left.severity] - SEVERITY_ORDER[right.severity],
  );
  const worst = sorted[0]!;
  const meta = SEVERITY_META[worst.severity];
  const Icon = meta.icon;
  const hasDetails = warnings.length > 1;

  return (
    <div className={`shrink-0 border-b px-4 py-1.5 text-[11px] ${meta.container}`}>
      <div className="flex items-start gap-2">
        <Icon size={12} className={`mt-0.5 shrink-0 ${meta.iconClass}`} />
        <div className="min-w-0 flex-1">
          <p className="truncate">
            <span className="font-medium">{meta.label}</span>
            <span className="ml-1.5">
              {hasDetails ? `${warnings.length} 条分析告警，首条：` : ""}
              {worst.message}
            </span>
          </p>
          {worst.file && (
            <p className="mt-0.5 truncate font-mono text-[10px] opacity-70">
              {worst.file}
              {worst.loc ? `:${worst.loc.line}:${worst.loc.col}` : ""}
            </p>
          )}
        </div>
        {hasDetails && (
          <button
            type="button"
            onClick={() => setExpanded((current) => !current)}
            className="flex shrink-0 items-center gap-0.5 opacity-70 transition hover:opacity-100"
          >
            {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            明细
          </button>
        )}
      </div>

      {expanded && hasDetails && (
        <ul className="mt-1.5 space-y-1 border-t border-current/10 pl-5 pt-1.5">
          {sorted.map((warning, index) => {
            const itemMeta = SEVERITY_META[warning.severity];
            const ItemIcon = itemMeta.icon;
            return (
              <li key={`${warning.code}-${index}`} className="flex items-start gap-1.5">
                <ItemIcon size={11} className={`mt-0.5 shrink-0 ${itemMeta.iconClass}`} />
                <div className="min-w-0">
                  <span className="font-mono text-[10px] opacity-60">[{warning.code}]</span>{" "}
                  {warning.message}
                  {warning.file && (
                    <span className="ml-1 font-mono text-[10px] opacity-60">
                      {warning.file}
                      {warning.loc ? `:${warning.loc.line}` : ""}
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
