"""TraceLogger —— 本地结构化 Trace（W10 双轨：LangSmith 可选 + 本地兜底）。

- 进程内环形缓冲（至多 200 条），记录阶段/工具/延迟/幻觉信号
- use_langsmith：配置了 LANGCHAIN_API_KEY 时自动启用真实 Trace（langchain
  tracing 由 env 控制 LANGCHAIN_TRACING_V2；本模块记录侧仍然工作）
- 供 API 诊断（/system/trace 摘要）与性能报告消费
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

MAX_BUFFER = 200


class TraceLogger:
    """环形缓冲 Trace 记录器。"""

    def __init__(self, capacity: int = MAX_BUFFER) -> None:
        self._buffer: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._stats: dict[str, list[float]] = {}  # kind -> 延迟序列
        self._counts: dict[str, int] = {}  # kind -> 计数

    @property
    def langsmith_enabled(self) -> bool:
        """双轨：有 key 且开启 tracing 时才走 LangSmith。"""
        return bool(
            settings.openai_api_key
            and (settings.langchain_api_key or "")
            and settings.langchain_tracing_v2
        )

    def record(self, kind: str, data: dict[str, Any] | None = None) -> None:
        """记录一条 Trace 事件。"""
        data = data or {}
        with self._lock:
            self._buffer.append(
                {
                    "ts": time.time(),
                    "kind": kind,
                    **data,
                }
            )
            self._counts[kind] = self._counts.get(kind, 0) + 1
            # 工具成功/失败细分计数（tool_fail_ratio 依赖）
            if kind == "tool" and data.get("status") in ("ok", "fail"):
                st_key = f"tool_{data['status']}"
                self._counts[st_key] = self._counts.get(st_key, 0) + 1
            latency = data.get("latency_ms")
            if latency is not None:
                self._stats.setdefault(kind, []).append(float(latency))

    def tool_ok(self, name: str, latency_ms: int) -> None:
        self.record("tool", {"name": name, "status": "ok", "latency_ms": latency_ms})

    def tool_fail(self, name: str, latency_ms: int, error: str) -> None:
        self.record(
            "tool", {"name": name, "status": "fail", "latency_ms": latency_ms, "error": error[:200]}
        )

    def latency_percentiles(self, kind: str = "tool") -> dict[str, float]:
        """指定类型延迟分位数（P50/P95/P99），无数据返回空表。"""
        with self._lock:
            values = sorted(self._stats.get(kind, []))
        if not values:
            return {}
        n = len(values)

        def pct(p: float) -> float:
            # nearest-rank：索引 = round(p/100 * (n-1))，P50 落在中位元素
            return values[min(round(p / 100 * (n - 1)), n - 1)]

        return {
            "count": n,
            "p50_ms": round(pct(50), 2),
            "p95_ms": round(pct(95), 2),
            "p99_ms": round(pct(99), 2),
        }

    def tool_fail_ratio(self) -> float:
        """工具调用失败率（幻觉/故障信号之一）。"""
        with self._lock:
            ok = self._counts.get("tool_ok", 0)
            fail = self._counts.get("tool_fail", 0)
        total = ok + fail
        return round(fail / total, 4) if total else 0.0

    def summary(self) -> dict[str, Any]:
        """诊断摘要（供 /system/trace）。"""
        with self._lock:
            kinds = dict(self._counts)
            guardrail_kinds = {k: v for k, v in self._counts.items() if k == "guardrail"}
            last = list(self._buffer)[-10:] if self._buffer else []
        return {
            "langsmith_enabled": self.langsmith_enabled,
            "total_events": sum(kinds.values()),
            "tool_fail_ratio": self.tool_fail_ratio(),
            "tool_latency": self.latency_percentiles("tool"),
            "events_by_kind": kinds,
            "guardrail_signals": guardrail_kinds,
            "recent": last,
        }


trace = TraceLogger()


def panel_html(summary: dict[str, Any]) -> str:
    """自包含 Trace 诊断面板（W11）：深色卡片式，JS 带 JWT 轮询 /system/trace。

    首帧数据由服务端渲染（Docker 部署后可直接访问）；登录态下页面自动刷新。
    """
    payload = json.dumps(summary, ensure_ascii=False, default=str)
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Insight AI · Trace 诊断面板</title>
<style>
  body {{ background:#0f172a; color:#e2e8f0; font-family:ui-monospace,Consolas,monospace;
        margin:0; padding:24px; }}
  h1 {{ font-size:18px; color:#38bdf8; margin:0 0 4px; }}
  .sub {{ color:#64748b; font-size:12px; margin-bottom:20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }}
  .card {{ background:#1e293b; border:1px solid #334155; border-radius:10px; padding:14px; }}
  .card .k {{ color:#94a3b8; font-size:11px; text-transform:uppercase; }}
  .card .v {{ font-size:22px; font-weight:700; margin-top:6px; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px;
           background:#0e7490; color:#cffafe; }}
  .badge.off {{ background:#334155; color:#94a3b8; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
  th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid #1e293b; }}
  th {{ color:#94a3b8; font-weight:500; }}
  .warn {{ color:#fbbf24; }} .err {{ color:#f87171; }} .ok {{ color:#34d399; }}
  .recent td {{ font-size:12px; }}
  .sec {{ margin-top:24px; }}
  .sec h2 {{ font-size:14px; color:#94a3b8; margin-bottom:8px; }}
</style></head><body>
<h1>Insight AI · Trace 诊断面板</h1>
<div class="sub">本地 Trace（W10 双轨：LangSmith 可选）· 进程内环形缓冲 · <span id="refresh">—</span></div>
<div class="grid">
  <div class="card"><div class="k">LangSmith</div><div class="v" id="ls">—</div></div>
  <div class="card"><div class="k">事件总数</div><div class="v" id="total">—</div></div>
  <div class="card"><div class="k">工具失败率</div><div class="v" id="ratio">—</div></div>
  <div class="card"><div class="k">P50 / P95 / P99</div><div class="v" id="lat" style="font-size:16px">—</div></div>
</div>
<div class="sec"><h2>事件分布（by kind）</h2><table id="kinds"></table></div>
<div class="sec"><h2>Guardrail 信号（幻觉防护）</h2><table id="guard"></table></div>
<div class="sec"><h2>最近事件</h2><table class="recent" id="recent"></table></div>
<script>
const TOKEN_KEY = "insight_token";
const INIT = {payload};
function esc(s) {{ return String(s ?? "").replace(/[&<>"']/g, c =>
  {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]); }}
function render(d) {{
  document.getElementById("ls").innerHTML =
    d.langsmith_enabled ? '<span class="badge">已启用</span>' : '<span class="badge off">本地兜底</span>';
  document.getElementById("total").textContent = d.total_events ?? 0;
  const ratio = d.tool_fail_ratio ?? 0;
  const rEl = document.getElementById("ratio");
  rEl.textContent = (ratio * 100).toFixed(1) + "%";
  rEl.className = "v " + (ratio > 0.2 ? "err" : ratio > 0 ? "warn" : "ok");
  const lat = d.tool_latency || {{}};
  document.getElementById("lat").textContent =
    lat.count ? lat.p50_ms + " / " + lat.p95_ms + " / " + lat.p99_ms + " ms" : "暂无数据";
  const kinds = Object.entries(d.events_by_kind || {{}});
  document.getElementById("kinds").innerHTML = kinds.length
    ? kinds.map(([k, v]) => `<tr><td>${{esc(k)}}</td><td>${{v}}</td></tr>`).join("")
    : '<tr><td colspan="2">暂无事件</td></tr>';
  const guard = Object.entries(d.guardrail_signals || {{}});
  document.getElementById("guard").innerHTML = guard.length
    ? guard.map(([k, v]) => `<tr><td>${{esc(k)}}</td><td class="warn">${{v}}</td></tr>`).join("")
    : '<tr><td colspan="2">无幻觉信号</td></tr>';
  const recent = (d.recent || []).slice().reverse();
  document.getElementById("recent").innerHTML = recent.length
    ? recent.map(e => `<tr><td>${{esc(e.kind)}}</td><td>${{esc(e.name || e.stage || "")}}</td>
        <td class="${{e.status === "fail" ? "err" : e.status === "ok" ? "ok" : ""}}">${{esc(e.status || "")}}</td>
        <td>${{e.latency_ms != null ? e.latency_ms + "ms" : ""}}</td></tr>`).join("")
    : '<tr><td colspan="4">暂无事件</td></tr>';
}}
render(INIT);
const token = localStorage.getItem(TOKEN_KEY);
if (token) {{
  let last = Date.now();
  setInterval(async () => {{
    try {{
      const r = await fetch("/api/v1/system/trace", {{ headers: {{ Authorization: "Bearer " + token }} }});
      if (r.ok) {{ render(await r.json()); last = Date.now(); }}
    }} catch (e) {{ /* 网络瞬断忽略 */ }}
    document.getElementById("refresh").textContent = "上次刷新 " + new Date(last).toLocaleTimeString();
  }}, 3000);
}} else {{
  document.getElementById("refresh").textContent = "未登录：显示静态快照（登录后自动刷新）";
}}
</script></body></html>"""
