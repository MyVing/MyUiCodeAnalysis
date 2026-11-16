/**
 * 封装注解面板：说明组件是某个 UI 库组件的二次封装。
 *
 * 面板回答三个问题：
 *  - 它包着谁（target）
 *  - props 怎么改名（props 映射）
 *  - 这条结论是否真的连得上（`resolved`）
 *
 * `resolved: false` 必须显示且显眼：配置声明了映射，但渲染分析器没有在封装
 * 内部找到目标标签（常见于动态组件）。那不是"分析成功"，是注释与图不一致。
 */

import type { IRNode } from "@uicodeanalysis/contracts";

interface WrapperInfo {
  target: string;
  display: string;
  package: string;
  props: [string, string][];
  resolved: boolean;
  description: string | null;
}

/** 把 attrs.wrapper 收窄成 WrapperInfo；形状不对时返回 null。 */
function readWrapper(node: IRNode): WrapperInfo | null {
  const raw = node.attrs.wrapper;
  if (!raw || typeof raw !== "object") return null;

  const candidate = raw as Record<string, unknown>;
  if (typeof candidate.target !== "string" || candidate.target.length === 0) return null;

  const props: [string, string][] = [];
  if (candidate.props && typeof candidate.props === "object") {
    for (const [key, value] of Object.entries(candidate.props)) {
      if (typeof value === "string") props.push([key, value]);
    }
  }

  return {
    target: candidate.target,
    display: typeof candidate.display === "string" ? candidate.display : candidate.target,
    package: typeof candidate.package === "string" ? candidate.package : "",
    props,
    resolved: candidate.resolved === true,
    description: typeof candidate.description === "string" ? candidate.description : null,
  };
}

export function WrapperPanel({ node }: { node: IRNode }) {
  const wrapper = readWrapper(node);
  if (!wrapper) return null;

  return (
    <section>
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-slate-400">二次封装</h3>

      <div className="mt-2 rounded-lg border border-slate-200 px-2.5 py-2 text-xs">
        <p className="text-slate-700">
          封装 <span className="font-mono text-[11px]">{wrapper.package}</span> 的{" "}
          <span className="font-mono text-[11px] font-medium">{wrapper.display}</span>
        </p>
        <p className="mt-1 break-all font-mono text-[10px] text-slate-400">{wrapper.target}</p>

        {wrapper.description ? (
          <p className="mt-1.5 leading-relaxed text-[11px] text-slate-500">{wrapper.description}</p>
        ) : null}

        {wrapper.props.length > 0 ? (
          <dl className="mt-2 divide-y divide-slate-100 border-t border-slate-100">
            {wrapper.props.map(([from, to]) => (
              <div key={from} className="flex gap-3 py-1 text-[11px]">
                <dt className="w-28 shrink-0 break-all font-mono text-slate-500">{from}</dt>
                <dd className="min-w-0 break-all font-mono text-slate-700">→ {to}</dd>
              </div>
            ))}
          </dl>
        ) : null}

        {wrapper.resolved ? null : (
          <p className="mt-2 rounded bg-amber-50 px-2 py-1 text-[10px] leading-relaxed text-amber-700">
            渲染边没有连到这个组件（常见于动态组件），注解仅来自映射配置。
          </p>
        )}
      </div>
    </section>
  );
}
