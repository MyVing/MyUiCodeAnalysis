import { useMutation } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  CircleCheck,
  Compass,
  Download,
  Loader2,
  Minus,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { useState } from "react";

import { api } from "@/api/client";
import { useExploreArtifact, useExploreTask } from "@/api/queries";
import type { DiscoveredElement, ExploreArtifact, ExploreStepResult } from "@uicodeanalysis/contracts";

const STEP_RESULT_STYLE: Record<string, string> = {
  success: "border-emerald-400/30 text-emerald-200",
  failed: "border-rose-400/40 text-rose-200",
  skipped: "border-slate-400/30 text-slate-400",
  no_change: "border-amber-400/30 text-amber-200",
};

const STEP_RESULT_LABEL: Record<string, string> = {
  success: "成功",
  failed: "失败",
  skipped: "跳过",
  no_change: "无可见变化",
};

const PRIORITY_LABEL: Record<string, string> = {
  p0: "P0",
  p1: "P1",
  p2: "P2",
  p3: "P3",
};

/** 步骤的可读描述：动作 + 目标（selector 摘要）+ 值。 */
function stepLabel(result: ExploreStepResult): string {
  const selectorText = result.selector
    ? `${result.selector.strategy}:${result.selector.value}`
    : (result.target ?? "");
  const valueText = result.valueSummary ? ` = ${result.valueSummary}` : "";
  return `${result.action} ${selectorText}${valueText}`;
}

interface ExplorePanelProps {
  projectId: string;
}

/** AI 自动探索面板：目标 → discovery → 计划预生成 →（可选）确定性执行。 */
export function ExplorePanel({ projectId }: ExplorePanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [goal, setGoal] = useState("");
  const [command, setCommand] = useState("npm run dev");
  const [url, setUrl] = useState("");
  // 默认 dry-run：先审阅计划质量，再决定是否真的执行
  const [dryRun, setDryRun] = useState(true);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  const explore = useMutation({
    mutationFn: () => {
      const trimmedCommand = command.trim();
      const trimmedUrl = url.trim();
      return api.startExplore(projectId, {
        goal: goal.trim(),
        dryRun,
        ...(trimmedCommand ? { command: trimmedCommand } : {}),
        ...(trimmedUrl ? { url: trimmedUrl } : {}),
      });
    },
    onSuccess: (task) => setActiveTaskId(task.id),
  });

  const taskQuery = useExploreTask(activeTaskId);
  const task = taskQuery.data;
  const taskActive = task?.status === "queued" || task?.status === "running";

  const artifactQuery = useExploreArtifact(task?.status === "succeeded" ? task.id : null);
  const artifact = artifactQuery.data;

  const canSubmit =
    goal.trim().length > 0 &&
    (command.trim().length > 0 || url.trim().length > 0) &&
    !explore.isPending &&
    !taskActive;

  return (
    <div className="shrink-0 border-b border-white/[0.09] bg-[#151b25]/70" data-testid="explore-panel">
      <button
        type="button"
        onClick={() => setExpanded((current) => !current)}
        data-testid="explore-panel-toggle"
        className="flex w-full items-center gap-1.5 px-4 py-2 text-xs text-slate-300 transition hover:bg-white/[0.03] hover:text-slate-200"
      >
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <Compass size={13} />
        <span className="font-medium text-slate-200">AI 自动探索</span>
        <span className="ml-2 text-[11px] font-normal text-slate-400">
          枚举页面真实元素 → 预生成确定性计划 → 执行产出轨迹、场景与 Playwright 脚本
        </span>
      </button>

      {expanded && (
        <div className="px-4 pb-3">
          <div className="flex items-start gap-2">
            <textarea
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              placeholder="例如：验证订单列表的搜索与状态筛选交互"
              rows={2}
              data-testid="explore-goal-input"
              className="app-input min-w-0 flex-1 text-xs"
            />
            <div className="flex shrink-0 flex-col items-stretch gap-1.5">
              <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-slate-300">
                <input
                  type="checkbox"
                  checked={dryRun}
                  onChange={(event) => setDryRun(event.target.checked)}
                  data-testid="explore-dry-run-toggle"
                  className="accent-indigo-500"
                />
                仅生成计划（dry-run）
              </label>
              <button
                type="button"
                disabled={!canSubmit}
                onClick={() => explore.mutate()}
                data-testid="explore-submit"
                className="app-btn-primary"
              >
                {explore.isPending || taskActive ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : dryRun ? (
                  <Compass size={13} />
                ) : (
                  <Sparkles size={13} />
                )}
                {dryRun ? "生成探索计划" : "开始探索"}
              </button>
            </div>
          </div>

          <div className="mt-2 grid gap-2 md:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-slate-400">
                启动命令（托管 dev server，探索完自动停止）
              </span>
              <input
                value={command}
                onChange={(event) => setCommand(event.target.value)}
                placeholder="npm run dev"
                data-testid="explore-command-input"
                className="app-input font-mono text-xs"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-slate-400">或页面地址（已在运行的页面）</span>
              <input
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="http://localhost:5199/"
                data-testid="explore-url-input"
                className="app-input font-mono text-xs"
              />
            </label>
          </div>

          {explore.isError && (
            <p className="mt-2 text-[11px] text-rose-300" data-testid="explore-error">
              {explore.error instanceof Error ? explore.error.message : "发起探索失败"}
            </p>
          )}

          {taskActive && (
            <p className="mt-2 flex items-center gap-1.5 text-[11px] text-slate-300">
              <Loader2 size={12} className="animate-spin" />
              {dryRun ? "正在枚举页面元素并生成计划…" : "正在执行探索计划（逐步截图）…"}
            </p>
          )}

          {task?.status === "failed" && (
            <p
              className="mt-2 flex items-center gap-1.5 text-[11px] text-rose-300"
              data-testid="explore-failed"
            >
              <TriangleAlert size={12} />
              探索失败（{task.errorCode ?? "unknown"}）：{task.errorMessage ?? "未知错误"}
            </p>
          )}

          {task?.status === "succeeded" && (
            <p
              className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-300"
              data-testid="explore-summary"
            >
              <span className="flex items-center gap-1 text-emerald-300">
                <CircleCheck size={12} />
                探索完成{task.dryRun ? "（dry-run，仅计划）" : ""}
              </span>
              <span>元素 {task.elementCount ?? 0}</span>
              <span>步骤 {task.stepCount ?? 0}</span>
              {!task.dryRun && (
                <>
                  <span className="text-emerald-300">成功 {task.successCount ?? 0}</span>
                  <span className={task.failedCount ? "text-rose-300" : ""}>
                    失败 {task.failedCount ?? 0}
                  </span>
                  <span>截图 {task.screenshotCount ?? 0}</span>
                </>
              )}
              <a
                href={api.exploreScriptUrl(task.id)}
                download
                className="flex items-center gap-1 text-indigo-300 underline-offset-2 hover:underline"
                data-testid="explore-script-download"
              >
                <Download size={12} />
                下载 Playwright 脚本
              </a>
            </p>
          )}

          {artifact && <ExploreArtifactView artifact={artifact} />}
        </div>
      )}
    </div>
  );
}

function ExploreArtifactView({ artifact }: { artifact: ExploreArtifact }) {
  return (
    <div className="mt-2 space-y-2" data-testid="explore-result">
      {artifact.warnings.length > 0 && (
        <ul className="space-y-0.5">
          {artifact.warnings.map((warning, index) => (
            <li
              key={index}
              className={`flex items-start gap-1.5 text-[10px] ${
                warning.severity === "error"
                  ? "text-rose-300"
                  : warning.severity === "warning"
                    ? "text-amber-200"
                    : "text-slate-400"
              }`}
            >
              <TriangleAlert size={11} className="mt-0.5 shrink-0" />
              <span className="font-mono opacity-60">[{warning.code}]</span>
              <span className="min-w-0">{warning.message}</span>
            </li>
          ))}
        </ul>
      )}

      <section>
        <h3 className="text-[11px] font-medium text-slate-300">探索计划</h3>
        {artifact.plan.summary && (
          <p className="mt-0.5 text-[10px] text-slate-400">{artifact.plan.summary}</p>
        )}
        <ol className="mt-1 space-y-1">
          {artifact.plan.steps.map((step) => (
            <li
              key={step.index}
              className="rounded-lg border border-white/[0.07] bg-white/[0.02] px-2.5 py-1.5"
              data-testid="explore-plan-step"
            >
              <div className="flex items-baseline gap-1.5 text-[11px]">
                <span className="w-8 shrink-0 text-right font-mono text-slate-500">
                  #{step.index}
                </span>
                <span className="shrink-0 rounded border border-indigo-400/30 px-1 font-mono text-[10px] text-indigo-200">
                  {step.action}
                </span>
                <span className="min-w-0 flex-1 break-all text-slate-300">
                  {step.targetIndex !== undefined ? `元素 #${step.targetIndex}` : ""}
                  {step.value ? ` · 值「${step.value}」` : ""}
                  {step.assertion ? ` · 断言「${step.assertion}」` : ""}
                  {step.optional ? " · 可选" : ""}
                </span>
              </div>
              {step.reasoning && (
                <p className="mt-0.5 pl-10 text-[10px] text-slate-500">{step.reasoning}</p>
              )}
            </li>
          ))}
        </ol>
      </section>

      {artifact.results.length > 0 && (
        <section>
          <h3 className="text-[11px] font-medium text-slate-300">执行轨迹（逐步结果与截图）</h3>
          <ol className="mt-1 space-y-1">
            {artifact.results.map((result) => {
              const screenshot = artifact.screenshots.find(
                (shot) => shot.stepIndex === result.index,
              );
              return (
                <li
                  key={result.index}
                  className="rounded-lg border border-white/[0.07] bg-white/[0.02] px-2.5 py-1.5"
                  data-testid="explore-step-result"
                >
                  <div className="flex items-baseline gap-1.5 text-[11px]">
                    <span className="w-8 shrink-0 text-right font-mono text-slate-500">
                      #{result.index}
                    </span>
                    <span
                      className={`shrink-0 rounded border px-1 font-mono text-[10px] ${
                        STEP_RESULT_STYLE[result.result] ?? "border-white/20 text-slate-300"
                      }`}
                    >
                      {STEP_RESULT_LABEL[result.result] ?? result.result}
                    </span>
                    <span className="min-w-0 flex-1 break-all text-slate-300">
                      {stepLabel(result)}
                    </span>
                  </div>
                  {result.error && (
                    <p className="mt-0.5 break-all pl-10 text-[10px] text-rose-300">
                      {result.error}
                    </p>
                  )}
                  {result.triggeredNetworkEvents && result.triggeredNetworkEvents.length > 0 && (
                    <p className="mt-0.5 truncate pl-10 font-mono text-[10px] text-slate-500">
                      网络 {result.triggeredNetworkEvents.length} 条 ·{" "}
                      {result.triggeredNetworkEvents[0]}
                    </p>
                  )}
                  {screenshot && (
                    <a
                      href={api.exploreScreenshotUrl(artifact.taskId, screenshot.fileName)}
                      target="_blank"
                      rel="noreferrer"
                      className="mt-1 ml-10 inline-block"
                      data-testid="explore-screenshot-link"
                    >
                      <img
                        src={api.exploreScreenshotUrl(artifact.taskId, screenshot.fileName)}
                        alt={`步骤 ${result.index} 截图`}
                        className="max-h-28 rounded border border-white/[0.12] object-cover"
                        loading="lazy"
                      />
                    </a>
                  )}
                </li>
              );
            })}
          </ol>
        </section>
      )}

      <section>
        <h3 className="text-[11px] font-medium text-slate-300">场景与用例清单</h3>
        <div className="mt-1 rounded-lg border border-white/[0.07] bg-white/[0.02] px-2.5 py-1.5">
          <p className="text-[11px] font-medium text-slate-200" data-testid="explore-scenario-title">
            {artifact.scenario.title}
          </p>
          {artifact.scenario.description && (
            <p className="mt-0.5 text-[10px] text-slate-400">{artifact.scenario.description}</p>
          )}
          {artifact.scenario.preconditions && artifact.scenario.preconditions.length > 0 && (
            <p className="mt-0.5 text-[10px] text-slate-500">
              前置：{artifact.scenario.preconditions.join("；")}
            </p>
          )}
        </div>
        {artifact.testCases.length > 0 && (
          <ul className="mt-1 space-y-1">
            {artifact.testCases.map((testCase) => (
              <li
                key={testCase.title}
                className="rounded-lg border border-white/[0.07] bg-white/[0.02] px-2.5 py-1.5"
                data-testid="explore-test-case"
              >
                <div className="flex items-baseline gap-1.5 text-[11px]">
                  {testCase.priority && (
                    <span className="shrink-0 rounded border border-amber-400/30 px-1 text-[10px] text-amber-200">
                      {PRIORITY_LABEL[testCase.priority] ?? testCase.priority}
                    </span>
                  )}
                  <span className="min-w-0 flex-1 font-medium text-slate-200">
                    {testCase.title}
                  </span>
                </div>
                {testCase.steps.length > 0 && (
                  <ol className="mt-1 list-inside list-decimal space-y-0.5 pl-1 text-[10px] text-slate-400">
                    {testCase.steps.map((step, index) => (
                      <li key={index}>{step}</li>
                    ))}
                  </ol>
                )}
                {testCase.expected.length > 0 && (
                  <ul className="mt-0.5 space-y-0.5 text-[10px] text-emerald-300/80">
                    {testCase.expected.map((expectation, index) => (
                      <li key={index} className="flex items-start gap-1">
                        <CircleCheck size={10} className="mt-0.5 shrink-0" />
                        {expectation}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {artifact.discovery.length > 0 && (
        <section>
          <h3 className="text-[11px] font-medium text-slate-300">
            页面可交互元素（{artifact.discovery.length}）
          </h3>
          <ElementList elements={artifact.discovery} />
        </section>
      )}
    </div>
  );
}

function ElementList({ elements }: { elements: DiscoveredElement[] }) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? elements : elements.slice(0, 8);
  return (
    <>
      <ul className="mt-1 space-y-0.5">
        {shown.map((element) => (
          <li
            key={element.index}
            className="flex items-baseline gap-1.5 text-[10px] text-slate-400"
            data-testid="explore-element"
          >
            <span className="w-8 shrink-0 text-right font-mono text-slate-500">
              #{element.index}
            </span>
            <span className="shrink-0 rounded border border-white/[0.12] px-1 font-mono text-slate-300">
              {element.role}
            </span>
            <span className="min-w-0 flex-1 truncate">{element.name || "（无名称）"}</span>
            <span className="shrink-0 font-mono">&lt;{element.tag}&gt;</span>
          </li>
        ))}
      </ul>
      {elements.length > 8 && (
        <button
          type="button"
          onClick={() => setExpanded((current) => !current)}
          className="mt-1 flex items-center gap-1 text-[10px] text-indigo-300 hover:underline"
        >
          {expanded ? <Minus size={10} /> : <ChevronRight size={10} />}
          {expanded ? "收起" : `展开全部 ${elements.length} 个元素`}
        </button>
      )}
    </>
  );
}
