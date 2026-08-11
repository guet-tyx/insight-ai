"""Guardrails 输出校验（W10：幻觉防护）。

- validate_report_citations: 报告 [N] 引用编号校验（超标引用 = 幻觉风险信号）
- route_validator: 路由决策枚举校验（日志级告警，不阻断）
   纯函数，可单测。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_CITATION_RE = re.compile(r"\[(\d{1,3})\]")


def extract_citations(report: str) -> list[int]:
    """提取报告中的引用编号列表（去重保序）。"""
    return [int(m) for m in _CITATION_RE.findall(report)]


def validate_report_citations(report: str, material_count: int) -> dict:
    """校验报告引用编号是否超出素材范围。

    返回 {valid, citations, out_of_bounds, warnings: [...]}；
    超标引用即「引用不存在素材」的幻觉风险信号，交由 Trace 记录。
    """
    citations = extract_citations(report)
    oob = sorted({c for c in citations if c > material_count})
    warnings = []
    if oob:
        warnings.append(f"引用编号超出素材范围（素材 {material_count} 条）：{oob}")
    if material_count > 0 and not citations:
        warnings.append("报告未使用任何引用（有素材但零引用）")
    return {
        "valid": not warnings,
        "citations": citations,
        "out_of_bounds": oob,
        "warnings": warnings,
    }


def route_validator(next_node: str, allowed: set[str]) -> dict:
    """路由决策枚举校验（日志级告警，不阻断流转）。"""
    ok = next_node in allowed
    if not ok:
        logger.warning("Guardrails: 路由决策超出枚举 %r → %r", allowed, next_node)
    return {"valid": ok, "next_node": next_node, "allowed": sorted(allowed)}


def track_hallucination_signal(kind: str, detail: dict) -> None:
    """幻觉信号埋点（W10：写入 TraceLogger）。"""
    from app.services.trace_logger import trace

    trace.record("guardrail", {"kind": kind, **detail})
