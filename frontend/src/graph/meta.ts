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
    badge: "border-sky-400/25 bg-sky-400/10 text-sky-300",
    border: "border-sky-400/25",
    color: "#38bdf8",
  },
  "server-component": {
    label: "服务端组件",
    badge: "border-violet-400/25 bg-violet-400/10 text-violet-300",
    border: "border-violet-400/25",
    color: "#a78bfa",
  },
  "server-action": {
    label: "Server Action",
    badge: "border-fuchsia-400/25 bg-fuchsia-400/10 text-fuchsia-300",
    border: "border-fuchsia-400/25",
    color: "#e879f9",
  },
  element: {
    label: "JSX 元素",
    badge: "border-white/10 bg-white/[0.05] text-slate-200",
    border: "border-white/10",
    color: "#94a3b8",
  },
  hook: {
    label: "Hook",
    badge: "border-cyan-400/25 bg-cyan-400/10 text-cyan-300",
    border: "border-cyan-400/25",
    color: "#22d3ee",
  },
  state: {
    label: "状态",
    badge: "border-amber-400/25 bg-amber-400/10 text-amber-300",
    border: "border-amber-400/25",
    color: "#fbbf24",
  },
  fn: {
    label: "函数",
    badge: "border-teal-400/25 bg-teal-400/10 text-teal-300",
    border: "border-teal-400/25",
    color: "#2dd4bf",
  },
  api: {
    label: "接口",
    badge: "border-emerald-400/25 bg-emerald-400/10 text-emerald-300",
    border: "border-emerald-400/25",
    color: "#34d399",
  },
  route: {
    label: "路由",
    badge: "border-orange-400/25 bg-orange-400/10 text-orange-300",
    border: "border-orange-400/25",
    color: "#fb923c",
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
  contains: "包含",
  navigates: "跳转",
};

export const SOURCE_META: Record<
  Source,
  { label: string; description: string; badge: string; panel: string; color: string }
> = {
  runtime: {
    label: "运行时事实",
    description: "浏览器中真实观测",
    badge: "border-emerald-400/25 bg-emerald-400/10 text-emerald-300",
    panel: "border-emerald-400/20 bg-emerald-400/[0.06]",
    color: "#34d399",
  },
  static: {
    label: "静态事实",
    description: "源码解析结果",
    badge: "border-white/10 bg-white/[0.06] text-slate-200",
    panel: "border-white/[0.07] bg-white/[0.02]",
    color: "#94a3b8",
  },
  ai: {
    label: "AI 推断",
    description: "需人工确认或驳回",
    badge: "border-amber-400/25 bg-amber-400/10 text-amber-300",
    panel: "border-amber-400/20 bg-amber-400/[0.06]",
    color: "#fbbf24",
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
