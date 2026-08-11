"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";

interface Props {
  draft: string;
  busy: boolean;
  onSubmit: (action: "approve" | "reject" | "revise", comment: string) => void;
}

/** HITL 报告审核卡：Markdown 渲染草稿 + 批准/拒绝/修改意见。 */
export default function ReviewCard({ draft, busy, onSubmit }: Props) {
  const [comment, setComment] = useState("");
  const [showComment, setShowComment] = useState(false);

  function submit(action: "approve" | "reject" | "revise") {
    if (action === "revise" && !comment.trim()) return;
    onSubmit(action, comment.trim());
  }

  return (
    <div className="rounded-xl border border-violet-500/40 bg-violet-950/20">
      <div className="flex items-center justify-between border-b border-violet-500/30 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 animate-pulse rounded-full bg-violet-400" />
          <span className="text-sm font-medium text-violet-200">报告待人工审核（HITL）</span>
        </div>
        <span className="text-xs text-violet-300/70">Supervisor 已暂停 · 等待你的决策</span>
      </div>

      {/* 报告草稿（Markdown 渲染） */}
      <div className="prose prose-invert prose-sm max-w-none px-4 py-3 text-neutral-200">
        <ReactMarkdown>{draft}</ReactMarkdown>
      </div>

      {/* 审核操作区 */}
      <div className="border-t border-violet-500/30 px-4 py-3">
        {showComment && (
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="输入修改意见（如：请在开头补充研究背景，结论部分增加风险提示…）"
            className="mb-3 w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-violet-500"
            rows={3}
          />
        )}
        <div className="flex gap-3">
          <button
            onClick={() => submit("approve")}
            disabled={busy}
            className="rounded-lg bg-emerald-600 px-5 py-2 text-sm font-medium hover:bg-emerald-500 disabled:opacity-50"
          >
            批准通过
          </button>
          <button
            onClick={() => {
              setShowComment(true);
              if (showComment && comment.trim()) submit("revise");
            }}
            disabled={busy || (showComment && !comment.trim())}
            className="rounded-lg bg-amber-600 px-5 py-2 text-sm font-medium hover:bg-amber-500 disabled:opacity-50"
          >
            {showComment ? "提交修改意见" : "修改意见"}
          </button>
          <button
            onClick={() => submit("reject")}
            disabled={busy}
            className="rounded-lg border border-red-700 px-5 py-2 text-sm text-red-300 hover:bg-red-950 disabled:opacity-50"
          >
            拒绝
          </button>
          {busy && <span className="self-center text-xs text-neutral-400">提交中…</span>}
        </div>
      </div>
    </div>
  );
}