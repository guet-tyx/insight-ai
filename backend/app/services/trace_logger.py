"""TraceLogger —— 本地结构化 Trace（W10 双轨：LangSmith 可选 + 本地兜底）。

- 进程内环形缓冲（至多 200 条），记录阶段/工具/延迟/幻觉信号
- use_langsmith：配置了 LANGCHAIN_API_KEY 时自动启用真实 Trace（langchain
  tracing 由 env 控制 LANGCHAIN_TRACING_V2；本模块记录侧仍然工作）
- 供 API 诊断（/system/trace 摘要）与性能报告消费
"""
from __future__ import annotations

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
        self._stats: dict[str, list[float]] = {}   # kind -> 延迟序列
        self._counts: dict[str, int] = {}          # kind -> 计数

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
            self._buffer.append({
                "ts": time.time(),
                "kind": kind,
                **data,
            })
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
        self.record("tool", {"name": name, "status": "fail", "latency_ms": latency_ms,
                             "error": error[:200]})

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

        return {"count": n, "p50_ms": round(pct(50), 2),
                "p95_ms": round(pct(95), 2), "p99_ms": round(pct(99), 2)}

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