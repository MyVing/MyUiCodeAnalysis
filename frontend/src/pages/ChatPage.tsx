import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Bot,
  CircleUserRound,
  Loader2,
  MessagesSquare,
  RotateCcw,
  Send,
  Trash2,
} from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, api } from "@/api/client";
import { useChatSessions } from "@/api/queries";
import type { ChatMessage, ChatSessionUpsert } from "@/api/types";
import ChatHistorySidebar from "@/components/ChatHistorySidebar";

const WELCOME = "描述你想做的功能，比如「我想在用户列表页加一个状态筛选」——我会追问细节，帮你把需求聊清楚。项目完成代码分析后，也可以直接问「某个功能的前端交互视图和逻辑」，我会基于项目代码定位页面并生成规格文档。";

/** 前端下载规格文档：Blob 触发浏览器下载，不引入额外依赖 */
function downloadDoc(markdown: string, page: string | undefined) {
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${page ?? "前端交互视图"}.md`;
  anchor.click();
  URL.revokeObjectURL(url);
}

/**
 * 需求对话页：与 AI 聊清楚功能需求，产出草稿后一键带回工作台的输入框。
 * 会话历史落库（每项目最多 10 条，超出由服务端淘汰最旧）：左侧栏可回放历史
 * 会话或删除；requirement-chat 接口仍由本页全量回传 messages，保存是旁路。
 */
export default function ChatPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [draft, setDraft] = useState<string | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const historyQuery = useChatSessions(projectId ?? "");
  const sessions = historyQuery.data?.items ?? [];

  const invalidateHistory = () => {
    if (projectId) {
      void queryClient.invalidateQueries({ queryKey: ["chat-sessions", projectId] });
    }
  };

  const saveSession = useMutation({
    mutationFn: (payload: ChatSessionUpsert) =>
      api.saveChatSession(projectId ?? "", payload),
    onSuccess: (session) => {
      setSessionId(session.id);
      invalidateHistory();
    },
    onError: () => {
      // 保存失败不影响对话继续：侧栏可能少一条，可在下次回复后重试
      setLastError("会话保存失败，本次对话可能不会出现在历史列表中");
    },
  });

  const loadSession = useMutation({
    mutationFn: (targetId: string) => api.getChatSession(targetId),
    onSuccess: (session) => {
      setSessionId(session.id);
      setMessages(session.messages);
      setDraft(session.draft);
      setLastError(null);
    },
    onError: (error) => {
      setLastError(error instanceof Error ? error.message : "加载会话失败");
    },
  });

  const removeSession = useMutation({
    mutationFn: (targetId: string) => api.deleteChatSession(targetId),
    onSuccess: (_result, targetId) => {
      invalidateHistory();
      if (targetId === sessionId) {
        startNewChat();
      }
    },
    onError: (error) => {
      setLastError(error instanceof Error ? error.message : "删除会话失败");
    },
  });

  const chat = useMutation({
    mutationFn: (history: ChatMessage[]) =>
      api.chatRequirement(projectId ?? "", { messages: history }),
    onSuccess: (result, history) => {
      setLastError(null);
      const full: ChatMessage[] = [
        ...history,
        {
          role: "assistant",
          content: result.reply,
          docMarkdown: result.docMarkdown ?? undefined,
          docPage: result.docPage ?? undefined,
        },
      ];
      setMessages(full);
      setDraft(result.draftRequirement ?? null);
      // 回复成功后整段落库：新建（id 省略）或整体替换当前会话
      saveSession.mutate({
        id: sessionId ?? undefined,
        messages: full,
        draft: result.draftRequirement ?? null,
      });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 503) {
        setLastError("模型未启用：需要在后端配置 llm_enabled 与 llm_api_key 后才能使用 AI 对话");
        return;
      }
      setLastError(error instanceof Error ? error.message : "发送失败，请重试");
    },
  });

  // 新消息到达后滚到底部（jsdom 没有 scrollTo，测试环境走 scrollTop 赋值）
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    list.scrollTop = list.scrollHeight;
  }, [messages, chat.isPending]);

  const startNewChat = () => {
    setSessionId(null);
    setMessages([]);
    setDraft(null);
    setLastError(null);
  };

  const send = (event: FormEvent) => {
    event.preventDefault();
    const text = input.trim();
    if (!text || chat.isPending || !projectId) return;
    const history: ChatMessage[] = [...messages, { role: "user", content: text }];
    setMessages(history);
    setInput("");
    setDraft(null);
    chat.mutate(history);
  };

  const clear = () => {
    if (chat.isPending) return;
    // 清空 = 结束当前会话：落库的那份一并删除，避免历史里留下空会话
    if (sessionId) {
      removeSession.mutate(sessionId);
    } else {
      startNewChat();
    }
  };

  const selectSession = (targetId: string) => {
    if (chat.isPending) return;
    loadSession.mutate(targetId);
  };

  const deleteSession = (targetId: string) => {
    if (chat.isPending) return;
    removeSession.mutate(targetId);
  };

  const fillDraft = () => {
    if (!draft || !projectId) return;
    navigate(`/projects/${projectId}`, { state: { draftRequirement: draft } });
  };

  const canSend = input.trim().length > 0 && !chat.isPending && Boolean(projectId);
  const busy = chat.isPending || loadSession.isPending || removeSession.isPending;

  return (
    <div className="flex h-screen overflow-hidden bg-[#0b0f16]">
      <ChatHistorySidebar
        sessions={sessions}
        activeId={sessionId}
        pending={busy}
        loading={historyQuery.isLoading}
        onSelect={selectSession}
        onDelete={deleteSession}
        onNew={startNewChat}
      />

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-white/[0.09] bg-[#0e1219]/85 px-4">
          <div className="flex items-center gap-2">
            <Link
              to={`/projects/${projectId}`}
              className="app-btn-ghost shrink-0"
              data-testid="chat-back"
            >
              <ArrowLeft size={14} />
              返回工作台
            </Link>
            <span className="flex items-center gap-1.5 text-sm font-medium text-slate-200">
              <MessagesSquare size={15} className="text-indigo-300" />
              AI 需求助手
            </span>
          </div>
          <button
            type="button"
            onClick={clear}
            disabled={chat.isPending || messages.length === 0}
            className="app-btn-ghost"
            data-testid="chat-clear"
          >
            <Trash2 size={14} />
            清空对话
          </button>
        </header>

        <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-4" data-testid="chat-list">
          {messages.length === 0 && !chat.isPending && (
            <div className="mx-auto mt-10 max-w-md rounded-xl border border-white/[0.09] bg-white/[0.02] px-4 py-3 text-xs leading-relaxed text-slate-300">
              {WELCOME}
            </div>
          )}

          <ul className="mx-auto max-w-3xl space-y-3">
            {messages.map((message, index) => (
              <li
                key={index}
                className={`flex items-start gap-2 ${
                  message.role === "user" ? "flex-row-reverse" : ""
                }`}
                data-testid="chat-message"
              >
                <span
                  className={`mt-0.5 shrink-0 ${
                    message.role === "user" ? "text-indigo-300" : "text-slate-400"
                  }`}
                >
                  {message.role === "user" ? (
                    <CircleUserRound size={16} />
                  ) : (
                    <Bot size={16} />
                  )}
                </span>
                <div
                  className={`max-w-[80%] rounded-xl px-3 py-2 text-xs leading-relaxed ${
                    message.role === "user"
                      ? "bg-indigo-500/25 text-slate-100"
                      : "border border-white/[0.09] bg-white/[0.03] text-slate-200"
                  }`}
                >
                  <div className={message.docMarkdown ? "" : "whitespace-pre-wrap"}>
                    {message.content}
                  </div>
                  {message.docMarkdown && (
                    <details
                      className="mt-2 rounded-lg border border-indigo-400/25 bg-indigo-400/[0.06]"
                      data-testid="chat-doc-block"
                      open
                    >
                      <summary className="cursor-pointer px-3 py-2 text-[11px] font-medium text-indigo-200">
                        规格文档 · {message.docPage ?? "命中页面"}
                      </summary>
                      <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap px-3 pb-2 text-[11px] leading-relaxed text-slate-300">
                        {message.docMarkdown}
                      </pre>
                      <div className="px-3 pb-3">
                        <button
                          type="button"
                          onClick={() => downloadDoc(message.docMarkdown!, message.docPage)}
                          className="app-btn-ghost"
                          data-testid="chat-doc-download"
                        >
                          下载 .md
                        </button>
                      </div>
                    </details>
                  )}
                </div>
              </li>
            ))}
            {chat.isPending && (
              <li className="flex items-center gap-2 text-xs text-slate-400" data-testid="chat-pending">
                <Loader2 size={12} className="animate-spin" />
                正在思考…
              </li>
            )}
          </ul>
        </div>

        <div className="shrink-0 border-t border-white/[0.09] bg-[#151b25]/70 px-4 py-3">
          <div className="mx-auto max-w-3xl space-y-2">
            {draft && (
              <div
                className="rounded-lg border border-emerald-400/25 bg-emerald-400/[0.08] px-3 py-2"
                data-testid="chat-draft-banner"
              >
                <p className="text-[11px] font-medium text-emerald-200">已生成需求草稿</p>
                <p className="mt-1 whitespace-pre-wrap text-[11px] leading-relaxed text-emerald-100/90">
                  {draft}
                </p>
                <button
                  type="button"
                  onClick={fillDraft}
                  className="app-btn-primary mt-2"
                  data-testid="chat-fill-draft"
                >
                  填入需求定位输入框
                </button>
              </div>
            )}

            {lastError && (
              <div
                className="flex items-center gap-2 rounded-lg border border-rose-400/25 bg-rose-400/[0.08] px-3 py-2 text-[11px] text-rose-200"
                data-testid="chat-error"
              >
                <span className="min-w-0 flex-1">{lastError}</span>
                <button
                  type="button"
                  onClick={() => chat.mutate(messages)}
                  className="app-btn-ghost shrink-0"
                  data-testid="chat-retry"
                >
                  <RotateCcw size={12} />
                  重试
                </button>
              </div>
            )}

            <form onSubmit={send} className="flex items-end gap-2">
              <textarea
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    send(event);
                  }
                }}
                placeholder="输入你的想法，Enter 发送，Shift+Enter 换行"
                rows={2}
                disabled={chat.isPending}
                data-testid="chat-input"
                className="app-input min-w-0 flex-1 resize-none text-xs"
              />
              <button
                type="submit"
                disabled={!canSend}
                className="app-btn-primary shrink-0"
                data-testid="chat-send"
              >
                <Send size={13} />
                发送
              </button>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
