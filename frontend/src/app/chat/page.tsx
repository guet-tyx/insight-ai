"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { createSession, getToken, logout } from "@/lib/api";
import { streamChat } from "@/lib/sse";

interface Msg {
  role: "user" | "assistant";
  content: string;
}

interface ToolCard {
  key: number;
  name: string;
  args: string;
  running: boolean;
  preview: string;
}

const STALL_MS = 60_000; // 无事件超过该时长判定停滞（对应计划风险表"SSE 中断防护"）

export default function ChatPage() {
  const router = useRouter();
  const [tokenReady, setTokenReady] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [toolCards, setToolCards] = useState<ToolCard[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [error, setError] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const stallTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // 登录守卫 + 首次创建会话
  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    setTokenReady(true);
    createSession()
      .then((s) => setSessionId(s.session_id))
      .catch((err) => setError(err.message));
  }, [router]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, toolCards]);

  const resetStallTimer = useCallback(() => {
    if (stallTimerRef.current) clearTimeout(stallTimerRef.current);
    stallTimerRef.current = setTimeout(() => {
      abortRef.current?.abort();
      setStreaming(false);
      setThinking(false);
      setError("连接似乎停滞了（超过 60 秒无响应），请重试");
    }, STALL_MS);
  }, []);

  const clearStallTimer = useCallback(() => {
    if (stallTimerRef.current) clearTimeout(stallTimerRef.current);
  }, []);

  async function send(text?: string) {
    const content = (text ?? input).trim();
    if (!content || streaming || !sessionId) return;
    const token = getToken();
    if (!token) return;

    setInput("");
    setError("");
    setStreaming(true);
    setThinking(true);
    setMessages((prev) => [...prev, { role: "user", content }]);
    setToolCards((prev) => [...prev]); // 保持卡片数组引用更新

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      resetStallTimer();
      await streamChat(
        sessionId,
        content,
        token,
        (frame) => {
          resetStallTimer();
          switch (frame.type) {
            case "tool_start":
              setThinking(false);
              setToolCards((prev) => [
                ...prev,
                {
                  key: Date.now() + Math.random(),
                  name: String(frame.name ?? ""),
                  args: JSON.stringify(frame.args ?? {}),
                  running: true,
                  preview: "",
                },
              ]);
              break;
            case "tool_end":
              setToolCards((prev) => {
                const next = [...prev];
                for (let i = next.length - 1; i >= 0; i--) {
                  if (next[i].running) {
                    next[i] = { ...next[i], running: false, preview: String(frame.preview ?? "") };
                    break;
                  }
                }
                return next;
              });
              break;
            case "token": {
              setThinking(false);
              const text2 = String(frame.content ?? "");
              // 纯 updater：助手消息恒为上一条追加（在流中不插入其他内容）
              setMessages((prev) => {
                const next = [...prev];
                const last = next[next.length - 1];
                if (last?.role === "assistant") {
                  next[next.length - 1] = { ...last, content: last.content + text2 };
                } else {
                  next.push({ role: "assistant", content: text2 });
                }
                return next;
              });
              break;
            }
            case "done":
              setThinking(false);
              if (frame.answer) {
                const answer = String(frame.answer);
                setMessages((prev) => {
                  const next = [...prev];
                  const last = next[next.length - 1];
                  if (last?.role === "assistant") {
                    next[next.length - 1] = { ...last, content: answer };
                  } else {
                    next.push({ role: "assistant", content: answer });
                  }
                  return next;
                });
              }
              break;
            case "error":
              setError(String(frame.message ?? "Agent 执行出错"));
              break;
          }
        },
        controller.signal,
      );
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setError(err instanceof Error ? err.message : "请求失败");
      }
    } finally {
      clearStallTimer();
      setStreaming(false);
      setThinking(false);
    }
  }

  return (
    <main className="flex h-screen flex-col bg-neutral-950 text-neutral-100">
      {/* 顶栏 */}
      <header className="flex items-center justify-between border-b border-neutral-800 px-6 py-3">
        <div className="flex items-center gap-3">
          <span className="h-2.5 w-2.5 rounded-full bg-sky-500" />
          <h1 className="font-semibold">Insight AI</h1>
          <span className="rounded-md bg-neutral-800 px-2 py-0.5 text-xs text-neutral-400">
            阶段一 MVP · 工具型助手
          </span>
        </div>
        <button onClick={logout} className="text-sm text-neutral-400 hover:text-neutral-200">
          退出登录
        </button>
      </header>

      {/* 消息区 */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-3xl space-y-4">
          {messages.length === 0 && (
            <div className="rounded-xl border border-dashed border-neutral-800 p-8 text-center text-neutral-500">
              <p className="mb-1 text-lg">开始你的情报分析</p>
              <p className="text-sm">例如：「知识库中介绍了哪些检索技术？」或「帮我采集 example.com 的标题」</p>
            </div>
          )}

          {/* 用户/助手消息 */}
          {messages.map((msg, i) => (
            <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
                  msg.role === "user"
                    ? "bg-sky-600 text-white"
                    : "border border-neutral-800 bg-neutral-900 text-neutral-100"
                }`}
              >
                {msg.content || (msg.role === "assistant" && thinking ? "思考中…" : "")}
              </div>
            </div>
          ))}

          {/* 工具卡片 */}
          {toolCards.map((card) => (
            <div key={card.key} className="rounded-lg border border-amber-700/50 bg-amber-950/30 px-4 py-2.5 text-sm">
              <div className="flex items-center gap-2">
                <span className={card.running ? "animate-pulse text-amber-400" : "text-amber-500"}>
                  {card.running ? "⏳" : "✓"}
                </span>
                <span className="font-mono text-xs text-amber-300">{card.name}</span>
                <span className="truncate text-xs text-amber-200/70">{card.args}</span>
              </div>
              {card.preview && (
                <p className="mt-1 truncate text-xs text-neutral-400">{card.preview}</p>
              )}
            </div>
          ))}

          {thinking && messages.length > 0 && messages[messages.length - 1]?.role === "assistant" && (
            <div className="flex justify-start">
              <div className="rounded-2xl border border-neutral-800 bg-neutral-900 px-4 py-2.5">
                <span className="inline-block h-2 w-2 animate-bounce rounded-full bg-neutral-500" />
                <span className="mx-0.5 inline-block h-2 w-2 animate-bounce rounded-full bg-neutral-500 [animation-delay:120ms]" />
                <span className="inline-block h-2 w-2 animate-bounce rounded-full bg-neutral-500 [animation-delay:240ms]" />
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* 错误横幅 */}
      {error && (
        <div className="mx-auto mb-2 flex w-full max-w-3xl items-center justify-between rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
          <span>{error}</span>
          <button
            onClick={() => {
              setError("");
              const last = messages[messages.length - 1];
              if (last?.role === "user") send(last.content);
            }}
            className="rounded bg-red-900 px-3 py-1 text-xs hover:bg-red-800"
          >
            重试
          </button>
        </div>
      )}

      {/* 输入区 */}
      <div className="border-t border-neutral-800 px-6 py-4">
        <form
          className="mx-auto flex max-w-3xl gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={streaming || !tokenReady}
            placeholder={streaming ? "Agent 正在执行…" : "输入问题，回车发送"}
            className="flex-1 rounded-xl border border-neutral-700 bg-neutral-900 px-4 py-3 text-sm outline-none focus:border-sky-500 disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={streaming || !input.trim() || !tokenReady}
            className="rounded-xl bg-sky-600 px-6 py-3 text-sm font-medium hover:bg-sky-500 disabled:opacity-40"
          >
            {streaming ? "执行中" : "发送"}
          </button>
        </form>
      </div>
    </main>
  );
}