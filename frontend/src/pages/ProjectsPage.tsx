import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Boxes,
  Database,
  Loader2,
  Plus,
  Sparkles,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "@/api/client";
import { useHealth, useProjects } from "@/api/queries";
import type { Framework, ProjectCreate } from "@/api/types";
import { formatDateTime } from "@/lib/format";

const FRAMEWORK_LABEL: Record<Framework, string> = {
  react: "React Web",
  next: "Next.js RSC",
};

const INPUT_CLASS =
  "w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm outline-none transition focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100";

const EMPTY_FORM: ProjectCreate = { name: "", rootPath: "", framework: "react" };

export default function ProjectsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const health = useHealth();
  const projects = useProjects();
  const [form, setForm] = useState<ProjectCreate>(EMPTY_FORM);

  const createProject = useMutation({
    mutationFn: api.createProject,
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      navigate(`/projects/${project.id}`);
    },
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    createProject.mutate(form);
  }

  const online = health.isSuccess;

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-6 py-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-600 text-white">
              <Boxes size={18} />
            </div>
            <div>
              <h1 className="text-sm font-semibold text-slate-900">React 代码智能分析平台</h1>
              <p className="text-[11px] text-slate-500">
                静态结构 · 运行时事实 · AI 推断，统一为可追溯的组件图
              </p>
            </div>
          </div>

          <span
            className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] ${
              online
                ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                : "border-rose-200 bg-rose-50 text-rose-600"
            }`}
          >
            {online ? <Wifi size={12} /> : <WifiOff size={12} />}
            {online ? `后端在线 · v${health.data?.version ?? "-"}` : "后端离线"}
          </span>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-8">
        <div className="grid gap-6 lg:grid-cols-[360px_1fr]">
          <form
            onSubmit={handleSubmit}
            className="h-fit rounded-xl border border-slate-200 bg-white p-5 shadow-sm"
          >
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-900">接入项目</h2>
              <button
                type="button"
                onClick={() =>
                  setForm({
                    name: "企业级 React 示例应用",
                    rootPath: "D:/pythonProjects/UiCodeAnaylysis/MyUiCodeAnalysis/tests/fixtures/enterprise-react-app",
                    framework: "react",
                  })
                }
                className="text-[11px] text-indigo-600 hover:text-indigo-700"
              >
                填入示例
              </button>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-slate-500">
              登记项目后可在工作台对源码做真实静态分析；未分析时图数据为明确标记的示例 IR。
            </p>

            <div className="mt-4 space-y-3">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-600">项目名称</span>
                <input
                  required
                  maxLength={80}
                  value={form.name}
                  onChange={(event) => setForm({ ...form, name: event.target.value })}
                  placeholder="例如：订单平台"
                  className={INPUT_CLASS}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-600">项目根路径</span>
                <input
                  required
                  maxLength={512}
                  value={form.rootPath}
                  onChange={(event) => setForm({ ...form, rootPath: event.target.value })}
                  placeholder="D:/work/orders"
                  className={`${INPUT_CLASS} font-mono text-xs`}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-600">项目类型</span>
                <select
                  value={form.framework}
                  onChange={(event) =>
                    setForm({ ...form, framework: event.target.value as Framework })
                  }
                  className={INPUT_CLASS}
                >
                  <option value="react">React Web（客户端渲染）</option>
                  <option value="next">Next.js App Router（含 RSC）</option>
                </select>
              </label>
            </div>

            {createProject.isError && (
              <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {createProject.error instanceof Error
                  ? createProject.error.message
                  : "接入项目失败，请稍后重试。"}
              </p>
            )}

            <button
              type="submit"
              disabled={createProject.isPending}
              className="mt-4 flex w-full items-center justify-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {createProject.isPending ? (
                <Loader2 size={15} className="animate-spin" />
              ) : (
                <Plus size={15} />
              )}
              接入项目
            </button>
          </form>

          <section>
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-900">已接入项目</h2>
              <span className="text-[11px] text-slate-400">
                {projects.data?.length ?? 0} 个项目 · 内存存储
              </span>
            </div>

            {projects.isLoading && (
              <div className="mt-6 flex items-center justify-center gap-2 rounded-xl border border-dashed border-slate-200 bg-white py-10 text-xs text-slate-400">
                <Loader2 size={14} className="animate-spin" />
                正在加载项目…
              </div>
            )}

            {projects.isError && (
              <p className="mt-4 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-700">
                {projects.error instanceof Error ? projects.error.message : "加载项目失败"}
              </p>
            )}

            {projects.isSuccess && projects.data.length === 0 && (
              <div className="mt-4 rounded-xl border border-dashed border-slate-200 bg-white py-12 text-center">
                <Boxes size={22} className="mx-auto text-slate-300" />
                <p className="mt-2 text-xs text-slate-500">还没有项目，先接入一个开始分析。</p>
              </div>
            )}

            <div className="mt-3 space-y-2">
              {projects.data?.map((project) => (
                <button
                  key={project.id}
                  type="button"
                  onClick={() => navigate(`/projects/${project.id}`)}
                  className="group flex w-full items-center gap-4 rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:border-indigo-300 hover:shadow"
                >
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-500">
                    <Boxes size={18} />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="truncate text-sm font-semibold text-slate-900">{project.name}</p>
                      <span className="rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700">
                        {FRAMEWORK_LABEL[project.framework]}
                      </span>
                    </div>
                    <p className="mt-1 truncate font-mono text-[11px] text-slate-400">
                      {project.rootPath}
                    </p>
                    <p className="mt-1 text-[11px] text-slate-400">
                      接入于 {formatDateTime(project.createdAt)} · 重启后清空
                    </p>
                  </div>
                  <ArrowRight
                    size={16}
                    className="shrink-0 text-slate-300 transition group-hover:text-indigo-500"
                  />
                </button>
              ))}
            </div>
          </section>
        </div>

        <div className="mt-8 flex items-start gap-3 rounded-xl border border-slate-200 bg-white p-4">
          <Database size={16} className="mt-0.5 shrink-0 text-slate-400" />
          <p className="text-xs leading-relaxed text-slate-500">
            M1 已接入：启动分析后由 ast-worker 对源码生成真实组件图（未分析时显示示例 IR）。
            运行时采集（M2）与 AI 语义层（M3）尚未接入；存储为进程内内存，重启后清空。
          </p>
          <Sparkles size={16} className="mt-0.5 shrink-0 text-amber-400" />
        </div>
      </main>
    </div>
  );
}
