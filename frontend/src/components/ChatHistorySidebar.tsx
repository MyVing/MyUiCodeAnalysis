import { Loader2, MessageSquarePlus, MessagesSquare, Trash2 } from "lucide-react";

import type { ChatSessionSummary } from "@/api/types";
import { formatRelativeTime } from "@/lib/format";

interface ChatHistorySidebarProps {
  sessions: ChatSessionSummary[];
  activeId: string | null;
  pending: boolean;
  loading: boolean;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
  onNew: () => void;
}

/**
 * AI 需求助手左侧历史栏：新建对话 + 会话列表（点击回放、hover 删除）。
 * 服务端每项目最多保留 10 条，超出自动淘汰最旧，前端无需处理上限。
 */
export default function ChatHistorySidebar({
  sessions,
  activeId,
  pending,
  loading,
  onSelect,
  onDelete,
  onNew,
}: ChatHistorySidebarProps) {
  return (
    <aside
      className="flex h-full w-56 shrink-0 flex-col border-r border-white/[0.09] bg-[#0e1219]/85"
      data-testid="chat-history"
    >
      <div className="shrink-0 space-y-2 border-b border-white/[0.09] px-3 py-3">
        <p className="flex items-center gap-1.5 text-[11px] font-medium text-slate-300">
          <MessagesSquare size={13} className="text-indigo-300" />
          历史对话
        </p>
        <button
          type="button"
          onClick={onNew}
          disabled={pending}
          className="app-btn-ghost w-full"
          data-testid="chat-history-new"
        >
          <MessageSquarePlus size={13} />
          新建对话
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
        {loading && (
          <div className="flex items-center gap-2 px-2 py-2 text-[11px] text-slate-400">
            <Loader2 size={12} className="animate-spin" />
            加载中…
          </div>
        )}
        {!loading && sessions.length === 0 && (
          <p className="px-2 py-2 text-[11px] leading-relaxed text-slate-500" data-testid="chat-history-empty">
            还没有历史对话，发送第一条消息后会自动保存（最多保留 10 条）。
          </p>
        )}
        <ul className="space-y-1">
          {sessions.map((session) => {
            const active = session.id === activeId;
            return (
              <li key={session.id} className="group relative">
                <button
                  type="button"
                  onClick={() => onSelect(session.id)}
                  disabled={pending}
                  className={`w-full rounded-lg px-2.5 py-2 pr-8 text-left transition-colors ${
                    active
                      ? "border border-indigo-400/30 bg-indigo-500/[0.12]"
                      : "border border-transparent hover:bg-white/[0.04]"
                  }`}
                  data-testid="chat-history-item"
                >
                  <span className="block truncate text-[11px] text-slate-200" title={session.title}>
                    {session.title}
                  </span>
                  <span className="mt-0.5 block text-[10px] text-slate-500">
                    {formatRelativeTime(session.updatedAt)} · {session.messageCount} 条
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => onDelete(session.id)}
                  disabled={pending}
                  aria-label={`删除会话 ${session.title}`}
                  className="absolute top-1/2 right-1.5 -translate-y-1/2 rounded p-1 text-slate-500 opacity-0 transition-opacity hover:text-rose-300 focus-visible:opacity-100 group-hover:opacity-100"
                  data-testid="chat-history-delete"
                >
                  <Trash2 size={12} />
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </aside>
  );
}
