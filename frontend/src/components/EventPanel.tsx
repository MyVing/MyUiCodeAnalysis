/**
 * 事件面板：把 element 节点上的 attrs.events 渲染成事件表。
 *
 * 三件事必须在界面上说清楚，否则读图的人会误判：
 *  - **落到谁**：targets 为空不等于分析失败。处理函数由 props 注入时逻辑在父组件，
 *    这条链在子组件里本来就不该连出去——所以 `injected` 单独标注，文案是
 *    「由父组件注入」，不是「未解析」。
 *  - **形态**：内联函数 vs 具名引用。内联时 handlerName 为 null 是事实，不是缺字段。
 *  - **原文**：handler 是截断后的源码片段，人需要它来对照代码。
 *
 * 和 PropsPanel 一样做运行时收窄：attrs 契约上是自由扩展，形状不对就整块不渲染。
 */

import type { IRNode } from "@uicodeanalysis/contracts";

interface EventTarget {
  id: string;
  kind: string;
}

interface EventBinding {
  name: string;
  handler: string;
  kind: string;
  handlerName: string | null;
  targets: EventTarget[];
  injected: boolean;
}

const KIND_TEXT: Record<string, string> = {
  "inline-function": "内联函数",
  reference: "具名引用",
  other: "其他表达式",
};

/** 把 attrs.events 收窄成 EventBinding[]；形状不对时返回 null。 */
function readEvents(node: IRNode): EventBinding[] | null {
  const raw = node.attrs.events;
  if (!Array.isArray(raw) || raw.length === 0) return null;

  const events: EventBinding[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const candidate = item as Partial<EventBinding>;
    if (typeof candidate.name !== "string" || typeof candidate.handler !== "string") continue;

    const targets = Array.isArray(candidate.targets)
      ? candidate.targets.flatMap((target) => {
          if (!target || typeof target !== "object") return [];
          const { id, kind } = target as Partial<EventTarget>;
          return typeof id === "string" && typeof kind === "string" ? [{ id, kind }] : [];
        })
      : [];

    events.push({
      name: candidate.name,
      handler: candidate.handler,
      kind: typeof candidate.kind === "string" ? candidate.kind : "other",
      handlerName: typeof candidate.handlerName === "string" ? candidate.handlerName : null,
      targets,
      injected: candidate.injected === true,
    });
  }

  return events.length > 0 ? events : null;
}

/** 落点说明：注入 / 已解析到具体节点 / 无法判定。 */
function describeTargets(event: EventBinding): string {
  if (event.targets.length > 0) {
    return event.targets.map((target) => `${target.kind}：${target.id}`).join("、");
  }
  return event.injected ? "由父组件注入" : "无法静态判定";
}

export function EventPanel({ node }: { node: IRNode }) {
  if (node.kind !== "element") return null;

  const events = readEvents(node);
  // 没有事件清单说明这不是事件分析产出的元素节点，整块不渲染
  if (!events) return null;

  return (
    <section>
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
        事件（{events.length}）
      </h3>

      <ul className="mt-2 space-y-2">
        {events.map((event, index) => (
          <li
            key={`${event.name}-${index}`}
            className="rounded-lg border border-slate-200 px-2.5 py-2 text-xs"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-[11px] font-medium text-slate-800">{event.name}</span>
              <span className="shrink-0 text-[10px] text-slate-400">
                {KIND_TEXT[event.kind] ?? event.kind}
              </span>
            </div>
            <p className="mt-1 break-all font-mono text-[10px] text-slate-500">{event.handler}</p>
            <p
              className={`mt-1 break-all text-[10px] ${
                event.targets.length > 0 ? "text-slate-600" : "text-amber-600"
              }`}
            >
              落到：{describeTargets(event)}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}
