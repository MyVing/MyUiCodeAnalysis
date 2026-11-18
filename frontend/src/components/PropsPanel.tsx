/**
 * props 面板：把组件节点的 attrs.props 渲染成属性表。
 *
 * 数据来自静态分析（worker），形状由 shared/schemas 的 attrs 自由扩展承载。
 * 这里做一层运行时收窄：attrs 的类型是 Record<string, unknown>，契约上不保证
 * 一定有 props，也不保证形状正确（外部插件可以写别的）。宁可整块不渲染，
 * 也不要把半截数据画出来。
 */

import type { IRNode } from "@uicodeanalysis/contracts";

interface PropField {
  name: string;
  type: string;
  optional: boolean;
  defaultValue: string | null;
  description: string | null;
  file: string | null;
  loc: { line: number; col: number } | null;
}

interface PropsInfo {
  typeName: string | null;
  typeFile: string | null;
  generic: boolean;
  fields: PropField[];
}

/** 把 attrs.props 收窄成 PropsInfo；形状不对时返回 null。 */
function readProps(node: IRNode): PropsInfo | null {
  const raw = node.attrs.props;
  if (!raw || typeof raw !== "object") return null;

  const candidate = raw as Partial<PropsInfo>;
  if (!Array.isArray(candidate.fields)) return null;

  const fields: PropField[] = [];
  for (const item of candidate.fields) {
    if (!item || typeof item !== "object") continue;
    const field = item as Partial<PropField>;
    if (typeof field.name !== "string" || typeof field.type !== "string") continue;

    fields.push({
      name: field.name,
      type: field.type,
      optional: field.optional === true,
      defaultValue: typeof field.defaultValue === "string" ? field.defaultValue : null,
      description: typeof field.description === "string" ? field.description : null,
      file: typeof field.file === "string" ? field.file : null,
      loc:
        field.loc && typeof field.loc.line === "number" && typeof field.loc.col === "number"
          ? { line: field.loc.line, col: field.loc.col }
          : null,
    });
  }

  return {
    typeName: typeof candidate.typeName === "string" ? candidate.typeName : null,
    typeFile: typeof candidate.typeFile === "string" ? candidate.typeFile : null,
    generic: candidate.generic === true,
    fields,
  };
}

export function PropsPanel({ node }: { node: IRNode }) {
  const props = readProps(node);
  // 没有 props 信息（外部组件、尚未分析）时整块不渲染，避免一个空标题占位
  if (!props) return null;

  return (
    <section>
      <h3 className="app-section-label">Props（{props.fields.length}）</h3>

      {props.typeName ? (
        <p className="mt-1 font-mono text-[11px] text-slate-300">
          {props.typeName}
          {props.generic ? "<…>" : ""}
          {props.typeFile ? (
            <span className="ml-1 text-slate-400">来自 {props.typeFile}</span>
          ) : null}
        </p>
      ) : (
        <p className="mt-1 text-[11px] text-slate-400">内联类型，无类型名</p>
      )}

      {props.fields.length === 0 ? (
        <p className="mt-2 text-xs text-slate-400">该组件不声明 props</p>
      ) : (
        <ul className="mt-2 divide-y divide-white/[0.05] rounded-lg border border-white/[0.07] bg-white/[0.02]">
          {props.fields.map((field) => (
            <li key={field.name} className="px-2.5 py-2">
              <div className="flex flex-wrap items-baseline gap-x-1.5 font-mono text-[11px]">
                <span className="font-medium text-slate-200">{field.name}</span>
                {/* 可选性用问号表达，不靠在类型里写 undefined */}
                <span className="text-slate-400">{field.optional ? "?:" : ":"}</span>
                <span className="break-all text-slate-300">{field.type}</span>
                {field.defaultValue ? (
                  <span className="text-slate-400">= {field.defaultValue}</span>
                ) : null}
              </div>

              {field.description ? (
                <p className="mt-0.5 text-[11px] leading-snug text-slate-300">
                  {field.description}
                </p>
              ) : null}

              {field.file && field.loc ? (
                <p className="mt-0.5 font-mono text-[10px] text-slate-400">
                  {field.file}:{field.loc.line}:{field.loc.col}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
