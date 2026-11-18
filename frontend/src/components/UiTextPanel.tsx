/**
 * 可见文案面板：列出组件里用户能在页面上看到的文字。
 *
 * 这块信息存在的理由很具体：需求文档说的是「搜索用户」「姓名列」，代码里
 * 对应的是 `placeholder="搜索用户"` 和 `{ title: "姓名" }`。符号名（`ProTable`、
 * `onChange`）对不上需求语言，文案是两者唯一的交集。
 *
 * 两类条目分开显示，因为它们的可信度完全不同：
 *  - **texts**：语法事实，源码里有确切字面量，带行列号；
 *  - **dynamic**：文案来自变量（`<h1>{title}</h1>`），字面量不在本文件。
 *    把它渲染成「动态」而不是隐藏起来，读者才不会以为这个组件真的没有标题。
 */

import type { IRNode } from "@uicodeanalysis/contracts";

interface UiTextItem {
  text: string;
  kind: string;
  via: string | null;
  loc: { line: number; col: number } | null;
}

interface DynamicItem {
  expr: string;
  loc: { line: number; col: number } | null;
}

interface UiTextInfo {
  texts: UiTextItem[];
  dynamic: DynamicItem[];
  truncated: number;
}

/** 来源分类的展示名。未知取值原样显示，不吞掉契约外的数据。 */
const KIND_LABEL: Record<string, string> = {
  text: "文本",
  attr: "属性",
  object: "配置",
  array: "选项",
  template: "模板",
  i18n: "词条",
};

function readLoc(raw: unknown): { line: number; col: number } | null {
  if (!raw || typeof raw !== "object") return null;
  const candidate = raw as Record<string, unknown>;
  const line = candidate.line;
  const col = candidate.col;
  if (typeof line !== "number" || typeof col !== "number") return null;
  return { line, col };
}

/** 把 attrs.uiText 收窄成结构化数据；形状不对时返回 null。 */
function readUiText(node: IRNode): UiTextInfo | null {
  const raw = node.attrs.uiText;
  if (!raw || typeof raw !== "object") return null;

  const candidate = raw as Record<string, unknown>;

  const texts: UiTextItem[] = [];
  if (Array.isArray(candidate.texts)) {
    for (const item of candidate.texts) {
      if (!item || typeof item !== "object") continue;
      const entry = item as Record<string, unknown>;
      if (typeof entry.text !== "string" || entry.text.length === 0) continue;
      texts.push({
        text: entry.text,
        kind: typeof entry.kind === "string" ? entry.kind : "text",
        via: typeof entry.via === "string" && entry.via.length > 0 ? entry.via : null,
        loc: readLoc(entry.loc),
      });
    }
  }

  const dynamic: DynamicItem[] = [];
  if (Array.isArray(candidate.dynamic)) {
    for (const item of candidate.dynamic) {
      if (!item || typeof item !== "object") continue;
      const entry = item as Record<string, unknown>;
      if (typeof entry.expr !== "string" || entry.expr.length === 0) continue;
      dynamic.push({ expr: entry.expr, loc: readLoc(entry.loc) });
    }
  }

  const truncated = typeof candidate.truncated === "number" ? candidate.truncated : 0;

  if (texts.length === 0 && dynamic.length === 0) return null;
  return { texts, dynamic, truncated };
}

export function UiTextPanel({ node }: { node: IRNode }) {
  const info = readUiText(node);
  if (!info) return null;

  return (
    <section>
      <h3 className="app-section-label flex items-center gap-2">
        可见文案
        <span className="rounded bg-white/[0.06] px-1 text-[10px] text-slate-300">
          {info.texts.length}
        </span>
      </h3>

      {info.texts.length > 0 ? (
        <ul className="mt-2 space-y-1">
          {info.texts.map((item, index) => (
            <li
              key={`${item.text}-${index}`}
              className="flex items-start gap-2 rounded-lg border border-white/[0.07] bg-white/[0.02] px-2 py-1 text-xs"
            >
              <span className="mt-0.5 shrink-0 rounded bg-white/[0.06] px-1 text-[10px] text-slate-300">
                {KIND_LABEL[item.kind] ?? item.kind}
              </span>
              <span className="min-w-0 flex-1 break-all text-slate-200">{item.text}</span>
              <span className="shrink-0 font-mono text-[10px] text-slate-400">
                {item.via ? `${item.via} · ` : ""}
                {item.loc ? `L${item.loc.line}` : ""}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-slate-400">没有字面量文案</p>
      )}

      {info.dynamic.length > 0 ? (
        <div className="mt-2 rounded-lg border border-white/[0.06] bg-white/[0.02] px-2 py-1.5">
          <p className="text-[10px] leading-relaxed text-slate-400">
            以下位置的文案来自变量，字面量不在本文件（不做跨文件求值）：
          </p>
          <ul className="mt-1 space-y-0.5">
            {info.dynamic.map((item, index) => (
              <li
                key={`${item.expr}-${index}`}
                className="flex items-center gap-2 font-mono text-[10px] text-slate-400"
              >
                <span className="min-w-0 flex-1 truncate">{item.expr}</span>
                {item.loc ? <span className="shrink-0">L{item.loc.line}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {info.truncated > 0 ? (
        <p className="mt-1.5 text-[10px] leading-relaxed text-amber-400">
          另有 {info.truncated} 条超出上限未列出，检索时可能漏命中。
        </p>
      ) : null}
    </section>
  );
}
