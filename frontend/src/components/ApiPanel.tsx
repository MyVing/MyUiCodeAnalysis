/**
 * API / 函数面板：把请求链路的两端渲染出来。
 *
 * - `api` 节点：请求方法 + URL。位置来自「首次发现该接口的调用点」而不是接口
 *   自身的定义（接口没有定义），所以这里注明来源，避免和组件位置混淆。
 * - `fn` 节点：函数在请求链路里的角色。
 *
 * 和 HookPanel 一样，字段缺失时逐项跳过而不是整块不渲染——这段数据来自分析器，
 * 缺字段说明版本不一致，把已有的部分显示出来比什么都不显示更利于排查。
 */

import type { IRNode } from "@uicodeanalysis/contracts";

/** 字段存在且是非空字符串才算有值。 */
function asText(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

const KIND_TITLE: Record<string, string> = {
  api: "接口",
  fn: "请求链路",
};

export function ApiPanel({ node }: { node: IRNode }) {
  const title = KIND_TITLE[node.kind];
  if (!title) return null;

  const method = asText(node.attrs.method);
  const url = asText(node.attrs.url);
  const role = asText(node.attrs.role);

  const rows: { label: string; value: string }[] = [];
  if (method) rows.push({ label: "方法", value: method });
  if (url) rows.push({ label: "URL", value: url });
  if (role) rows.push({ label: "角色", value: role });

  return (
    <section>
      <h3 className="app-section-label">{title}</h3>

      {rows.length === 0 ? (
        <p className="mt-2 text-xs text-slate-400">无附加信息</p>
      ) : (
        <dl className="mt-2 divide-y divide-white/[0.05] rounded-lg border border-white/[0.07] bg-white/[0.02]">
          {rows.map((row) => (
            <div key={row.label} className="flex gap-3 px-2.5 py-1.5 text-xs">
              <dt className="w-14 shrink-0 text-slate-400">{row.label}</dt>
              <dd className="min-w-0 break-all font-mono text-[11px] text-slate-200">
                {row.value}
              </dd>
            </div>
          ))}
        </dl>
      )}

      {node.loc ? (
        <p className="mt-1 font-mono text-[10px] text-slate-400">
          {node.kind === "api" ? "首次发现于 " : ""}
          {node.file}:{node.loc.line}:{node.loc.col}
        </p>
      ) : null}
    </section>
  );
}
