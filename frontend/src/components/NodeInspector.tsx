import { Code2, Loader2 } from "lucide-react";

import { useSourceExcerpt } from "@/api/queries";
import { ApiPanel } from "@/components/ApiPanel";
import { EventPanel } from "@/components/EventPanel";
import { EvidencePanel } from "@/components/EvidencePanel";
import { HookPanel } from "@/components/HookPanel";
import { PropsPanel } from "@/components/PropsPanel";
import { UiTextPanel } from "@/components/UiTextPanel";
import { ViewPanel } from "@/components/ViewPanel";
import { WrapperPanel } from "@/components/WrapperPanel";
import { EDGE_LABEL, KIND_META } from "@/graph/meta";
import type { IREdge, IRGraph, IRNode } from "@uicodeanalysis/contracts";

interface NodeInspectorProps {
  node?: IRNode;
  /** 全图（不裁剪）：用来还原当前节点的上下游调用关系 */
  graph?: IRGraph;
  projectId?: string;
  /** 示例 IR：节点路径只是演示数据，不去仓库里读源码 */
  sample?: boolean;
  /** 点击依赖节点时联动选中并聚焦 */
  onSelectNode?: (nodeId: string) => void;
}

/**
 * 由专门面板渲染的 attrs，不在「扩展属性」里重复成一段 JSON。
 *
 * 只在节点类型对得上时排除：hook 字段出现在组件节点上属于契约外数据，
 * 那种情况应当照实显示出来，而不是悄悄藏掉。
 */
const DEDICATED_ATTRS: Partial<Record<IRNode["kind"], string[]>> = {
  component: ["props", "wrapper", "uiText"],
  hook: ["hookName", "custom", "deps", "binding"],
  state: ["hookName", "setter", "typeText", "initial"],
  api: ["method", "url"],
  fn: ["role"],
};

/**
 * 视图层结构化属性：ViewPanel 会渲染它们。
 *
 * 不能无条件排除——`tag` / `target` 是事件层也写的字段。元素没有 `viewRole`
 * 时 ViewPanel 整块不渲染，此时这些属性是唯一能看到的信息，藏起来就是丢数据。
 */
const VIEW_ATTRS = ["tag", "target", "viewRole", "viewText", "viewTextDynamic", "columns"];

/** 该节点真正被某个专属面板接手了吗？接手了才可以从扩展属性里剔除。 */
function dedicatedAttrs(node: IRNode): Set<string> {
  const keys = new Set(DEDICATED_ATTRS[node.kind] ?? []);
  // events 由 EventPanel 渲染，条件是 attrs.events 是非空数组
  if (node.kind === "element" && Array.isArray(node.attrs.events)) keys.add("events");
  // 视图属性由 ViewPanel 渲染，条件是 attrs.viewRole 合法
  if (node.kind === "element" && typeof node.attrs.viewRole === "string") {
    for (const key of VIEW_ATTRS) keys.add(key);
  }
  return keys;
}

function renderAttrValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value);
}

/** 节点自身位置：优先节点 file + 静态证据行号，其次任一证据的文件 */
function sourceLocation(node: IRNode): { file: string; line?: number } | null {
  if (node.file) {
    const evidence = node.evidence.find((item) => item.file === node.file && item.loc);
    return { file: node.file, line: evidence?.loc?.line ?? node.loc?.line };
  }
  const evidence = node.evidence.find((item) => item.file);
  return evidence?.file ? { file: evidence.file, line: evidence.loc?.line } : null;
}

export function NodeInspector({
  node,
  graph,
  projectId,
  sample = false,
  onSelectNode,
}: NodeInspectorProps) {
  const location = node ? sourceLocation(node) : null;
  const sourceQuery = useSourceExcerpt(
    sample ? "" : (projectId ?? ""),
    location?.file ?? null,
    location?.line,
  );
  const excerpt = sourceQuery.data;

  if (!node) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center text-xs text-slate-400">
        点击图中的节点查看属性、证据与源码。
      </div>
    );
  }

  const meta = KIND_META[node.kind];
  const dedicated = dedicatedAttrs(node);
  const attrs = Object.entries(node.attrs).filter(([key]) => !dedicated.has(key));

  const outgoing = graph?.edges.filter((edge) => edge.from === node.id) ?? [];
  const incoming = graph?.edges.filter((edge) => edge.to === node.id) ?? [];
  const nameOf = (nodeId: string) =>
    graph?.nodes.find((item) => item.id === nodeId)?.name ?? nodeId;

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-white/[0.06] p-4">
        <span className={`inline-block rounded border px-1.5 py-0.5 text-[10px] ${meta.badge}`}>
          {meta.label}
        </span>
        <h2 className="mt-2 text-base font-semibold text-white">{node.name}</h2>
        <p className="mt-1 break-all font-mono text-[11px] text-slate-400">{node.id}</p>
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto p-4">
        <PropsPanel node={node} />
        <HookPanel node={node} />
        <ApiPanel node={node} />
        {/* 界面在前、事件在后：先知道「它是什么」，再知道「点了会怎样」 */}
        <ViewPanel node={node} />
        <EventPanel node={node} />
        <WrapperPanel node={node} />
        <UiTextPanel node={node} />

        {graph && (incoming.length > 0 || outgoing.length > 0) && (
          <section data-testid="node-dependencies">
            <h3 className="app-section-label">依赖与调用</h3>
            <div className="mt-2 space-y-2">
              <EdgeList
                title="上游（谁依赖它）"
                edges={incoming}
                other={nodeIdFrom("from")}
                nameOf={nameOf}
                onSelectNode={onSelectNode}
              />
              <EdgeList
                title="下游（它依赖谁）"
                edges={outgoing}
                other={nodeIdFrom("to")}
                nameOf={nameOf}
                onSelectNode={onSelectNode}
              />
            </div>
          </section>
        )}

        {location && (
          <section data-testid="node-source">
            <h3 className="app-section-label flex items-center gap-1.5">
              <Code2 size={12} />
              前端逻辑（源码片段）
            </h3>
            <p className="mt-1 break-all font-mono text-[10px] text-slate-400">
              {location.file}
              {location.line ? `:${location.line}` : ""}
            </p>

            {sample ? (
              <p
                className="mt-2 rounded-lg border border-amber-400/20 bg-amber-400/[0.08] px-2.5 py-1.5 text-[11px] text-amber-200"
                data-testid="source-sample-note"
              >
                当前是示例 IR，节点路径不在真实仓库中。点击右上角「开始分析」生成真实图后即可读取源码。
              </p>
            ) : (
              <>
                {sourceQuery.isLoading && (
                  <p className="mt-2 flex items-center gap-1.5 text-[11px] text-slate-400">
                    <Loader2 size={12} className="animate-spin" />
                    正在读取源码…
                  </p>
                )}

                {sourceQuery.isError && (
                  <p className="mt-2 text-[11px] text-rose-300" data-testid="source-error">
                    {sourceQuery.error instanceof Error
                      ? sourceQuery.error.message
                      : "读取源码失败"}
                  </p>
                )}

                {excerpt && (
                  <div className="mt-2 overflow-hidden rounded-lg border border-white/[0.07] bg-black/30">
                    <div className="flex items-center justify-between border-b border-white/[0.06] px-2.5 py-1 text-[10px] text-slate-400">
                      <span className="font-mono">
                        L{excerpt.startLine}–L{excerpt.endLine}
                      </span>
                      {excerpt.truncated && <span>已截断</span>}
                    </div>
                    <pre className="max-h-72 overflow-auto px-2 py-1.5 font-mono text-[10.5px] leading-5 text-slate-200">
                      {excerpt.content.split("\n").map((line, index) => {
                        const lineNo = excerpt.startLine + index;
                        const active = lineNo === location.line;
                        return (
                          <div
                            key={lineNo}
                            className={`flex ${active ? "bg-indigo-400/10" : ""}`}
                          >
                            <span
                              className={`w-9 shrink-0 select-none pr-2 text-right ${
                                active ? "text-indigo-300" : "text-slate-500"
                              }`}
                            >
                              {lineNo}
                            </span>
                            <code className="whitespace-pre">{line || " "}</code>
                          </div>
                        );
                      })}
                    </pre>
                  </div>
                )}
              </>
            )}
          </section>
        )}

        <section>
          <h3 className="app-section-label">扩展属性</h3>
          {attrs.length === 0 ? (
            <p className="mt-2 text-xs text-slate-400">无扩展属性</p>
          ) : (
            <dl className="mt-2 divide-y divide-white/[0.05] rounded-lg border border-white/[0.07] bg-white/[0.02]">
              {attrs.map(([key, value]) => (
                <div key={key} className="flex gap-3 px-2.5 py-1.5 text-xs">
                  <dt className="w-20 shrink-0 font-mono text-[11px] text-slate-400">{key}</dt>
                  <dd className="min-w-0 break-all font-mono text-[11px] text-slate-200">
                    {renderAttrValue(value)}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </section>

        <section>
          <h3 className="app-section-label">证据（{node.evidence.length}）</h3>
          <div className="mt-2">
            <EvidencePanel evidence={node.evidence} />
          </div>
        </section>
      </div>

      <p className="border-t border-white/[0.06] p-3 text-[11px] leading-relaxed text-slate-400">
        每条结论都携带来源、置信度与代码位置；AI 推断不会被当作代码事实。
      </p>
    </div>
  );
}

function nodeIdFrom(side: "from" | "to") {
  return (edge: IREdge) => edge[side];
}

function EdgeList({
  title,
  edges,
  other,
  nameOf,
  onSelectNode,
}: {
  title: string;
  edges: IREdge[];
  other: (edge: IREdge) => string;
  nameOf: (nodeId: string) => string;
  onSelectNode?: (nodeId: string) => void;
}) {
  if (edges.length === 0) {
    return null;
  }

  return (
    <div>
      <p className="text-[10px] uppercase tracking-[0.1em] text-slate-400">
        {title} · {edges.length}
      </p>
      <ul className="mt-1 space-y-1">
        {edges.map((edge) => {
          const targetId = other(edge);
          return (
            <li key={edge.id}>
              <button
                type="button"
                disabled={!onSelectNode}
                onClick={() => onSelectNode?.(targetId)}
                className="flex w-full items-center gap-2 rounded-lg border border-white/[0.07] bg-white/[0.02] px-2 py-1.5 text-left text-xs transition hover:border-indigo-400/30 hover:bg-white/[0.05] disabled:cursor-default"
                data-testid="dependency-item"
              >
                <span className="shrink-0 rounded border border-white/10 bg-white/[0.05] px-1 py-0.5 text-[10px] text-slate-300">
                  {EDGE_LABEL[edge.kind]}
                </span>
                <span className="min-w-0 flex-1 truncate text-slate-200">{nameOf(targetId)}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
