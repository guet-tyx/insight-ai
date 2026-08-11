"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";

import { createRun, reviewRun, streamRun, type RunStage } from "@/lib/agentsApi";
import { getToken, logout } from "@/lib/api";
import ReviewCard from "@/components/ReviewCard";

export default function AgentsPage() {
  const router = useRouter();
  const [tokenReady, setTokenReady] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [stages, setStages] = useState<RunStage[]>([]);
  const [draft, setDraft] = useState<string | null>(null);
  const [finalReport, setFinalReport] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    setTokenReady(true);
  }, [router]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [stages, draft, finalReport]);

  const listen = useCallback((id: string, token: string) => {
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy(true);
    streamRun(id, token, (frame) => {
      switch (frame.type) {
        case "stage":
          setStages((prev) => [...prev, { type: "stage", stage: frame.stage, detail: frame.detail }]);
          break;
        case "review_required":
          setDraft(frame.draft ?? "");
          setBusy(false);
          break;
        case "done":
          setFinalReport(frame.answer ?? "");
          setDone(true);
          setBusy(false);
          break;
        case "error":
          setError(frame.message ?? "任务执行出错");
          setBusy(false);
          break;
      }
    }, controller.signal).catch((err) => {
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setError(err.message);
        setBusy(false);
      }
    });
  }, []);

  async function start() {
    const text = instruction.trim();
    const token = getToken();
    if (!text || !token || busy) return;
    setError("");
    setDone(false);
    setFinalReport(null);
    setDraft(null);
    setStages([]);
    setRunId(null);
    try {
      const { run_id } = await createRun(text, token);
      setRunId(run_id);
      listen(run_id, token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "启动失败");
    }
  }

  async function handleReview(action: "approve" | "reject" | "revise", comment: string) {
    const token = getToken();
    if (!token || !runId) return;
    setError("");
    setDraft(null); // 审核提交后隐藏卡片，等待下一阶段
    setBusy(true);
    try {
      await reviewRun(runId, action, comment, token);
      listen(runId, token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败");
      setBusy(false);
    }
  }

  return (
    <main className="flex h-screen flex-col bg-neutral-950 text-neutral-100">
      <header className="flex items-center justify-between border-b border-neutral-800 px-6 py-3">
        <div className="flex items-center gap-3">
          <span className="h-2.5 w-2.5 rounded-full bg-violet-500" />
          <h1 className="font-semibold">Insight AI · 多智能体任务</h1>
          <span className="rounded-md bg-neutral-800 px-2 py-0.5 text-xs text-neutral-400">
            阶段二 · Supervisor-Worker + HITL
          </span>
        </div>
        <div className="flex items-center gap-4">
          <Link href="/chat" className="text-sm text-neutral-400 hover:text-neutral-200">
            聊天
          </Link>
          <button onClick={logout} className="text-sm text-neutral-400 hover:text-neutral-200">
            退出登录
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-3xl space-y-4">
          {/* 启动区 */}
          {!runId && (
            <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-4">
              <p className="mb-2 text-sm text-neutral-400">
                发起复合情报分析任务：Supervisor 拆解 → Collector 采集 → Research 交叉校验 →
                Analyst 生成报告 → 人工审核（HITL）
              </p>
              <div className="flex gap-3">
                <input
                  value={instruction}
                  onChange={(e) => setInstruction(e.target.value)}
                  placeholder="例如：分析知识库中介绍的检索技术并生成总结报告"
                  className="flex-1 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-sm outline-none focus:border-violet-500"
                />
                <button
                  onClick={start}
                  disabled={!instruction.trim() || !tokenReady}
                  className="rounded-lg bg-violet-600 px-6 py-2.5 text-sm font-medium hover:bg-violet-500 disabled:opacity-40"
                >
                  发起任务
                </button>
              </div>
            </div>
          )}

          {/* 阶段事件流 */}
          {runId && (
            <div className="rounded-lg border border-neutral-800 px-4 py-2 font-mono text-xs text-neutral-400">
              run: {runId.slice(0, 8)}… · {busy && !draft ? "执行中" : draft ? "等待审核" : done ? "已完成" : ""}
            </div>
          )}
          {stages.map((s, i) => (
            <div key={i} className="rounded-lg border border-neutral-800 bg-neutral-900/60 px-4 py-2 text-sm">
              <span className="mr-2 font-mono text-xs text-violet-300">{s.stage}</span>
              <span className="text-neutral-300">{s.detail}</span>
            </div>
          ))}

          {/* HITL 审核卡 */}
          {draft !== null && (
            <ReviewCard draft={draft} busy={busy} onSubmit={handleReview} />
          )}

          {/* 终态报告 */}
          {finalReport && (
            <div className="rounded-xl border border-emerald-700/40 bg-neutral-900">
              <div className="flex items-center gap-2 border-b border-neutral-800 px-4 py-2.5">
                <span className="h-2 w-2 rounded-full bg-emerald-500" />
                <span className="text-sm font-medium text-emerald-300">最终分析报告（已批准）</span>
              </div>
              <div className="prose prose-invert prose-sm max-w-none px-4 py-3 text-neutral-200">
                <ReactMarkdown>{finalReport}</ReactMarkdown>
              </div>
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
              {error}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>
    </main>
  );
}