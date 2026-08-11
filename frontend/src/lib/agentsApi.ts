// /agents 任务 API 封装（JWT）
import { getToken } from "./api";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1";

export interface RunStage {
  type: string;
  stage?: string;
  next?: string;
  detail?: string;
}

export interface RunInfo {
  run_id: string;
  status: "running" | "awaiting_review" | "ready" | "failed";
  stages: RunStage[];
  draft_report: string | null;
  final_report: string | null;
  error: string | null;
}

export interface SSEFrame {
  type: "stage" | "review_required" | "done" | "error";
  stage?: string;
  detail?: string;
  draft?: string;
  answer?: string;
  message?: string;
}

export async function createRun(instruction: string, token: string): Promise<{ run_id: string }> {
  const res = await fetch(`${BASE}/agents/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ instruction }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `启动任务失败（${res.status}）`);
  }
  return res.json();
}

export async function reviewRun(
  runId: string,
  action: "approve" | "reject" | "revise",
  comment: string,
  token: string,
): Promise<void> {
  const res = await fetch(`${BASE}/agents/runs/${runId}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ action, comment }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `提交审核失败（${res.status}）`);
  }
}

/** 从任务创建起持续监听 SSE（阶段事件 → 审核卡 → 终态报告）。 */
export async function streamRun(
  runId: string,
  token: string,
  onFrame: (frame: SSEFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/agents/runs/${runId}/stream`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `监听任务失败（${res.status}）`);
  }
  if (!res.body) throw new Error("浏览器不支持流式响应");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const dataLine = frame.split("\n").find((l) => l.startsWith("data: "));
      if (dataLine) {
        try {
          onFrame(JSON.parse(dataLine.slice(6)) as SSEFrame);
        } catch {
          /* 忽略心跳等非 JSON 帧 */
        }
      }
    }
  }
}

export { getToken };