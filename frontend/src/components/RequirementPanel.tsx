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
import { useEffect, useState } from "react";

import { api } from "@/api/client";
import { useRequirementTask } from "@/api/queries";
import type { ActionChain, ChainStep, RequirementMap } from "@uicodeanalysis/contracts";

const SIGNAL_LABEL: Record<string, string> = {
  routePath: "路由",
  routeTitle: "路由标题",
  componentName: "组件名",
  uiText: "界面文案",
  apiUrl: "接口",
  propName: "属性名",
  propDescription: "属性说明",
  semanticRank: "AI 名次",
};

const SOURCE_LABEL: Record<string, string> = {
  static: "静态分析",
  runtime: "运行时",
  ai: "AI",
};

const REASON_LABEL: Record<string, string> = {
  node_not_in_candidates: "选了候选集外的节点",
  chain_not_in_candidates: "选了链路集合外的交互",
  quote_not_found: "引文在节点原文中找不到",
  unknown_feature: "功能点 id 不在本次拆解结果里",
  invalid_shape: "字段类型不符",
};

// 链路每一段的角色。起点恒为 element，所以最常被读的就是前两项。
const ROLE_LABEL: Record<string, string> = {
  element: "元素",
  handler: "处理函数",
  state: "状态",
  hook: "Hook",
  api: "接口",
  route: "路由",
};

const VIEW_ROLE_LABEL: Record<string, string> = {
  control: "控件",
  container: "容器",
  text: "文本",
  link: "链接",
};

/** 与 ast-worker 的 isExternalUrl 保持同一口径：外链没有站内路由可跳。 */
const isExternalHref = (href: string) => /^(https?:)?\/\//i.test(href);

/**
 * 候选是怎么进名单的。
 *
 * semanticRank 只由 locate 在 AI 选中页面后追加，所以它的有无就是「AI 也选了」。
 * 两者一致是可信度最高的情形：语义路和词法路独立指向同一页面。
 */
function candidateOrigin(signals: ReadonlyArray<{ kind: string }>): string | null {
  const ai = signals.some((signal) => signal.kind === "semanticRank");
  const lexical = signals.some((signal) => signal.kind !== "semanticRank");
  if (ai && lexical) return "AI + 词法一致";
  if (ai) return "AI 选中";
  return null;
}

interface RequirementPanelProps {
  projectId: string;
  /** 当前图产物 id：绑定到具体图，避免结果与图版本错位 */
  analysisId?: string;
  /** 点击需求匹配时联动选中页面，并可传入后端计算的一跳子图 */
  onSelectNode: (nodeId: string, focusNodeIds?: string[]) => void;
  /** 从 AI 对话页带回的需求草稿：到达即填入输入框 */
  initialRequirement?: string;
}

/** 需求定位面板：需求 → 功能点召回 → AI 判定（可 dry-run），产物落 RequirementMap。 */
export function RequirementPanel({
  projectId,
  analysisId,
  onSelectNode,
  initialRequirement,
}: RequirementPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [requirement, setRequirement] = useState("");

  // AI 对话草稿到达时自动展开面板并填入（草稿非空才触发，手动清空不受影响）
  useEffect(() => {
    if (!initialRequirement) return;
    setRequirement(initialRequirement);
    setExpanded(true);
  }, [initialRequirement]);

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
    <div className="shrink-0 border-b border-white/[0.09] bg-[#151b25]/70" data-testid="requirement-panel">
      <button
        type="button"
        onClick={() => setExpanded((current) => !current)}
        className="flex w-full items-center gap-1.5 px-4 py-2 text-xs text-slate-300 transition hover:bg-white/[0.03] hover:text-slate-200"
      >
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <LocateFixed size={13} />
        <span className="font-medium text-slate-200">需求定位</span>
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
              className="app-input min-w-0 flex-1 text-xs"
            />
            <div className="flex shrink-0 flex-col items-stretch gap-1.5">
              <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-slate-300">
                <input
                  type="checkbox"
                  checked={dryRun}
                  onChange={(event) => setDryRun(event.target.checked)}
                  data-testid="dry-run-toggle"
                  className="accent-indigo-500"
                />
                仅确定性召回（dry-run）
              </label>
              <button
                type="button"
                disabled={!canSubmit}
                onClick={() => locate.mutate()}
                data-testid="submit-locate"
                className="app-btn-primary"
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
            <p className="mt-2 text-[11px] text-rose-300" data-testid="locate-error">
              {locate.error instanceof Error ? locate.error.message : "发起需求定位失败"}
            </p>
          )}

          {taskActive && (
            <p className="mt-2 flex items-center gap-1.5 text-[11px] text-slate-300">
              <Loader2 size={12} className="animate-spin" />
              {dryRun ? "正在召回候选页面…" : "模型判定中（最多几次调用，稍等）…"}
            </p>
          )}

          {task?.status === "failed" && (
            <p
              className="mt-2 flex items-center gap-1.5 text-[11px] text-rose-300"
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
  onSelectNode: (nodeId: string, focusNodeIds?: string[]) => void;
}) {
  const candidateName = (nodeId: string) =>
    map.candidates?.find((candidate) => candidate.nodeId === nodeId)?.name ?? nodeId;

  // 链路按 id 索引：一条链路可能被多个功能点命中，不该重复渲染
  const chainById = new Map<string, ActionChain>(
    (map.interactionChains ?? []).map((chain) => [chain.id, chain]),
  );

  // 一条链路可能被多个功能点命中，命中节点取并集后才好在链路视图里标出来
  const targetsByChain = new Map<string, Set<string>>();
  for (const match of map.matches) {
    if (!match.chainId) continue;
    const targets = targetsByChain.get(match.chainId) ?? new Set<string>();
    for (const target of match.targets ?? []) targets.add(target);
    targetsByChain.set(match.chainId, targets);
  }

  return (
    <div className="mt-2 space-y-2" data-testid="requirement-result">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
        <span
          className="rounded border border-indigo-400/25 bg-indigo-400/10 px-1.5 py-0.5 text-indigo-300"
          data-testid="map-badge"
        >
          {map.dryRun ? "确定性召回" : "AI 判定"}
        </span>
        <span>
          功能点 {map.features.length} · 匹配 {map.matches.length} · 未匹配{" "}
          {map.unmatched?.length ?? 0} · 落选 {map.dropped?.length ?? 0} · 候选{" "}
          {map.candidates?.length ?? 0}
          {(map.interactionChains?.length ?? 0) > 0
            ? ` · 链路 ${map.interactionChains?.length}`
            : ""}
        </span>
      </div>

      {map.candidates && map.candidates.length > 0 && (
        <section data-testid="candidate-list">
          <h3 className="text-[11px] font-medium text-slate-300">召回候选与命中信号</h3>
          <ul className="mt-1 max-h-48 space-y-1 overflow-y-auto pr-1">
            {map.candidates.map((candidate) => {
              const origin = candidateOrigin(candidate.signals);
              return (
              <li
                key={candidate.nodeId}
                className="rounded-lg border border-white/[0.07] bg-white/[0.02] px-2.5 py-1.5"
              >
                <div className="flex items-start gap-2 text-[11px]">
                  <span className="min-w-0 flex-1 font-medium text-slate-200">{candidate.name}</span>
                  {origin && (
                    <span
                      className={`shrink-0 rounded px-1 text-[10px] ${
                        origin === "AI + 词法一致"
                          ? "border border-emerald-400/30 text-emerald-200"
                          : "border border-indigo-400/30 text-indigo-200"
                      }`}
                      data-testid="candidate-origin"
                    >
                      {origin}
                    </span>
                  )}
                  <span className="shrink-0 font-mono text-slate-400">
                    总分 {candidate.score.toFixed(2)}
                  </span>
                </div>
                <p className="mt-0.5 break-all font-mono text-[10px] text-slate-500">
                  {candidate.file ?? candidate.nodeId}
                  {candidate.routeId ? ` · ${candidate.routeId}` : ""}
                </p>
                {candidate.signals.length > 0 ? (
                  <ul className="mt-1 space-y-0.5">
                    {candidate.signals.map((signal, index) => (
                      <li
                        key={`${signal.kind}-${signal.value}-${index}`}
                        className="flex gap-1.5 text-[10px] text-indigo-200/90"
                      >
                        <span className="shrink-0 text-slate-400">
                          {SIGNAL_LABEL[signal.kind] ?? signal.kind}
                        </span>
                        <span className="min-w-0 flex-1 break-all">{signal.value}</span>
                        <span className="shrink-0 font-mono text-slate-400">
                          +{signal.score.toFixed(2)}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-1 text-[10px] text-slate-500">没有可展示的命中信号</p>
                )}
              </li>
              );
            })}
          </ul>
        </section>
      )}

      {map.matches.length > 0 && (
        <ul className="space-y-1.5" data-testid="match-list">
          {map.matches.map((match) => {
            const chain = match.chainId ? chainById.get(match.chainId) : undefined;
            // 有链路就喂链路的节点集：比整页子图小得多，聚焦后能直接看到这条动作路径
            const focusIds = chain?.path.nodeIds.length
              ? chain.path.nodeIds
              : match.subgraph?.nodeIds;
            return (
              <li key={`${match.featureId}-${match.pageNodeId}-${match.chainId ?? ""}`}>
                <button
                  type="button"
                  onClick={() => onSelectNode(match.pageNodeId, focusIds)}
                  className="w-full rounded-lg border border-emerald-400/25 bg-emerald-400/[0.08] px-2.5 py-1.5 text-left text-xs transition hover:border-emerald-400/40 hover:bg-emerald-400/[0.12]"
                  data-testid="match-item"
                >
                  <span className="flex items-center gap-1.5 font-medium text-emerald-200">
                    <CircleCheck size={12} className="shrink-0" />
                    {candidateName(match.pageNodeId)}
                    <span className="ml-auto shrink-0 font-mono text-[10px] text-emerald-300/80">
                      {match.featureId} · 置信度 {match.confidence.toFixed(2)}
                    </span>
                  </span>
                  {chain && (
                    <p
                      className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-emerald-200"
                      data-testid="match-chain"
                    >
                      <span className="shrink-0 text-emerald-300/70">链路</span>
                      <span className="font-mono">{chain.text ?? chain.element.name}</span>
                      {chain.href && (
                        <span className="break-all font-mono text-[10px] text-emerald-300/80">
                          href={chain.href}
                        </span>
                      )}
                      {chain.terminatedAt && (
                        <span className="rounded border border-amber-400/30 px-1 text-[10px] text-amber-200">
                          中断：{chain.terminatedAt}
                        </span>
                      )}
                    </p>
                  )}
                  {match.quote && (
                    <p className="mt-1 truncate font-mono text-[10px] text-emerald-300/80">
                      依据：「{match.quote}」
                    </p>
                  )}
                  {match.verifiedEvidence && match.verifiedEvidence.length > 0 ? (
                    <ul className="mt-1 space-y-0.5" data-testid="verified-evidence">
                      {match.verifiedEvidence.map((evidence, index) => (
                        <li
                          key={`${evidence.nodeId}-${evidence.via ?? "evidence"}-${index}`}
                          className="break-all font-mono text-[10px] text-emerald-300/80"
                        >
                          已核验证据 [{SOURCE_LABEL[evidence.source] ?? evidence.source}]
                          {evidence.via ? ` ${SIGNAL_LABEL[evidence.via] ?? evidence.via}` : ""}
                          {evidence.file ? ` · ${evidence.file}` : ""}
                          {evidence.loc ? `:${evidence.loc.line}:${evidence.loc.col}` : ""}
                          {`：「${evidence.quote}」`}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-1 text-[10px] text-amber-200/80" data-testid="no-verified-evidence">
                      暂无可定位证据
                    </p>
                  )}
                  {match.reason && (
                    <p className="mt-0.5 text-[10px] text-slate-300">{match.reason}</p>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {(map.interactionChains?.length ?? 0) > 0 && (
        <section data-testid="chain-list">
          <h3 className="text-[11px] font-medium text-slate-300">
            命中链路的逐段路径（点击任意段可在图里定位）
          </h3>
          <ul className="mt-1 space-y-1.5">
            {map.interactionChains?.map((chain) => (
              <li key={chain.id}>
                <ChainView
                  chain={chain}
                  targets={targetsByChain.get(chain.id)}
                  pageName={candidateName(chain.pageId)}
                  onSelectNode={onSelectNode}
                />
              </li>
            ))}
          </ul>
        </section>
      )}

      {(map.unmatched?.length ?? 0) > 0 && (
        <ul className="space-y-1" data-testid="unmatched-list">
          {map.unmatched?.map((item) => (
            <li
              key={item.featureId}
              className="flex items-start gap-1.5 rounded-lg border border-white/[0.07] bg-white/[0.02] px-2.5 py-1.5 text-xs text-slate-300"
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
        <div className="rounded-lg border border-amber-400/20 bg-amber-400/[0.08] px-2.5 py-1.5" data-testid="dropped-list">
          <p className="flex items-center gap-1.5 text-[11px] font-medium text-amber-200">
            <TriangleAlert size={12} />
            模型输出中被校验拒绝的结论（{map.dropped?.length} 条，未采纳）
          </p>
          <ul className="mt-1 space-y-0.5">
            {map.dropped?.map((dropped, index) => (
              <li key={index} className="text-[10px] text-amber-300/90">
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
                warning.severity === "error" ? "text-rose-300" : "text-slate-400"
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

/**
 * 一条命中链路的逐段路径。
 *
 * 链路是确定性推导出来的（后端 chains.py），所以这里只负责呈现，不做任何推断：
 * 段序、断点、href 都照抄产物。`terminatedAt` 非空时显式标出中断——宁可在
 * 「外链跳走了」处停下，也不给人一条看起来能走通的假路径。
 */
function ChainView({
  chain,
  targets,
  pageName,
  onSelectNode,
}: {
  chain: ActionChain;
  targets?: Set<string>;
  pageName: string;
  onSelectNode: (nodeId: string, focusNodeIds?: string[]) => void;
}) {
  return (
    <div
      className="rounded-lg border border-white/[0.09] bg-white/[0.02] px-2.5 py-1.5"
      data-testid="chain-view"
    >
      <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
        <span className="font-medium text-slate-200">{pageName}</span>
        <span className="font-mono text-slate-500">·</span>
        <span className="font-mono text-slate-300">{chain.text ?? chain.element.name}</span>
        {chain.viewRole && (
          <span className="rounded border border-white/[0.12] px-1 text-[10px] text-slate-400">
            {VIEW_ROLE_LABEL[chain.viewRole] ?? chain.viewRole}
          </span>
        )}
        {chain.href && (
          <span
            className={`break-all font-mono text-[10px] ${
              isExternalHref(chain.href) ? "text-amber-200" : "text-slate-400"
            }`}
          >
            {chain.href}
            {isExternalHref(chain.href) ? "（外链）" : ""}
          </span>
        )}
      </div>

      <ol className="mt-1 space-y-0.5 border-l border-white/[0.12] pl-2.5">
        {chain.steps.map((step, index) => (
          <li key={`${step.nodeId}-${index}`}>
            <ChainStepRow
              step={step}
              isTarget={targets?.has(step.nodeId) ?? false}
              onClick={() => onSelectNode(step.nodeId, chain.path.nodeIds)}
            />
          </li>
        ))}
      </ol>

      {chain.terminatedAt && (
        <p className="mt-1 text-[10px] text-amber-200" data-testid="chain-terminated">
          ⟂ 链路在此中断：{chain.terminatedAt}
        </p>
      )}
    </div>
  );
}

function ChainStepRow({
  step,
  isTarget,
  onClick,
}: {
  step: ChainStep;
  isTarget: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid="chain-step"
      className={`flex w-full items-baseline gap-1.5 rounded px-1 py-0.5 text-left text-[10px] transition hover:bg-white/[0.05] ${
        isTarget ? "text-emerald-200" : "text-slate-300"
      }`}
    >
      <span className="w-10 shrink-0 text-right text-slate-500">
        {ROLE_LABEL[step.role] ?? step.role}
      </span>
      <span className="min-w-0 flex-1 truncate font-mono">
        {isTarget && "▸ "}
        {step.name}
      </span>
      {step.file && (
        <span className="shrink-0 truncate font-mono text-slate-500">
          {step.file}
          {step.loc ? `:${step.loc.line}:${step.loc.col}` : ""}
        </span>
      )}
    </button>
  );
}
