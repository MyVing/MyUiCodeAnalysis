import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleX,
  Loader2,
  LocateFixed,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { useState } from "react";

import { api } from "@/api/client";
import { useRequirementTask } from "@/api/queries";
import type { RequirementMap } from "@uicodeanalysis/contracts";

const REASON_LABEL: Record<string, string> = {
  node_not_in_candidates: "选了候选集外的节点",
  quote_not_found: "引文在节点原文中找不到",
  unknown_feature: "功能点 id 不在本次拆解结果里",
  invalid_shape: "字段类型不符",
};

interface RequirementPanelProps {
  projectId: string;
  /** 当前图产物 id：绑定到具体图，避免结果与图版本错位 */
  analysisId?: string;
  /** 点击匹配的页面节点时，联动工作台的图选中 */
  onSelectNode: (nodeId: string) => void;
}

/** 需求定位面板：需求 → 功能点召回 → AI 判定（可 dry-run），产物落 RequirementMap。 */
export function RequirementPanel({ projectId, analysisId, onSelectNode }: RequirementPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [requirement, setRequirement] = useState("");
  // 默认 dry-run：先看召回质量，再决定要不要烧一次模型
  const [dryRun, setDryRun] = useState(true);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  const locate = useMutation({
    mutationFn: () =>
      api.startRequirementLocate(projectId, {
        requirement: requirement.trim(),
        dryRun,
        ...(analysisId ? { analysisId } : {}),
      }),
    onSuccess: (task) => setActiveTaskId(task.id),
  });

  const taskQuery = useRequirementTask(activeTaskId);
  const task = taskQuery.data;
  const taskActive = task?.status === "queued" || task?.status === "running";

  const mapQuery = useQuery({
    queryKey: ["requirement-map", activeTaskId],
    queryFn: () => api.getRequirementMap(activeTaskId as string),
    enabled: task?.status === "succeeded",
  });

  const canSubmit = requirement.trim().length > 0 && !locate.isPending && !taskActive;

  return (
    <div className="shrink-0 border-b border-slate-200 bg-white" data-testid="requirement-panel">
      <button
        type="button"
        onClick={() => setExpanded((current) => !current)}
        className="flex w-full items-center gap-1.5 px-4 py-2 text-xs text-slate-600 transition hover:bg-slate-50"
      >
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <LocateFixed size={13} />
        <span className="font-medium">需求定位</span>
        <span className="ml-2 text-[11px] font-normal text-slate-400">
          把需求映射到实现它的页面（先确定性召回，可选 AI 判定）
        </span>
      </button>

      {expanded && (
        <div className="px-4 pb-3">
          <div className="flex items-start gap-2">
            <textarea
              value={requirement}
              onChange={(event) => setRequirement(event.target.value)}
              placeholder="例如：用户列表页增加状态筛选，详情页支持展示审核状态"
              rows={2}
              data-testid="requirement-input"
              className="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 outline-none transition focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
            />
            <div className="flex shrink-0 flex-col items-stretch gap-1.5">
              <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-slate-600">
                <input
                  type="checkbox"
                  checked={dryRun}
                  onChange={(event) => setDryRun(event.target.checked)}
                  data-testid="dry-run-toggle"
                />
                仅确定性召回（dry-run）
              </label>
              <button
                type="button"
                disabled={!canSubmit}
                onClick={() => locate.mutate()}
                data-testid="submit-locate"
                className="flex items-center justify-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {locate.isPending || taskActive ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : dryRun ? (
                  <LocateFixed size={13} />
                ) : (
                  <Sparkles size={13} />
                )}
                {dryRun ? "开始召回" : "AI 判定"}
              </button>
            </div>
          </div>

          {locate.isError && (
            <p className="mt-2 text-[11px] text-rose-600" data-testid="locate-error">
              {locate.error instanceof Error ? locate.error.message : "发起需求定位失败"}
            </p>
          )}

          {taskActive && (
            <p className="mt-2 flex items-center gap-1.5 text-[11px] text-slate-500">
              <Loader2 size={12} className="animate-spin" />
              {dryRun ? "正在召回候选页面…" : "模型判定中（最多几次调用，稍等）…"}
            </p>
          )}

          {task?.status === "failed" && (
            <p
              className="mt-2 flex items-center gap-1.5 text-[11px] text-rose-700"
              data-testid="task-failed"
            >
              <TriangleAlert size={12} />
              需求定位失败（{task.errorCode ?? "unknown"}）：{task.errorMessage ?? "未知错误"}
            </p>
          )}

          {mapQuery.data && (
            <RequirementMapResult map={mapQuery.data} onSelectNode={onSelectNode} />
          )}
        </div>
      )}
    </div>
  );
}

function RequirementMapResult({
  map,
  onSelectNode,
}: {
  map: RequirementMap;
  onSelectNode: (nodeId: string) => void;
}) {
  const candidateName = (nodeId: string) =>
    map.candidates?.find((candidate) => candidate.nodeId === nodeId)?.name ?? nodeId;

  return (
    <div className="mt-2 space-y-2" data-testid="requirement-result">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
        <span
          className="rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-indigo-700"
          data-testid="map-badge"
        >
          {map.dryRun ? "确定性召回" : "AI 判定"}
        </span>
        <span>
          功能点 {map.features.length} · 匹配 {map.matches.length} · 未匹配{" "}
          {map.unmatched?.length ?? 0} · 落选 {map.dropped?.length ?? 0} · 候选{" "}
          {map.candidates?.length ?? 0}
        </span>
      </div>

      {map.matches.length > 0 && (
        <ul className="space-y-1.5" data-testid="match-list">
          {map.matches.map((match) => (
            <li key={`${match.featureId}-${match.pageNodeId}`}>
              <button
                type="button"
                onClick={() => onSelectNode(match.pageNodeId)}
                className="w-full rounded-lg border border-emerald-200 bg-emerald-50/60 px-2.5 py-1.5 text-left text-xs transition hover:border-emerald-300 hover:bg-emerald-50"
                data-testid="match-item"
              >
                <span className="flex items-center gap-1.5 font-medium text-emerald-800">
                  <CircleCheck size={12} className="shrink-0" />
                  {candidateName(match.pageNodeId)}
                  <span className="ml-auto shrink-0 font-mono text-[10px] text-emerald-700">
                    {match.featureId} · 置信度 {match.confidence.toFixed(2)}
                  </span>
                </span>
                {match.quote && (
                  <p className="mt-1 truncate font-mono text-[10px] text-emerald-700">
                    依据：「{match.quote}」
                  </p>
                )}
                {match.reason && (
                  <p className="mt-0.5 text-[10px] text-slate-500">{match.reason}</p>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}

      {(map.unmatched?.length ?? 0) > 0 && (
        <ul className="space-y-1" data-testid="unmatched-list">
          {map.unmatched?.map((item) => (
            <li
              key={item.featureId}
              className="flex items-start gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-xs text-slate-600"
            >
              <CircleX size={12} className="mt-0.5 shrink-0 text-slate-400" />
              <span className="min-w-0">
                <span className="font-mono text-[10px]">{item.featureId}</span> 未定位：
                {item.reason}
              </span>
            </li>
          ))}
        </ul>
      )}

      {(map.dropped?.length ?? 0) > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5" data-testid="dropped-list">
          <p className="flex items-center gap-1.5 text-[11px] font-medium text-amber-800">
            <TriangleAlert size={12} />
            模型输出中被校验拒绝的结论（{map.dropped?.length} 条，未采纳）
          </p>
          <ul className="mt-1 space-y-0.5">
            {map.dropped?.map((dropped, index) => (
              <li key={index} className="text-[10px] text-amber-700">
                [{REASON_LABEL[dropped.reason] ?? dropped.reason}]
                {dropped.nodeId ? ` ${dropped.nodeId}` : ""}
                {dropped.quote ? ` · 引文「${dropped.quote}」` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(map.warnings?.length ?? 0) > 0 && (
        <ul className="space-y-0.5" data-testid="map-warnings">
          {map.warnings?.map((warning, index) => (
            <li
              key={index}
              className={`flex items-start gap-1.5 text-[10px] ${
                warning.severity === "error" ? "text-rose-700" : "text-slate-500"
              }`}
            >
              <span className="font-mono opacity-60">[{warning.code}]</span>
              {warning.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
