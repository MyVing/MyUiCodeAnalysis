/**
 * Hook / State 面板：把 hook 与 state 节点的 attrs 渲染成一张小表。
 *
 * 两类节点共用一个组件，因为它们的 attrs 形状是刻意对齐的（都有 hookName），
 * 差异只在 state 多了 setter / typeText / initial。字段缺失时逐项跳过而不是
 * 整块不渲染——这段数据来源于分析器，缺字段说明版本不一致，把已有的部分显示
 * 出来比什么都不显示更有利于排查。
 */

import type { IRNode } from "@uicodeanalysis/contracts";

/** 字段存在且是非空字符串才算有值。 */
function asText(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function readDeps(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.filter((item): item is string => typeof item === "string");
}

const KIND_TITLE: Record<string, string> = {
  hook: "Hook",
  state: "State",
};

export function HookPanel({ node }: { node: IRNode }) {
  const title = KIND_TITLE[node.kind];
  if (!title) return null;

  const hookName = asText(node.attrs.hookName);
  const setter = asText(node.attrs.setter);
  const typeText = asText(node.attrs.typeText);
  const initial = asText(node.attrs.initial);
  const binding = asText(node.attrs.binding);
  // deps 为 null 表示"每次渲染都执行"，与"依赖为空数组"是两件事，不能合并显示
  const deps = readDeps(node.attrs.deps);
  const custom = node.attrs.custom === true;

  const rows: { label: string; value: string }[] = [];
  if (hookName) rows.push({ label: "来源", value: hookName });
  if (binding && binding !== node.name) rows.push({ label: "绑定", value: binding });
  if (setter) rows.push({ label: "setter", value: setter });
  if (typeText) rows.push({ label: "类型", value: typeText });
  if (initial !== null) rows.push({ label: "初始值", value: initial });
  if (deps !== null) {
    rows.push({
      label: "依赖",
      value: deps.length === 0 ? "[] （仅挂载/卸载）" : `[${deps.join(", ")}]`,
    });
  }

  return (
    <section>
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{title}</h3>

      {custom ? (
        <p className="mt-1 text-[11px] text-slate-400">自定义 Hook（非 react 内置）</p>
      ) : null}

      {rows.length === 0 ? (
        <p className="mt-2 text-xs text-slate-400">无附加信息</p>
      ) : (
        <dl className="mt-2 divide-y divide-slate-100 rounded-lg border border-slate-200">
          {rows.map((row) => (
            <div key={row.label} className="flex gap-3 px-2.5 py-1.5 text-xs">
              <dt className="w-14 shrink-0 text-slate-400">{row.label}</dt>
              <dd className="min-w-0 break-all font-mono text-[11px] text-slate-700">
                {row.value}
              </dd>
            </div>
          ))}
        </dl>
      )}

      {node.loc ? (
        <p className="mt-1 font-mono text-[10px] text-slate-300">
          {node.file}:{node.loc.line}:{node.loc.col}
        </p>
      ) : null}
    </section>
  );
}
