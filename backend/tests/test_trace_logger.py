"""W10 TraceLogger 单元测试：环形缓冲 / 分位数 / 失败率 / 摘要。"""
from __future__ import annotations

import pytest

from app.services.trace_logger import TraceLogger, trace


def test_record_and_counts() -> None:
    t = TraceLogger()
    t.record("stage", {"stage": "supervisor"})
    t.record("stage", {"stage": "analyst"})
    s = t.summary()
    assert s["total_events"] == 2
    assert s["events_by_kind"]["stage"] == 2
    assert len(s["recent"]) == 2


def test_buffer_capacity_ring() -> None:
    """环形缓冲：超过容量后丢弃最旧事件。"""
    t = TraceLogger(capacity=3)
    for i in range(5):
        t.record("stage", {"i": i})
    s = t.summary()
    assert s["total_events"] == 5  # 计数保留
    assert len(s["recent"]) == 3  # 缓冲仅留最近 3 条
    assert s["recent"][0]["i"] == 2


def test_tool_ok_and_fail_ratio() -> None:
    """工具成功/失败计数 → tool_fail_ratio（修复后细分计数生效）。"""
    t = TraceLogger()
    t.tool_ok("knowledge_search", 120)
    t.tool_ok("fetch_rss", 40)
    t.tool_fail("collect_webpage", 3000, "超时")
    assert t.tool_fail_ratio() == pytest.approx(1 / 3, abs=1e-4)


def test_tool_fail_ratio_empty() -> None:
    assert TraceLogger().tool_fail_ratio() == 0.0


def test_latency_percentiles() -> None:
    """延迟分位数：P50/P95/P99 计算与四舍五入。"""
    t = TraceLogger()
    for ms in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
        t.tool_ok("t", ms)
    p = t.latency_percentiles("tool")
    assert p["count"] == 10
    assert p["p50_ms"] == 50   # nearest-rank：round(0.5*9)=4 → values[4]
    assert p["p95_ms"] == 100  # round(0.95*9)=9 → values[9]
    assert p["p99_ms"] == 100  # round(0.99*9)=9 → values[9]


def test_latency_percentiles_empty() -> None:
    assert TraceLogger().latency_percentiles("tool") == {}


def test_summary_fields_and_guardrail_signals() -> None:
    t = TraceLogger()
    t.tool_ok("knowledge_search", 50)
    t.record("guardrail", {"kind": "citation_oob"})
    s = t.summary()
    assert s["langsmith_enabled"] in (True, False)
    assert s["tool_fail_ratio"] == 0.0
    assert s["guardrail_signals"] == {"guardrail": 1}
    assert "tool_latency" in s


def test_module_singleton_exists() -> None:
    """模块级单例可直接消费（analyst/agent_service 埋点使用）。"""
    assert hasattr(trace, "record")
    assert hasattr(trace, "tool_ok")
    assert hasattr(trace, "tool_fail")
