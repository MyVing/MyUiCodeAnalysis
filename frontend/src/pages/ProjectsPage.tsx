import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRight,
  Boxes,
  ChevronDown,
  Database,
  FolderOpen,
  GitBranch,
  GitFork,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  TriangleAlert,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, api } from "@/api/client";
import { useHealth, useProjects } from "@/api/queries";
import type { Framework, Project, ProjectCreate, ProjectGitCreate } from "@/api/types";
import { formatDateTime } from "@/lib/format";

const FRAMEWORK_LABEL: Record<Framework, string> = {
  react: "React Web",
  next: "Next.js RSC",
};

const EMPTY_GIT_FORM = {
  gitUrl: "",
  name: "",
  branch: "",
  username: "",
  password: "",
  token: "",
  framework: "react" as Framework,
};
const EMPTY_LOCAL_FORM: ProjectCreate = { name: "", rootPath: "", framework: "react" };

function shortCommit(sha?: string | null): string | null {
  return sha ? sha.slice(0, 7) : null;
}

export default function ProjectsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const health = useHealth();
  const projects = useProjects();

  const [mode, setMode] = useState<"git" | "local">("git");
  const [gitForm, setGitForm] = useState(EMPTY_GIT_FORM);
  const [localForm, setLocalForm] = useState<ProjectCreate>(EMPTY_LOCAL_FORM);
  const [showGitAdvanced, setShowGitAdvanced] = useState(false);
  const [alreadyOnboardedId, setAlreadyOnboardedId] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<{ id: string; message: string } | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<{ id: string; message: string } | null>(null);

  const createGit = useMutation({
    mutationFn: (payload: ProjectGitCreate) => api.createGitProject(payload),
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      navigate(`/projects/${project.id}`);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "git_project_exists" && error.projectId) {
        setAlreadyOnboardedId(error.projectId);
      }
    },
    onMutate: () => setAlreadyOnboardedId(null),
  });

  const createLocal = useMutation({
    mutationFn: (payload: ProjectCreate) => api.createProject(payload),
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      navigate(`/projects/${project.id}`);
    },
  });

  const syncProject = useMutation({
    mutationFn: (projectId: string) => api.syncProject(projectId),
    onSuccess: () => {
      setSyncError(null);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (error, projectId) => {
      setSyncError({
        id: projectId,
        message: error instanceof Error ? error.message : "更新失败，请稍后重试。",
      });
    },
  });

  // 删除走两步确认：第一次点只进入待确认态，再点一次才真正删，
  // 避免误触——删除会连带清掉分析产物与 Git 受管克隆目录
  const deleteProject = useMutation({
    mutationFn: (projectId: string) => api.deleteProject(projectId),
    onSuccess: () => {
      setDeleteError(null);
      setConfirmingDeleteId(null);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (error, projectId) => {
      setConfirmingDeleteId(null);
      setDeleteError({
        id: projectId,
        message: error instanceof Error ? error.message : "删除失败，请稍后重试。",
      });
    },
  });

  function handleGitSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const payload: ProjectGitCreate = { gitUrl: gitForm.gitUrl.trim() };
    if (gitForm.name.trim()) payload.name = gitForm.name.trim();
    if (gitForm.branch.trim()) payload.branch = gitForm.branch.trim();
    if (gitForm.username.trim()) payload.username = gitForm.username.trim();
    if (gitForm.password) payload.password = gitForm.password;
    if (gitForm.token.trim()) payload.token = gitForm.token.trim();
    if (gitForm.framework !== "react") payload.framework = gitForm.framework;
    createGit.mutate(payload);
  }

  function handleLocalSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    createLocal.mutate(localForm);
  }

  const online = health.isSuccess;
  const gitProjectCount = projects.data?.filter((project) => project.source === "git").length ?? 0;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-white/[0.09] bg-[#0e1219]/85 backdrop-blur-xl">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-4">
          <div className="flex items-center gap-3">
            <div className="relative flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-[0_0_24px_-4px_rgba(99,102,241,0.8)]">
              <Boxes size={17} />
            </div>
            <div>
              <p className="text-sm font-semibold tracking-tight text-white">
                React 代码智能分析平台
              </p>
              <p className="text-[11px] text-slate-400">
                静态结构 · 运行时事实 · AI 推断，统一为可追溯的组件图
              </p>
            </div>
          </div>

          <span
            className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] ${
              online
                ? "border-emerald-400/20 bg-emerald-400/10 text-emerald-300"
                : "border-rose-400/20 bg-rose-400/10 text-rose-300"
            }`}
          >
            {online ? <Wifi size={12} /> : <WifiOff size={12} />}
            {online ? `后端在线 · v${health.data?.version ?? "-"}` : "后端离线"}
          </span>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 pb-16 pt-10">
        <section className="app-enter">
          <p className="text-[11px] font-medium uppercase tracking-[0.24em] text-indigo-400/80">
            Code Intelligence Workspace
          </p>
          <h1 className="mt-3 max-w-2xl text-[32px] font-semibold leading-tight tracking-tight text-white">
            把 React 代码库变成
            <span className="bg-gradient-to-r from-indigo-300 via-violet-300 to-sky-300 bg-clip-text text-transparent">
              可追溯的组件图谱
            </span>
          </h1>
          <p className="mt-3 max-w-2xl text-sm leading-relaxed text-slate-300">
            输入 Git 地址即可自动克隆、识别框架并建立分析工程。同一仓库只接入一次，更新时以远端为准，
            让每次分析都建立在最新代码上。
          </p>

          <div className="mt-6 flex flex-wrap gap-2">
            <span className="app-chip">
              <GitFork size={12} className="text-indigo-300" />
              已接入 {gitProjectCount} 个 Git 仓库
            </span>
            <span className="app-chip">
              <Database size={12} className="text-slate-400" />
              {health.data?.storage === "mysql" ? "MySQL 持久化" : "内存存储"}
            </span>
            <span className="app-chip">
              <Sparkles size={12} className="text-amber-300" />
              AI 语义层已就绪
            </span>
          </div>
        </section>

        <div className="mt-8 grid gap-6 lg:grid-cols-[400px_1fr]">
          <section className="app-card app-enter h-fit p-5">
            <div className="flex rounded-xl border border-white/[0.1] bg-black/20 p-1">
              <button
                type="button"
                onClick={() => setMode("git")}
                className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                  mode === "git"
                    ? "bg-white/[0.08] text-white shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                <GitBranch size={13} />
                Git 仓库
              </button>
              <button
                type="button"
                onClick={() => setMode("local")}
                className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                  mode === "local"
                    ? "bg-white/[0.08] text-white shadow-sm"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                <FolderOpen size={13} />
                本地目录
              </button>
            </div>

            {mode === "git" ? (
              <form onSubmit={handleGitSubmit} className="mt-5">
                <label className="block">
                  <span className="app-label">Git 地址</span>
                  <div className="relative">
                    <GitBranch
                      size={14}
                      className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                    />
                    <input
                      required
                      maxLength={512}
                      value={gitForm.gitUrl}
                      onChange={(event) =>
                        setGitForm({ ...gitForm, gitUrl: event.target.value })
                      }
                      placeholder="https://github.com/acme/app.git"
                      className="app-input pl-9 font-mono text-xs"
                    />
                  </div>
                </label>

                <p className="mt-2 text-[11px] leading-relaxed text-slate-400">
                  支持 HTTPS 与 <span className="font-mono text-slate-300">git@host:org/repo</span>{" "}
                  两种写法。平台会浅克隆到受管目录，不写入你的工作区。
                </p>

                <label className="mt-4 block">
                  <span className="app-label">项目名称（可选）</span>
                  <input
                    maxLength={80}
                    value={gitForm.name}
                    onChange={(event) => setGitForm({ ...gitForm, name: event.target.value })}
                    placeholder="默认取仓库名"
                    className="app-input text-xs"
                  />
                </label>

                <button
                  type="button"
                  onClick={() => setShowGitAdvanced((current) => !current)}
                  className="mt-4 flex items-center gap-1 text-[11px] text-slate-400 transition hover:text-slate-200"
                >
                  <ChevronDown
                    size={13}
                    className={`transition ${showGitAdvanced ? "" : "-rotate-90"}`}
                  />
                  高级选项（分支 · 私有仓库账号 / Token · 框架）
                </button>

                {showGitAdvanced && (
                  <div className="mt-3 space-y-3 rounded-xl border border-white/[0.06] bg-black/20 p-3">
                    <label className="block">
                      <span className="app-label">分支</span>
                      <input
                        maxLength={128}
                        value={gitForm.branch}
                        onChange={(event) =>
                          setGitForm({ ...gitForm, branch: event.target.value })
                        }
                        placeholder="默认仓库的默认分支"
                        className="app-input font-mono text-xs"
                      />
                    </label>
                    <label className="block">
                      <span className="app-label">用户名（私有仓库）</span>
                      <input
                        maxLength={256}
                        autoComplete="off"
                        value={gitForm.username}
                        onChange={(event) =>
                          setGitForm({ ...gitForm, username: event.target.value })
                        }
                        placeholder="例如：git 账号或邮箱"
                        className="app-input text-xs"
                      />
                    </label>
                    <label className="block">
                      <span className="app-label">密码 / 访问令牌（仅本次克隆使用）</span>
                      <div className="relative">
                        <Lock
                          size={13}
                          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                        />
                        <input
                          type="password"
                          maxLength={512}
                          autoComplete="new-password"
                          value={gitForm.password}
                          onChange={(event) =>
                            setGitForm({ ...gitForm, password: event.target.value })
                          }
                          placeholder="与用户名配合使用，不会保存"
                          className="app-input pl-9 text-xs"
                        />
                      </div>
                    </label>
                    <label className="block">
                      <span className="app-label">或使用 Token（仅本次克隆使用）</span>
                      <div className="relative">
                        <Lock
                          size={13}
                          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                        />
                        <input
                          type="password"
                          maxLength={512}
                          autoComplete="off"
                          value={gitForm.token}
                          onChange={(event) =>
                            setGitForm({ ...gitForm, token: event.target.value })
                          }
                          placeholder="与用户名密码任选其一，不会保存"
                          className="app-input pl-9 text-xs"
                        />
                      </div>
                    </label>
                    <label className="block">
                      <span className="app-label">框架</span>
                      <select
                        value={gitForm.framework}
                        onChange={(event) =>
                          setGitForm({
                            ...gitForm,
                            framework: event.target.value as Framework,
                          })
                        }
                        className="app-input text-xs"
                      >
                        <option value="react">React Web（自动识别，可覆盖）</option>
                        <option value="next">Next.js App Router（含 RSC）</option>
                      </select>
                    </label>
                  </div>
                )}

                {createGit.isError && (
                  <div className="mt-4 rounded-xl border border-rose-400/20 bg-rose-400/[0.08] px-3 py-2.5 text-xs text-rose-200">
                    <p className="flex items-start gap-1.5">
                      <TriangleAlert size={13} className="mt-0.5 shrink-0" />
                      <span>
                        {createGit.error instanceof Error
                          ? createGit.error.message
                          : "接入项目失败，请稍后重试。"}
                      </span>
                    </p>
                    {alreadyOnboardedId && (
                      <button
                        type="button"
                        onClick={() => navigate(`/projects/${alreadyOnboardedId}`)}
                        className="mt-2 inline-flex items-center gap-1 rounded-lg border border-rose-300/30 bg-rose-300/10 px-2 py-1 text-[11px] font-medium text-rose-100 transition hover:bg-rose-300/20"
                      >
                        打开已接入的项目
                        <ArrowUpRight size={12} />
                      </button>
                    )}
                  </div>
                )}

                <button type="submit" disabled={createGit.isPending} className="app-btn-primary mt-4 w-full">
                  {createGit.isPending ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <GitFork size={14} />
                  )}
                  {createGit.isPending ? "正在克隆并初始化…" : "接入并初始化项目"}
                </button>
              </form>
            ) : (
              <form onSubmit={handleLocalSubmit} className="mt-5 space-y-3">
                <label className="block">
                  <span className="app-label">项目名称</span>
                  <input
                    required
                    maxLength={80}
                    value={localForm.name}
                    onChange={(event) =>
                      setLocalForm({ ...localForm, name: event.target.value })
                    }
                    placeholder="例如：订单平台"
                    className="app-input text-xs"
                  />
                </label>
                <label className="block">
                  <span className="app-label">项目根路径</span>
                  <input
                    required
                    maxLength={512}
                    value={localForm.rootPath}
                    onChange={(event) =>
                      setLocalForm({ ...localForm, rootPath: event.target.value })
                    }
                    placeholder="D:/work/orders"
                    className="app-input font-mono text-xs"
                  />
                </label>
                <label className="block">
                  <span className="app-label">项目类型</span>
                  <select
                    value={localForm.framework}
                    onChange={(event) =>
                      setLocalForm({
                        ...localForm,
                        framework: event.target.value as Framework,
                      })
                    }
                    className="app-input text-xs"
                  >
                    <option value="react">React Web（客户端渲染）</option>
                    <option value="next">Next.js App Router（含 RSC）</option>
                  </select>
                </label>

                <button
                  type="button"
                  onClick={() =>
                    setLocalForm({
                      name: "企业级 React 示例应用",
                      rootPath:
                        "D:/pythonProjects/UiCodeAnaylysis/MyUiCodeAnalysis/tests/fixtures/enterprise-react-app",
                      framework: "react",
                    })
                  }
                  className="text-[11px] text-indigo-300 transition hover:text-indigo-200"
                >
                  填入示例路径
                </button>

                {createLocal.isError && (
                  <p className="rounded-xl border border-rose-400/20 bg-rose-400/[0.08] px-3 py-2 text-xs text-rose-200">
                    {createLocal.error instanceof Error
                      ? createLocal.error.message
                      : "接入项目失败，请稍后重试。"}
                  </p>
                )}

                <button type="submit" disabled={createLocal.isPending} className="app-btn-primary w-full">
                  {createLocal.isPending ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <Plus size={14} />
                  )}
                  接入本地项目
                </button>
              </form>
            )}
          </section>

          <section className="app-enter">
            <div className="flex items-end justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-white">已接入项目</h2>
                <p className="mt-0.5 text-[11px] text-slate-400">
                  {projects.data?.length ?? 0} 个项目 ·{" "}
                  {health.data?.storage === "mysql" ? "MySQL 持久化" : "重启后清空"}
                </p>
              </div>
              <span className="text-[11px] text-slate-400">
                同一 Git 地址仅允许接入一次
              </span>
            </div>

            {projects.isLoading && (
              <div className="app-card mt-4 flex items-center justify-center gap-2 py-14 text-xs text-slate-400">
                <Loader2 size={14} className="animate-spin" />
                正在加载项目…
              </div>
            )}

            {projects.isError && (
              <p className="mt-4 rounded-xl border border-rose-400/20 bg-rose-400/[0.08] px-4 py-3 text-xs text-rose-200">
                {projects.error instanceof Error ? projects.error.message : "加载项目失败"}
              </p>
            )}

            {projects.isSuccess && projects.data.length === 0 && (
              <div className="mt-4 rounded-2xl border border-dashed border-white/10 bg-white/[0.03] py-16 text-center">
                <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-xl border border-white/[0.12] bg-white/[0.06] text-slate-400">
                  <Boxes size={20} />
                </div>
                <p className="mt-3 text-xs text-slate-400">
                  还没有项目，粘贴一个 Git 地址开始。
                </p>
              </div>
            )}

            <div className="mt-4 space-y-2.5">
              {projects.data?.map((project) => (
                <ProjectCard
                  key={project.id}
                  project={project}
                  syncing={syncProject.isPending && syncProject.variables === project.id}
                  deleting={deleteProject.isPending && deleteProject.variables === project.id}
                  confirmingDelete={confirmingDeleteId === project.id}
                  error={
                    syncError?.id === project.id
                      ? syncError.message
                      : deleteError?.id === project.id
                        ? deleteError.message
                        : null
                  }
                  onOpen={() => {
                    setConfirmingDeleteId(null);
                    navigate(`/projects/${project.id}`);
                  }}
                  onSync={() => {
                    setConfirmingDeleteId(null);
                    syncProject.mutate(project.id);
                  }}
                  onDelete={() => {
                    if (confirmingDeleteId === project.id) {
                      deleteProject.mutate(project.id);
                    } else {
                      setConfirmingDeleteId(project.id);
                    }
                  }}
                />
              ))}
            </div>
          </section>
        </div>

        <div className="mt-8 flex items-start gap-3 rounded-2xl border border-white/[0.1] bg-white/[0.04] px-4 py-3.5">
          <Sparkles size={15} className="mt-0.5 shrink-0 text-indigo-300" />
          <p className="text-xs leading-relaxed text-slate-400">
            当前能力：真实静态分析生成组件图 · 浏览器运行时采集并与静态图合并 · 需求到页面的 AI 定位（可 dry-run）。
            Git 项目更新后会强制对齐远端分支，重新分析即可得到最新结构的图谱。
          </p>
        </div>
      </main>
    </div>
  );
}

function ProjectCard({
  project,
  syncing,
  deleting,
  confirmingDelete,
  error,
  onOpen,
  onSync,
  onDelete,
}: {
  project: Project;
  syncing: boolean;
  deleting: boolean;
  confirmingDelete: boolean;
  error: string | null;
  onOpen: () => void;
  onSync: () => void;
  onDelete: () => void;
}) {
  const isGit = project.source === "git";
  const commit = shortCommit(project.commitSha);

  return (
    <div className="group rounded-2xl border border-white/[0.1] bg-white/[0.04] transition hover:border-indigo-400/40 hover:bg-white/[0.06] hover:shadow-[0_18px_50px_-30px_rgba(99,102,241,0.7)]">
      <div className="flex items-center gap-3 p-4">
        <button
          type="button"
          onClick={onOpen}
          className="flex min-w-0 flex-1 items-center gap-3 text-left"
        >
          <div
            className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border ${
              isGit
                ? "border-indigo-400/20 bg-indigo-400/10 text-indigo-300"
                : "border-white/[0.12] bg-white/[0.06] text-slate-300"
            }`}
          >
            {isGit ? <GitBranch size={17} /> : <FolderOpen size={17} />}
          </div>

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <p className="truncate text-sm font-semibold text-white">{project.name}</p>
              <span className="rounded-md border border-indigo-400/20 bg-indigo-400/10 px-1.5 py-0.5 text-[10px] text-indigo-300">
                {FRAMEWORK_LABEL[project.framework]}
              </span>
              <span className="rounded-md border border-white/[0.12] bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-slate-400">
                {isGit ? "Git 接入" : "本地目录"}
              </span>
            </div>

            <p className="mt-1 truncate font-mono text-[11px] text-slate-400">
              {isGit ? project.remoteUrl : project.rootPath}
            </p>

            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-slate-400">
              {isGit ? (
                <>
                  <span className="flex items-center gap-1">
                    <GitBranch size={10} />
                    {project.gitBranch ?? "-"}
                  </span>
                  <span className="font-mono">{commit ?? "未记录提交"}</span>
                  <span>
                    {project.lastSyncedAt
                      ? `更新于 ${formatDateTime(project.lastSyncedAt)}`
                      : `接入于 ${formatDateTime(project.createdAt)}`}
                  </span>
                </>
              ) : (
                <span>接入于 {formatDateTime(project.createdAt)}</span>
              )}
            </div>
          </div>

          <ArrowUpRight
            size={16}
            className="shrink-0 text-slate-400 transition group-hover:text-indigo-300"
          />
        </button>

        {isGit && (
          <button
            type="button"
            onClick={onSync}
            disabled={syncing || deleting}
            title="从远端更新到最新提交"
            className="app-btn-icon shrink-0"
          >
            <RefreshCw size={13} className={syncing ? "animate-spin" : undefined} />
          </button>
        )}

        <button
          type="button"
          onClick={onDelete}
          disabled={deleting || syncing}
          title={
            confirmingDelete
              ? "再点一次确认删除（会清掉分析产物）"
              : "删除项目（分析产物一并清除）"
          }
          className={`app-btn-icon shrink-0 transition ${
            confirmingDelete
              ? "border-rose-400/40 bg-rose-400/15 text-rose-300"
              : "text-slate-400 hover:border-rose-400/30 hover:text-rose-300"
          }`}
        >
          {deleting ? (
            <Loader2 size={13} className="animate-spin" />
          ) : (
            <Trash2 size={13} />
          )}
        </button>
      </div>

      {confirmingDelete && (
        <div className="flex items-center justify-between gap-3 border-t border-rose-400/10 px-4 py-2">
          <p className="text-[11px] text-rose-200">
            删除后无法恢复：分析产物{isGit ? "与 Git 克隆目录 " : " "}一并清除，确定吗？
          </p>
          <button
            type="button"
            onClick={onDelete}
            disabled={deleting}
            className="shrink-0 rounded-lg border border-rose-400/40 bg-rose-400/15 px-2.5 py-1 text-[11px] font-medium text-rose-200 transition hover:bg-rose-400/25"
          >
            确认删除
          </button>
        </div>
      )}

      {error && (
        <p className="border-t border-rose-400/10 px-4 py-2 text-[11px] text-rose-300">
          {error}
        </p>
      )}
    </div>
  );
}
