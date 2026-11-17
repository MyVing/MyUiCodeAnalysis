import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  Database,
  Loader2,
  Radar,
  RotateCw,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "@/api/client";
import { useAnalysisTask, useProject, useProjectGraph, useRuntimeTask } from "@/api/queries";
import type { AnalysisStart, Framework, RuntimeStart } from "@/api/types";
import { GraphSidebar } from "@/components/GraphSidebar";
import { NodeInspector } from "@/components/NodeInspector";
import { WarningsBanner } from "@/components/WarningsBanner";
import { GraphCanvas } from "@/graph/GraphCanvas";
import type { NodeKind } from "@uicodeanalysis/contracts";

const FRAMEWORK_LABEL: Record<Framework, string> = {
  react: "React Web",
  next: "Next.js RSC",
};

const INPUT_CLASS =
  "w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 outline-none transition focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100";

export default function WorkbenchPage() {
  const params = useParams();
  const projectId = params.projectId ?? "";
  const projectQuery = useProject(projectId);
  const graphQuery = useProjectGraph(projectId);
  const queryClient = useQueryClient();

  const graph = graphQuery.data;
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [activeKinds, setActiveKinds] = useState<Set<NodeKind>>(new Set());
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [options, setOptions] = useState<AnalysisStart>({
    requirement: "",
    baseCommit: "",
    targetCommit: "",
  });
  // 运行时采集：command（托管启动）与 url（已运行页面）二选一
  const [showRuntime, setShowRuntime] = useState(false);
  const [runtimeOptions, setRuntimeOptions] = useState<RuntimeStart>({
    command: "npm run dev",
    url: "",
    settleMs: 3000,
  });
  const [activeRuntimeTaskId, setActiveRuntimeTaskId] = useState<string | null>(null);

  useEffect(() => {
    if (!graph) {
      return;
    }
    setActiveKinds(new Set(graph.nodes.map((node) => node.kind)));
    setSelectedNodeId(graph.nodes[0]?.id ?? null);
  }, [graph]);

  const analysis = useMutation({
    mutationFn: () => {
      const payload: AnalysisStart = {};
      const requirement = (options.requirement ?? "").trim();
      const baseCommit = (options.baseCommit ?? "").trim();
      const targetCommit = (options.targetCommit ?? "").trim();
      if (requirement) payload.requirement = requirement;
      if (baseCommit) payload.baseCommit = baseCommit;
      if (targetCommit) payload.targetCommit = targetCommit;
      const hasOptions = Object.keys(payload).length > 0;
      return api.startAnalysis(projectId, hasOptions ? payload : undefined);
    },
    onMutate: () => setActiveTaskId(null),
    onSuccess: (task) => setActiveTaskId(task.id),
  });

  const taskQuery = useAnalysisTask(activeTaskId);
  const task = taskQuery.data;
  const taskActive = task?.status === "queued" || task?.status === "running";

  useEffect(() => {
    if (task?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId, "graph"] });
    }
  }, [task?.status, projectId, queryClient]);

  const runtimeCollect = useMutation({
    mutationFn: () => {
      const payload: RuntimeStart = {};
      const command = (runtimeOptions.command ?? "").trim();
      const url = (runtimeOptions.url ?? "").trim();
      const settleMs = runtimeOptions.settleMs;
      if (command) payload.command = command;
      if (url) payload.url = url;
      if (settleMs && settleMs > 0) payload.settleMs = settleMs;
      return api.startRuntimeCollect(projectId, payload);
    },
    onSuccess: (collectTask) => setActiveRuntimeTaskId(collectTask.id),
  });

  const runtimeTaskQuery = useRuntimeTask(activeRuntimeTaskId);
  const runtimeTask = runtimeTaskQuery.data;
  const runtimeActive = runtimeTask?.status === "queued" || runtimeTask?.status === "running";

  useEffect(() => {
    // 采集成功后后端已自动产出合并图（kind=merged），刷新即可看到运行时证据
    if (runtimeTask?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId, "graph"] });
    }
  }, [runtimeTask?.status, projectId, queryClient]);

  const visibleNodeIds = useMemo(() => {
    if (!graph) {
      return new Set<string>();
    }
    const keyword = search.trim().toLowerCase();

    return new Set(
      graph.nodes
        .filter((node) => {
          if (!activeKinds.has(node.kind)) {
            return false;
          }
          if (!keyword) {
            return true;
          }
          const file = node.file ?? "";
          return [node.name, node.id, file, node.kind].join(" ").toLowerCase().includes(keyword);
        })
        .map((node) => node.id),
    );
  }, [graph, search, activeKinds]);

  const selectedNode = graph?.nodes.find((node) => node.id === selectedNodeId);
  const project = projectQuery.data;

  function toggleKind(kind: NodeKind) {
    setActiveKinds((current) => {
      const next = new Set(current);
      if (next.has(kind)) {
        next.delete(kind);
      } else {
        next.add(kind);
      }
      return next;
    });
  }

  if (projectQuery.isError) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-slate-50 px-6 text-center">
        <TriangleAlert size={24} className="text-rose-500" />
        <p className="text-sm font-medium text-slate-800">无法打开项目</p>
        <p className="max-w-md text-xs text-slate-500">
          {projectQuery.error instanceof Error ? projectQuery.error.message : "项目不存在"}
        </p>
        <div className="mt-2 flex gap-2">
          <Link
            to="/"
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
          >
            返回项目列表
          </Link>
          <button
            type="button"
            onClick={() => void projectQuery.refetch()}
            className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-700"
          >
            重试
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-slate-50">
      <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-slate-200 bg-white px-4">
        <div className="flex min-w-0 items-center gap-3">
          <Link
            to="/"
            className="flex shrink-0 items-center gap-1 rounded-lg border border-slate-200 px-2 py-1 text-xs text-slate-600 transition hover:bg-slate-50"
          >
            <ArrowLeft size={14} />
            项目
          </Link>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="truncate text-sm font-semibold text-slate-900">
                {project?.name ?? "加载中…"}
              </h1>
              {project && (
                <span className="rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">
                  {FRAMEWORK_LABEL[project.framework]}
                </span>
              )}
              {/* 徽标跟着图走：真实分析产物的 meta.sample 为空，标记自动消失 */}
              {graph?.meta?.sample ? (
                <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">
                  示例 IR
                </span>
              ) : null}
            </div>
            <p className="truncate font-mono text-[10px] text-slate-400">
              {project?.rootPath ?? projectId}
            </p>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <span className="hidden items-center gap-1 text-[11px] text-slate-400 md:flex">
            <Database size={12} />
            内存存储 · 重启清空
          </span>
          <button
            type="button"
            onClick={() => setShowRuntime((current) => !current)}
            className="flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50"
          >
            {showRuntime ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <Radar size={13} />
            运行时采集
          </button>
          <button
            type="button"
            onClick={() => setShowAdvanced((current) => !current)}
            className="flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50"
          >
            {showAdvanced ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            高级选项
          </button>
          <button
            type="button"
            onClick={() => analysis.mutate()}
            disabled={analysis.isPending || taskActive}
            className="flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {analysis.isPending || taskActive ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <Sparkles size={13} />
            )}
            开始分析
          </button>
        </div>
      </header>

      {showAdvanced && (
        <div className="shrink-0 border-b border-slate-200 bg-white px-4 py-3">
          <div className="grid gap-3 md:grid-cols-3">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium text-slate-500">
                需求描述（可选，AI 语义层预留）
              </span>
              <textarea
                value={options.requirement}
                onChange={(event) =>
                  setOptions((current) => ({ ...current, requirement: event.target.value }))
                }
                placeholder="用一句话描述本次要分析的需求，例如：用户列表页增加状态筛选"
                rows={2}
                className={INPUT_CLASS}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium text-slate-500">
                Base Commit（Diff 分析预留）
              </span>
              <input
                value={options.baseCommit}
                onChange={(event) =>
                  setOptions((current) => ({ ...current, baseCommit: event.target.value }))
                }
                placeholder="HEAD~1"
                className={INPUT_CLASS}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium text-slate-500">
                Target Commit（Diff 分析预留）
              </span>
              <input
                value={options.targetCommit}
                onChange={(event) =>
                  setOptions((current) => ({ ...current, targetCommit: event.target.value }))
                }
                placeholder="HEAD"
                className={INPUT_CLASS}
              />
            </label>
          </div>
        </div>
      )}

      {showRuntime && (
        <div className="shrink-0 border-b border-slate-200 bg-white px-4 py-3">
          <div className="grid gap-3 md:grid-cols-4">
            <label className="flex flex-col gap-1 md:col-span-2">
              <span className="text-[11px] font-medium text-slate-500">
                启动命令（托管 dev server，采集完自动停止）
              </span>
              <input
                value={runtimeOptions.command ?? ""}
                onChange={(event) =>
                  setRuntimeOptions((current) => ({ ...current, command: event.target.value }))
                }
                placeholder="npm run dev"
                className={INPUT_CLASS}
              />
            </label>
            <label className="flex flex-col gap-1 md:col-span-1">
              <span className="text-[11px] font-medium text-slate-500">
                或页面地址（已在运行的页面）
              </span>
              <input
                value={runtimeOptions.url ?? ""}
                onChange={(event) =>
                  setRuntimeOptions((current) => ({ ...current, url: event.target.value }))
                }
                placeholder="http://localhost:5199/"
                className={INPUT_CLASS}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium text-slate-500">
                静默观察窗口（ms）
              </span>
              <input
                type="number"
                min={100}
                max={120000}
                step={100}
                value={runtimeOptions.settleMs ?? 3000}
                onChange={(event) =>
                  setRuntimeOptions((current) => ({
                    ...current,
                    settleMs: event.target.valueAsNumber,
                  }))
                }
                className={INPUT_CLASS}
              />
            </label>
          </div>
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              disabled={
                runtimeCollect.isPending ||
                runtimeActive ||
                ((runtimeOptions.command ?? "").trim() === "" &&
                  (runtimeOptions.url ?? "").trim() === "")
              }
              onClick={() => runtimeCollect.mutate()}
              className="flex items-center gap-1.5 rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {runtimeCollect.isPending || runtimeActive ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <Radar size={13} />
              )}
              开始采集
            </button>
            <span className="text-[11px] text-slate-400">
              启动浏览器访问页面，记录网络 / Console / 组件挂载，产物与静态图合并
            </span>
          </div>
          {runtimeCollect.isError && (
            <p className="mt-2 text-[11px] text-rose-600">
              {runtimeCollect.error instanceof Error
                ? runtimeCollect.error.message
                : "发起采集失败"}
            </p>
          )}
        </div>
      )}

      {runtimeActive && (
        <div className="flex shrink-0 items-center gap-2 border-b border-slate-300 bg-slate-100 px-4 py-2 text-xs text-slate-700">
          <Loader2 size={13} className="animate-spin" />
          正在运行时采集…（启动 dev server 与浏览器执行中，完成后自动刷新合并图）
        </div>
      )}

      {runtimeTask?.status === "failed" && (
        <div className="flex shrink-0 items-center gap-2 border-b border-rose-200 bg-rose-50 px-4 py-2 text-xs text-rose-800">
          <TriangleAlert size={13} className="shrink-0" />
          <span className="min-w-0 flex-1 truncate">
            采集失败（{runtimeTask.errorCode ?? "unknown"}）：
            {runtimeTask.errorMessage ?? "未知错误"}
          </span>
          <button
            type="button"
            onClick={() => runtimeCollect.mutate()}
            className="shrink-0 rounded border border-rose-300 bg-white px-2 py-0.5 text-[11px] text-rose-700 hover:bg-rose-100"
          >
            重试
          </button>
        </div>
      )}

      {runtimeTask?.status === "succeeded" && (
        <div className="flex shrink-0 items-center gap-2 border-b border-sky-200 bg-sky-50 px-4 py-2 text-xs text-sky-800">
          <Radar size={13} className="shrink-0" />
          采集完成：{runtimeTask.componentCount ?? 0} 组件 · {runtimeTask.networkCount ?? 0} 网络请求
          · {runtimeTask.consoleCount ?? 0} Console · {runtimeTask.interactionCount ?? 0} 交互
          （已并入图谱，节点详情可见运行时证据）
        </div>
      )}

      {taskActive && (
        <div className="flex shrink-0 items-center gap-2 border-b border-indigo-200 bg-indigo-50 px-4 py-2 text-xs text-indigo-800">
          <Loader2 size={13} className="animate-spin" />
          正在分析源码…（ast-worker 子进程执行中，完成后自动刷新图）
        </div>
      )}

      {task?.status === "failed" && (
        <div className="flex shrink-0 items-center gap-2 border-b border-rose-200 bg-rose-50 px-4 py-2 text-xs text-rose-800">
          <TriangleAlert size={13} className="shrink-0" />
          <span className="min-w-0 flex-1 truncate">
            分析失败（{task.errorCode ?? "unknown"}）：
            {task.errorMessage ?? "未知错误"}
          </span>
          <button
            type="button"
            onClick={() => analysis.mutate()}
            className="shrink-0 rounded border border-rose-300 bg-white px-2 py-0.5 text-[11px] text-rose-700 hover:bg-rose-100"
          >
            重试
          </button>
        </div>
      )}

      {task?.status === "succeeded" && (
        <div className="flex shrink-0 items-center gap-2 border-b border-emerald-200 bg-emerald-50 px-4 py-2 text-xs text-emerald-800">
          <Sparkles size={13} className="shrink-0" />
          分析完成：{task.nodeCount ?? 0} 节点 · {task.edgeCount ?? 0} 边 · {task.warningCount ?? 0}{" "}
          告警（耗时约 {Math.max(1, Math.round(((task.finishedAt ? Date.parse(task.finishedAt) : 0) - (task.startedAt ? Date.parse(task.startedAt) : 0)) / 1000))}s）
        </div>
      )}

      {graph?.meta?.sample && (
        <div className="flex shrink-0 items-center gap-2 border-b border-amber-200/70 bg-amber-50/70 px-4 py-1.5 text-[11px] text-amber-800">
          <TriangleAlert size={12} />
          {graph.meta.note ?? "示例数据，不代表真实源码分析结果。"}
        </div>
      )}

      {/* sample 横幅说明"这是假数据"，warnings 说明"分析过程出了问题"，两者独立 */}
      <WarningsBanner warnings={graph?.warnings ?? []} />

      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-64 shrink-0 flex-col border-r border-slate-200 bg-white lg:flex">
          {graph && (
            <GraphSidebar
              graph={graph}
              search={search}
              onSearchChange={setSearch}
              activeKinds={activeKinds}
              onToggleKind={toggleKind}
              onSelectAllKinds={() =>
                setActiveKinds(new Set(graph.nodes.map((node) => node.kind)))
              }
              selectedNodeId={selectedNodeId}
              onSelectNode={setSelectedNodeId}
            />
          )}
        </aside>

        <main className="relative min-w-0 flex-1 bg-slate-100">
          {graphQuery.isLoading && (
            <div className="flex h-full items-center justify-center gap-2 text-xs text-slate-400">
              <Loader2 size={14} className="animate-spin" />
              正在加载图数据…
            </div>
          )}

          {graphQuery.isError && (
            <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
              <TriangleAlert size={22} className="text-rose-500" />
              <p className="text-sm text-slate-700">图数据加载失败</p>
              <p className="max-w-md text-xs text-slate-500">
                {graphQuery.error instanceof Error ? graphQuery.error.message : "未知错误"}
              </p>
              <button
                type="button"
                onClick={() => void graphQuery.refetch()}
                className="mt-1 flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
              >
                <RotateCw size={12} />
                重试
              </button>
            </div>
          )}

          {graph && (
            <GraphCanvas
              graph={graph}
              selectedNodeId={selectedNodeId}
              visibleNodeIds={visibleNodeIds}
              onSelectNode={setSelectedNodeId}
            />
          )}
        </main>

        <aside className="hidden w-80 shrink-0 border-l border-slate-200 bg-white xl:block">
          <NodeInspector node={selectedNode} />
        </aside>
      </div>
    </div>
  );
}
