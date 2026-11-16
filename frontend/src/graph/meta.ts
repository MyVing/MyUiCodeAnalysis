import type { EdgeKind, Evidence, NodeKind, Source } from "@uicodeanalysis/contracts";

interface KindMeta {
  label: string;
  badge: string;
  border: string;
  color: string;
}

export const KIND_META: Record<NodeKind, KindMeta> = {
  component: {
    label: "客户端组件",
    badge: "border-sky-200 bg-sky-50 text-sky-700",
    border: "border-sky-200",
    color: "#0ea5e9",
  },
  "server-component": {
    label: "服务端组件",
    badge: "border-violet-200 bg-violet-50 text-violet-700",
    border: "border-violet-200",
    color: "#8b5cf6",
  },
  "server-action": {
    label: "Server Action",
    badge: "border-fuchsia-200 bg-fuchsia-50 text-fuchsia-700",
    border: "border-fuchsia-200",
    color: "#d946ef",
  },
  element: {
    label: "JSX 元素",
    badge: "border-slate-200 bg-slate-50 text-slate-600",
    border: "border-slate-200",
    color: "#64748b",
  },
  hook: {
    label: "Hook",
    badge: "border-cyan-200 bg-cyan-50 text-cyan-700",
    border: "border-cyan-200",
    color: "#06b6d4",
  },
  state: {
    label: "状态",
    badge: "border-amber-200 bg-amber-50 text-amber-700",
    border: "border-amber-200",
    color: "#f59e0b",
  },
  fn: {
    label: "函数",
    badge: "border-teal-200 bg-teal-50 text-teal-700",
    border: "border-teal-200",
    color: "#14b8a6",
  },
  api: {
    label: "接口",
    badge: "border-emerald-200 bg-emerald-50 text-emerald-700",
    border: "border-emerald-200",
    color: "#10b981",
  },
  route: {
    label: "路由",
    badge: "border-orange-200 bg-orange-50 text-orange-700",
    border: "border-orange-200",
    color: "#f97316",
  },
};

export const EDGE_LABEL: Record<EdgeKind, string> = {
  renders: "渲染",
  calls: "调用",
  reads: "读取",
  writes: "写入",
  requests: "请求",
  uses: "使用",
  boundary: "客户端边界",
};

export const SOURCE_META: Record<
  Source,
  { label: string; description: string; badge: string; panel: string; color: string }
> = {
  runtime: {
    label: "运行时事实",
    description: "浏览器中真实观测",
    badge: "border-emerald-200 bg-emerald-50 text-emerald-700",
    panel: "border-emerald-200 bg-emerald-50/50",
    color: "#059669",
  },
  static: {
    label: "静态事实",
    description: "源码解析结果",
    badge: "border-slate-300 bg-slate-100 text-slate-700",
    panel: "border-slate-200 bg-white",
    color: "#94a3b8",
  },
  ai: {
    label: "AI 推断",
    description: "需人工确认或驳回",
    badge: "border-amber-300 bg-amber-50 text-amber-800",
    panel: "border-amber-200 bg-amber-50/60",
    color: "#f59e0b",
  },
};

const SOURCE_PRIORITY: Record<Source, number> = { runtime: 3, static: 2, ai: 1 };

export function primarySource(evidence: readonly Evidence[]): Source {
  return evidence.reduce<Source>(
    (current, item) =>
      SOURCE_PRIORITY[item.source] > SOURCE_PRIORITY[current] ? item.source : current,
    "ai",
  );
}

export function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}
