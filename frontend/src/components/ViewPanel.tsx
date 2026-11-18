/**
 * 视图面板：说明「这个元素在界面上是什么」。
 *
 * 与 EventPanel 分工明确——EventPanel 回答「点了会发生什么」，本面板回答
 * 「需求里的人指的是它」。两者服务同一个 element 节点，一个 node 可能只有其一。
 *
 * 三处必须如实呈现：
 *  - **角色**：控件 / 容器 / 文本 / 链接。这是需求措辞与代码之间唯一的桥。
 *  - **动态文案**：`<h1>{title}</h1>` 的文案定不下来，但「这是一级标题」成立。
 *    显示「由变量决定」而不是留空，否则读图的人会以为元素没有文案。
 *  - **表格列**：业务封装表格的唯一结构线索，需求里的列名就靠它对上。
 *
 * 与其余面板一致做运行时收窄：attrs 契约上是自由扩展，形状不对就整块不渲染。
 */

import type { IRNode } from "@uicodeanalysis/contracts";

type ViewRole = "control" | "container" | "text" | "link";

const ROLE_TEXT: Record<ViewRole, { label: string; description: string }> = {
  control: { label: "控件", description: "可操作的元素" },
  container: { label: "容器", description: "包裹其他内容的区域" },
  text: { label: "文本", description: "给人阅读的文案" },
  link: { label: "链接", description: "可点击跳转的入口" },
};

interface Column {
  title: string;
  dataIndex: string | null;
}

interface ViewInfo {
  tag: string;
  /** host：HTML 标签；component：组件实例 */
  target: "host" | "component";
  role: ViewRole;
  viewText: string | null;
  textDynamic: boolean;
  columns: Column[] | null;
}

function isRole(value: unknown): value is ViewRole {
  return value === "control" || value === "container" || value === "text" || value === "link";
}

function readColumns(raw: unknown): Column[] | null {
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const columns: Column[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const { title, dataIndex } = item as Record<string, unknown>;
    if (typeof title !== "string" || title.length === 0) continue;
    columns.push({ title, dataIndex: typeof dataIndex === "string" ? dataIndex : null });
  }
  return columns.length > 0 ? columns : null;
}

/** 把 attrs 收窄成 ViewInfo；没有 viewRole 说明不是视图层产出的节点。 */
function readView(node: IRNode): ViewInfo | null {
  if (node.kind !== "element") return null;

  const role = node.attrs.viewRole;
  if (!isRole(role)) return null;

  const tag = typeof node.attrs.tag === "string" ? node.attrs.tag : node.name;
  return {
    tag,
    target: node.attrs.target === "component" ? "component" : "host",
    role,
    viewText: typeof node.attrs.viewText === "string" ? node.attrs.viewText : null,
    textDynamic: node.attrs.viewTextDynamic === true,
    columns: readColumns(node.attrs.columns),
  };
}

export function ViewPanel({ node }: { node: IRNode }) {
  const view = readView(node);
  if (!view) return null;

  const role = ROLE_TEXT[view.role];

  return (
    <section data-testid="view-panel">
      <h3 className="app-section-label">界面</h3>

      <div className="mt-2 rounded-lg border border-white/[0.07] bg-white/[0.02] px-2.5 py-2 text-xs">
        <div className="flex items-start justify-between gap-2">
          <span className="break-all font-mono text-[11px] text-slate-200">
            &lt;{view.tag}&gt;
          </span>
          <span
            className="shrink-0 rounded border border-white/10 bg-white/[0.05] px-1.5 py-0.5 text-[10px] text-slate-300"
            title={role.description}
          >
            {view.target === "host" ? "HTML 标签" : "组件"} · {role.label}
          </span>
        </div>

        {view.viewText ? (
          <p className="mt-1.5 text-[11px] leading-relaxed text-slate-100">「{view.viewText}」</p>
        ) : view.textDynamic ? (
          <p className="mt-1.5 text-[11px] leading-relaxed text-amber-300">
            文案由变量决定
            <span className="text-slate-400">（结构成立，内容静态不可知）</span>
          </p>
        ) : null}

        {view.columns ? (
          <div className="mt-2 border-t border-white/[0.06] pt-1.5">
            <p className="text-[10px] uppercase tracking-[0.1em] text-slate-400">
              表格列 · {view.columns.length}
            </p>
            <ul className="mt-1 space-y-0.5">
              {view.columns.map((column, index) => (
                <li key={`${column.title}-${index}`} className="flex gap-2 text-[11px]">
                  <span className="min-w-0 break-all text-slate-200">{column.title}</span>
                  {column.dataIndex ? (
                    <span className="shrink-0 font-mono text-[10px] text-slate-500">
                      {column.dataIndex}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </section>
  );
}
