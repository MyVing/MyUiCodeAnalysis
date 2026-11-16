import { ApiPanel } from "@/components/ApiPanel";
import { EventPanel } from "@/components/EventPanel";
import { EvidencePanel } from "@/components/EvidencePanel";
import { HookPanel } from "@/components/HookPanel";
import { PropsPanel } from "@/components/PropsPanel";
import { UiTextPanel } from "@/components/UiTextPanel";
import { WrapperPanel } from "@/components/WrapperPanel";
import { KIND_META } from "@/graph/meta";
import type { IRNode } from "@uicodeanalysis/contracts";

interface NodeInspectorProps {
  node?: IRNode;
}

/**
 * 由专门面板渲染的 attrs，不在「扩展属性」里重复成一段 JSON。
 *
 * 只在节点类型对得上时排除：hook 字段出现在组件节点上属于契约外数据，
 * 那种情况应当照实显示出来，而不是悄悄藏掉。
 */
const DEDICATED_ATTRS: Partial<Record<IRNode["kind"], string[]>> = {
  component: ["props", "wrapper", "uiText"],
  element: ["events"],
  hook: ["hookName", "custom", "deps", "binding"],
  state: ["hookName", "setter", "typeText", "initial"],
  api: ["method", "url"],
  fn: ["role"],
};

function renderAttrValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value);
}

export function NodeInspector({ node }: NodeInspectorProps) {
  if (!node) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center text-xs text-slate-400">
        点击图中的节点查看属性、证据与源码位置。
      </div>
    );
  }

  const meta = KIND_META[node.kind];
  const dedicated = new Set(DEDICATED_ATTRS[node.kind] ?? []);
  const attrs = Object.entries(node.attrs).filter(([key]) => !dedicated.has(key));

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-slate-200 p-4">
        <span className={`inline-block rounded border px-1.5 py-0.5 text-[10px] ${meta.badge}`}>
          {meta.label}
        </span>
        <h2 className="mt-2 text-base font-semibold text-slate-900">{node.name}</h2>
        <p className="mt-1 break-all font-mono text-[11px] text-slate-500">{node.id}</p>
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto p-4">
        <PropsPanel node={node} />
        <HookPanel node={node} />
        <ApiPanel node={node} />
        <EventPanel node={node} />
        <WrapperPanel node={node} />
        <UiTextPanel node={node} />

        <section>
          <h3 className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
            扩展属性
          </h3>
          {attrs.length === 0 ? (
            <p className="mt-2 text-xs text-slate-400">无扩展属性</p>
          ) : (
            <dl className="mt-2 divide-y divide-slate-100 rounded-lg border border-slate-200">
              {attrs.map(([key, value]) => (
                <div key={key} className="flex gap-3 px-2.5 py-1.5 text-xs">
                  <dt className="w-20 shrink-0 font-mono text-[11px] text-slate-400">{key}</dt>
                  <dd className="min-w-0 break-all font-mono text-[11px] text-slate-700">
                    {renderAttrValue(value)}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </section>

        <section>
          <h3 className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
            证据（{node.evidence.length}）
          </h3>
          <div className="mt-2">
            <EvidencePanel evidence={node.evidence} />
          </div>
        </section>
      </div>

      <p className="border-t border-slate-200 p-3 text-[11px] leading-relaxed text-slate-400">
        每条结论都携带来源、置信度与代码位置；AI 推断不会被当作代码事实。
      </p>
    </div>
  );
}
