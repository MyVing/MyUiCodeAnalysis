import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  Crosshair,
  Database,
  Loader2,
  Maximize2,
  MessagesSquare,
  Radar,
  RefreshCw,
  RotateCw,
  Sparkles,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { api } from "@/api/client";
import { useAnalysisTask, useProject, useProjectGraph, useRuntimeTask } from "@/api/queries";
import type {
  AnalysisStart,
  Framework,
  RequirementCollectResult,
  RequirementCollectStart,
  RuntimeStart,
} from "@/api/types";
import { ExplorePanel } from "@/components/ExplorePanel";
import { GraphSidebar } from "@/components/GraphSidebar";
import { NodeInspector } from "@/components/NodeInspector";
import { RequirementPanel } from "@/components/RequirementPanel";
import { WarningsBanner } from "@/components/WarningsBanner";
import { GraphCanvas } from "@/graph/GraphCanvas";
import { resolveVisibleNodeIds, sliceGraph } from "@/graph/focus";
import type { NodeKind } from "@uicodeanalysis/contracts";

const FRAMEWORK_LABEL: Record<Framework, string> = {
  react: "React Web",
  next: "Next.js RSC",
};

/** 超过这个节点数就默认只渲染选中节点的邻域，避免一次性铺满画布 */
const AUTO_FOCUS_THRESHOLD = 120;

const INPUT_CLASS = "app-input text-xs";

export default function WorkbenchPage() {
  const params = useParams();
  const projectId = params.projectId ?? "";
  const location = useLocation();
  const navigate = useNavigate();
  const projectQuery = useProject(projectId);
  const graphQuery = useProjectGraph(projectId);
  const queryClient = useQueryClient();

  // 从 AI 对话页带回的需求草稿：一次性注入需求输入框，消费后立即清掉路由 state
  const draftFromChat = (
    location.state as { draftRequirement?: string } | null
  )?.draftRequirement;
  const [chatDraft, setChatDraft] = useState<string | null>(draftFromChat ?? null);
  useEffect(() => {
    if (!draftFromChat) return;
    setChatDraft(draftFromChat);
    navigate(location.pathname, { replace: true, state: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftFromChat]);

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
  // 运行时采集有两种入口：直接给地址，或给需求描述让后端定位页面再逐页采集
  const [runtimeMode, setRuntimeMode] = useState<"direct" | "requirement">("direct");
  const [reqCollectForm, setReqCollectForm] = useState({ requirement: "", baseUrl: "" });
  const [reqCollectResult, setReqCollectResult] = useState<RequirementCollectResult | null>(null);

  const project = projectQuery.data;
  const isGitProject = project?.source === "git";
  const [syncError, setSyncError] = useState<string | null>(null);

  useEffect(() => {
    // 切换项目时清掉上一个项目的更新错误，避免横幅串项目
    setSyncError(null);
  }, [projectId]);

  const syncProject = useMutation({
    mutationFn: () => api.syncProject(projectId),
    onMutate: () => setSyncError(null),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (error) => {
      setSyncError(error instanceof Error ? error.message : "更新代码失败");
    },
  });

  useEffect(() => {
    if (!graph) {
      return;
    }
    setActiveKinds(new Set(graph.nodes.map((node) => node.kind)));
    setSelectedNodeId(graph.nodes[0]?.id ?? null);
    setExactFocusNodeIds(null);
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

  // 按需求采集：同步等待需求定位完成（可能数十秒），再逐页发起采集任务
  const requirementCollect = useMutation({
    mutationFn: () => {
      const payload: RequirementCollectStart = {
        requirement: reqCollectForm.requirement.trim(),
        baseUrl: reqCollectForm.baseUrl.trim(),
      };
      return api.startRequirementCollect(projectId, payload);
    },
    onSuccess: (result) => {
      setReqCollectResult(result);
      // 跟踪第一个采集任务，复用既有的状态横幅与图刷新
      const firstTask = result.items[0];
      if (firstTask) {
        setActiveRuntimeTaskId(firstTask.id);
      }
    },
  });

  useEffect(() => {
    // 采集成功后后端已自动产出合并图（kind=merged），刷新即可看到运行时证据
    if (runtimeTask?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId, "graph"] });
    }
  }, [runtimeTask?.status, projectId, queryClient]);

  // null = 按图规模自动决定；用户手动切换后以用户选择为准
  const [focusOverride, setFocusOverride] = useState<boolean | null>(null);
  const [exactFocusNodeIds, setExactFocusNodeIds] = useState<Set<string> | null>(null);

  const filteredNodeIds = useMemo(() => {
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

  const focusMode = focusOverride ?? (graph ? graph.nodes.length > AUTO_FOCUS_THRESHOLD : false);

  const visibleNodeIds = useMemo(
    () =>
      graph
        ? resolveVisibleNodeIds(graph, {
            allowed: filteredNodeIds,
            selectedNodeId,
            exactNodeIds: exactFocusNodeIds,
            focusMode,
          })
        : filteredNodeIds,
    [graph, filteredNodeIds, focusMode, selectedNodeId, exactFocusNodeIds],
  );

  // 只把可见子图交给 React Flow：布局与节点数量都随子图收敛
  const renderedGraph = useMemo(
    () => (graph ? sliceGraph(graph, visibleNodeIds) : null),
    [graph, visibleNodeIds],
  );

  const selectedNode = graph?.nodes.find((node) => node.id === selectedNodeId);

  /** 需求匹配点击时使用服务端给出的精确子图；普通节点仍按邻域聚焦 */
  function focusNode(nodeId: string, focusNodeIds?: string[]) {
    setSelectedNodeId(nodeId);
    setExactFocusNodeIds(focusNodeIds?.length ? new Set(focusNodeIds) : null);
    setFocusOverride(true);
  }

  function selectGraphNode(nodeId: string | null) {
    setSelectedNodeId(nodeId);
    setExactFocusNodeIds(null);
  }

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
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 px-6 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-rose-400/20 bg-rose-400/10">
          <TriangleAlert size={22} className="text-rose-300" />
        </div>
        <p className="text-sm font-medium text-white">无法打开项目</p>
        <p className="max-w-md text-xs text-slate-400">
          {projectQuery.error instanceof Error ? projectQuery.error.message : "项目不存在"}
        </p>
        <div className="mt-2 flex gap-2">
          <Link to="/" className="app-btn-ghost">
            返回项目列表
          </Link>
          <button type="button" onClick={() => void projectQuery.refetch()} className="app-btn-primary">
            重试
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-white/[0.09] bg-[#0e1219]/85 px-4 backdrop-blur-xl">
        <div className="flex min-w-0 items-center gap-3">
          <Link to="/" className="app-btn-ghost shrink-0">
            <ArrowLeft size={14} />
            项目
          </Link>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="truncate text-sm font-semibold text-white">
                {project?.name ?? "加载中…"}
              </h1>
              {project && (
                <span className="rounded-md border border-indigo-400/20 bg-indigo-400/10 px-1.5 py-0.5 text-[10px] text-indigo-300">
                  {FRAMEWORK_LABEL[project.framework]}
                </span>
              )}
              {/* 徽标跟着图走：真实分析产物的 meta.sample 为空，标记自动消失 */}
              {graph?.meta?.sample ? (
                <span className="rounded-md border border-amber-400/20 bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-300">
                  示例 IR
                </span>
              ) : null}
            </div>
            <p className="truncate font-mono text-[10px] text-slate-400">
              {isGitProject && project?.remoteUrl ? project.remoteUrl : (project?.rootPath ?? projectId)}
              {isGitProject && project?.gitBranch ? ` · ${project.gitBranch}` : ""}
              {isGitProject && project?.commitSha ? ` · ${project.commitSha.slice(0, 7)}` : ""}
            </p>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <span className="hidden items-center gap-1 text-[11px] text-slate-400 md:flex">
            <Database size={12} />
            {project?.storage === "mysql" ? "MySQL 持久化" : "内存存储 · 重启清空"}
          </span>
          {isGitProject && (
            <button
              type="button"
              onClick={() => syncProject.mutate()}
              disabled={syncProject.isPending || taskActive || runtimeActive}
              title="从远端更新到最新提交"
              className="app-btn-ghost"
            >
              <RefreshCw size={13} className={syncProject.isPending ? "animate-spin" : undefined} />
              更新代码
            </button>
          )}
          <Link
            to={`/projects/${projectId}/chat`}
            className="app-btn-ghost"
            data-testid="open-requirement-chat"
          >
            <MessagesSquare size={13} />
            AI 需求助手
          </Link>
          <button
            type="button"
            onClick={() => setShowRuntime((current) => !current)}
            className="app-btn-ghost"
          >
            {showRuntime ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <Radar size={13} />
            运行时采集
          </button>
          <button
            type="button"
            onClick={() => setShowAdvanced((current) => !current)}
            className="app-btn-ghost"
          >
            {showAdvanced ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            高级选项
          </button>
          <button
            type="button"
            onClick={() => analysis.mutate()}
            disabled={analysis.isPending || taskActive}
            className="app-btn-primary"
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
        <div className="shrink-0 border-b border-white/[0.09] bg-[#151b25]/70 px-4 py-3">
          <div className="grid gap-3 md:grid-cols-3">
            <label className="flex flex-col gap-1">
              <span className="app-label">
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
              <span className="app-label">Base Commit（Diff 分析预留）</span>
              <input
                value={options.baseCommit}
                onChange={(event) =>
                  setOptions((current) => ({ ...current, baseCommit: event.target.value }))
                }
                placeholder="HEAD~1"
                className={`${INPUT_CLASS} font-mono`}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="app-label">Target Commit（Diff 分析预留）</span>
              <input
                value={options.targetCommit}
                onChange={(event) =>
                  setOptions((current) => ({ ...current, targetCommit: event.target.value }))
                }
                placeholder="HEAD"
                className={`${INPUT_CLASS} font-mono`}
              />
            </label>
          </div>
        </div>
      )}

      {showRuntime && (
        <div className="shrink-0 border-b border-white/[0.09] bg-[#151b25]/70 px-4 py-3">
          <div className="mb-2 flex items-center gap-1">
            {(
              [
                ["direct", "直接采集"],
                ["requirement", "按需求采集"],
              ] as const
            ).map(([mode, label]) => (
              <button
                key={mode}
                type="button"
                onClick={() => setRuntimeMode(mode)}
                className={`rounded-lg border px-2.5 py-1 text-[11px] transition ${
                  runtimeMode === mode
                    ? "border-indigo-400/30 bg-indigo-400/10 text-indigo-200"
                    : "border-white/[0.09] text-slate-400 hover:text-slate-200"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {runtimeMode === "direct" ? (
            <>
              <div className="grid gap-3 md:grid-cols-4">
                <label className="flex flex-col gap-1 md:col-span-2">
                  <span className="app-label">
                    启动命令（托管 dev server，采集完自动停止）
                  </span>
                  <input
                    value={runtimeOptions.command ?? ""}
                    onChange={(event) =>
                      setRuntimeOptions((current) => ({ ...current, command: event.target.value }))
                    }
                    placeholder="npm run dev"
                    className={`${INPUT_CLASS} font-mono`}
                  />
                </label>
                <label className="flex flex-col gap-1 md:col-span-1">
                  <span className="app-label">或页面地址（已在运行的页面）</span>
                  <input
                    value={runtimeOptions.url ?? ""}
                    onChange={(event) =>
                      setRuntimeOptions((current) => ({ ...current, url: event.target.value }))
                    }
                    placeholder="http://localhost:5199/"
                    className={`${INPUT_CLASS} font-mono`}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="app-label">静默观察窗口（ms）</span>
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
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={
                    runtimeCollect.isPending ||
                    runtimeActive ||
                    ((runtimeOptions.command ?? "").trim() === "" &&
                      (runtimeOptions.url ?? "").trim() === "")
                  }
                  onClick={() => runtimeCollect.mutate()}
                  className="app-btn-ghost"
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
                <p className="mt-2 text-[11px] text-rose-300">
                  {runtimeCollect.error instanceof Error
                    ? runtimeCollect.error.message
                    : "发起采集失败"}
                </p>
              )}
            </>
          ) : (
            <>
              <div className="grid gap-3 md:grid-cols-4">
                <label className="flex flex-col gap-1 md:col-span-2">
                  <span className="app-label">
                    需求描述（先定位页面，再按 IR 路由逐页采集）
                  </span>
                  <textarea
                    value={reqCollectForm.requirement}
                    onChange={(event) =>
                      setReqCollectForm((current) => ({
                        ...current,
                        requirement: event.target.value,
                      }))
                    }
                    placeholder="例如：验证用户列表的搜索交互"
                    rows={2}
                    className={INPUT_CLASS}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="app-label">站点地址（已在运行，作为路由拼接前缀）</span>
                  <input
                    value={reqCollectForm.baseUrl}
                    onChange={(event) =>
                      setReqCollectForm((current) => ({
                        ...current,
                        baseUrl: event.target.value,
                      }))
                    }
                    placeholder="http://localhost:5199"
                    className={`${INPUT_CLASS} font-mono`}
                  />
                </label>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={
                    requirementCollect.isPending ||
                    runtimeActive ||
                    reqCollectForm.requirement.trim() === "" ||
                    reqCollectForm.baseUrl.trim() === ""
                  }
                  onClick={() => requirementCollect.mutate()}
                  className="app-btn-ghost"
                >
                  {requirementCollect.isPending ? (
                    <Loader2 size={13} className="animate-spin" />
                  ) : (
                    <Radar size={13} />
                  )}
                  定位并采集
                </button>
                <span className="text-[11px] text-slate-400">
                  需求定位可能需要较长时间；无路由 / 动态参数的页面会跳过并说明原因
                </span>
              </div>
              {requirementCollect.isPending && (
                <p className="mt-2 text-[11px] text-indigo-300">正在定位需求对应的页面…</p>
              )}
              {requirementCollect.isError && (
                <p className="mt-2 text-[11px] text-rose-300">
                  {requirementCollect.error instanceof Error
                    ? requirementCollect.error.message
                    : "发起按需求采集失败"}
                </p>
              )}
              {reqCollectResult && (
                <div className="mt-2 space-y-1 text-[11px] text-slate-300">
                  <p>
                    已创建 {reqCollectResult.items.length} 个采集任务
                    {reqCollectResult.skipped.length > 0 &&
                      `，跳过 ${reqCollectResult.skipped.length} 个页面`}
                    （首个任务进度见下方横幅）
                  </p>
                  {reqCollectResult.skipped.map((item) => (
                    <p key={item.pageNodeId} className="text-amber-300/80">
                      跳过 {item.pageNodeId}
                      {item.routePath ? `（${item.routePath}）` : ""}：{item.reason} —{" "}
                      {item.message}
                    </p>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {syncError && (
        <StatusBanner tone="error" icon={TriangleAlert}>
          更新代码失败：{syncError}
        </StatusBanner>
      )}

      {runtimeActive && (
        <StatusBanner tone="info" icon={Loader2} spin>
          正在运行时采集…（启动 dev server 与浏览器执行中，完成后自动刷新合并图）
        </StatusBanner>
      )}

      {runtimeTask?.status === "failed" && (
        <StatusBanner
          tone="error"
          icon={TriangleAlert}
          action={{ label: "重试", onClick: () => runtimeCollect.mutate() }}
        >
          采集失败（{runtimeTask.errorCode ?? "unknown"}）：{runtimeTask.errorMessage ?? "未知错误"}
        </StatusBanner>
      )}

      {runtimeTask?.status === "succeeded" && (
        <StatusBanner tone="success" icon={Radar}>
          采集完成：{runtimeTask.componentCount ?? 0} 组件 · {runtimeTask.networkCount ?? 0} 网络请求
          · {runtimeTask.consoleCount ?? 0} Console · {runtimeTask.interactionCount ?? 0} 交互
          （已并入图谱，节点详情可见运行时证据）
        </StatusBanner>
      )}

      {taskActive && (
        <StatusBanner tone="info" icon={Loader2} spin>
          正在分析源码…（ast-worker 子进程执行中，完成后自动刷新图）
        </StatusBanner>
      )}

      {analysis.isError && (
        <StatusBanner
          tone="error"
          icon={TriangleAlert}
          action={{ label: "重试", onClick: () => analysis.mutate() }}
        >
          无法发起分析：
          {analysis.error instanceof Error ? analysis.error.message : "未知错误"}
        </StatusBanner>
      )}

      {task?.status === "failed" && (
        <StatusBanner
          tone="error"
          icon={TriangleAlert}
          action={{ label: "重试", onClick: () => analysis.mutate() }}
        >
          分析失败（{task.errorCode ?? "unknown"}）：{task.errorMessage ?? "未知错误"}
        </StatusBanner>
      )}

      {task?.status === "succeeded" && (
        <StatusBanner tone="success" icon={Sparkles}>
          分析完成：{task.nodeCount ?? 0} 节点 · {task.edgeCount ?? 0} 边 · {task.warningCount ?? 0}{" "}
          告警（耗时约{" "}
          {Math.max(
            1,
            Math.round(
              ((task.finishedAt ? Date.parse(task.finishedAt) : 0) -
                (task.startedAt ? Date.parse(task.startedAt) : 0)) /
                1000,
            ),
          )}
          s）
        </StatusBanner>
      )}

      {graph?.meta?.sample && (
        <StatusBanner tone="warning" icon={TriangleAlert}>
          {graph.meta.note ?? "示例数据，不代表真实源码分析结果。"}
        </StatusBanner>
      )}

      {/* sample 横幅说明"这是假数据"，warnings 说明"分析过程出了问题"，两者独立 */}
      <WarningsBanner warnings={graph?.warnings ?? []} />

      {graph && !graph.meta?.sample && (
        <RequirementPanel
          projectId={projectId}
          analysisId={graph.analysisId}
          onSelectNode={focusNode}
          initialRequirement={chatDraft ?? undefined}
        />
      )}

      <ExplorePanel projectId={projectId} />

      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-64 shrink-0 flex-col border-r border-white/[0.09] bg-[#10151e] lg:flex">
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
              onSelectNode={selectGraphNode}
            />
          )}
        </aside>

        <main className="relative min-w-0 flex-1 bg-[#121722]">
          {graphQuery.isLoading && (
            <div className="flex h-full items-center justify-center gap-2 text-xs text-slate-400">
              <Loader2 size={14} className="animate-spin" />
              正在加载图数据…
            </div>
          )}

          {graphQuery.isError && (
            <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
              <TriangleAlert size={22} className="text-rose-300" />
              <p className="text-sm text-slate-200">图数据加载失败</p>
              <p className="max-w-md text-xs text-slate-400">
                {graphQuery.error instanceof Error ? graphQuery.error.message : "未知错误"}
              </p>
              <button
                type="button"
                onClick={() => void graphQuery.refetch()}
                className="app-btn-ghost mt-1"
              >
                <RotateCw size={12} />
                重试
              </button>
            </div>
          )}

          {graph && renderedGraph && (
            <>
              <GraphCanvas
                graph={renderedGraph}
                selectedNodeId={selectedNodeId}
                onSelectNode={selectGraphNode}
              />
              <div className="pointer-events-auto absolute right-4 top-4 z-10 flex items-center gap-2 rounded-xl border border-white/[0.12] bg-[#151b25]/90 px-2.5 py-1.5 text-[11px] text-slate-300 shadow-lg shadow-black/40 backdrop-blur">
                <span data-testid="rendered-node-count">
                  {renderedGraph.nodes.length} / {graph.nodes.length} 节点
                </span>
                <button
                  type="button"
                  onClick={() => setFocusOverride(!focusMode)}
                  className="flex items-center gap-1 rounded-lg border border-white/[0.12] bg-white/[0.05] px-2 py-1 text-[11px] text-slate-200 transition hover:bg-white/[0.1]"
                  data-testid="focus-toggle"
                >
                  {focusMode ? <Maximize2 size={12} /> : <Crosshair size={12} />}
                  {focusMode ? "显示全图" : "聚焦选中"}
                </button>
              </div>
            </>
          )}
        </main>

        <aside className="hidden w-80 shrink-0 border-l border-white/[0.09] bg-[#10151e] xl:block">
          <NodeInspector
            node={selectedNode}
            graph={graph}
            projectId={projectId}
            sample={Boolean(graph?.meta?.sample)}
            onSelectNode={focusNode}
          />
        </aside>
      </div>
    </div>
  );
}

const TONE_CLASS: Record<"info" | "success" | "warning" | "error", string> = {
  info: "border-indigo-400/20 bg-indigo-400/[0.08] text-indigo-200",
  success: "border-emerald-400/20 bg-emerald-400/[0.08] text-emerald-200",
  warning: "border-amber-400/20 bg-amber-400/[0.08] text-amber-200",
  error: "border-rose-400/20 bg-rose-400/[0.08] text-rose-200",
};

function StatusBanner({
  tone,
  icon: Icon,
  spin,
  action,
  children,
}: {
  tone: "info" | "success" | "warning" | "error";
  icon: LucideIcon;
  spin?: boolean;
  action?: { label: string; onClick: () => void };
  children: ReactNode;
}) {
  return (
    <div
      className={`flex shrink-0 items-center gap-2 border-b px-4 py-2 text-xs ${TONE_CLASS[tone]}`}
    >
      <Icon size={13} className={`shrink-0 ${spin ? "animate-spin" : ""}`} />
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {action && (
        <button
          type="button"
          onClick={action.onClick}
          className="shrink-0 rounded-lg border border-current/20 bg-white/[0.04] px-2 py-0.5 text-[11px] transition hover:bg-white/[0.08]"
        >
          {action.label}
        </button>
      )}
    </div>
  );
}
