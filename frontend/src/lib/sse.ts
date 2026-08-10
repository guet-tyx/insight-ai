// SSE 流式客户端：POST + ReadableStream 手工解析（浏览器 EventSource 不支持 POST）
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1";

export interface SSEFrame {
  type: "tool_start" | "tool_end" | "token" | "done" | "error";
  [key: string]: unknown;
}

export type SSEHandler = (frame: SSEFrame) => void;

/** 流式发送用户消息；onFrame 逐帧回调；返回 Promise（流结束/异常时 resolve/reject）。 */
export async function streamChat(
  sessionId: string,
  message: string,
  token: string,
  onFrame: SSEHandler,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `请求失败（${res.status}）`);
  }
  if (!res.body) throw new Error("浏览器不支持流式响应");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // 按空行切分 SSE 帧；支持一个网络块内多帧
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const dataLine = frame
        .split("\n")
        .find((l) => l.startsWith("data: "));
      if (dataLine) {
        try {
          onFrame(JSON.parse(dataLine.slice(6)) as SSEFrame);
        } catch {
          /* 忽略非 JSON 心跳/注释帧 */
        }
      }
    }
  }
  // 尾部残帧兜底
  if (buffer.trim()) {
    const dataLine = buffer
      .split("\n")
      .find((l) => l.startsWith("data: "));
    if (dataLine) {
      try {
        onFrame(JSON.parse(dataLine.slice(6)) as SSEFrame);
      } catch {
        /* ignore */
      }
    }
  }
}