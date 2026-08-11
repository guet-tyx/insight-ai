"""W10 Guardrails 单元测试：引用校验 / 路由枚举校验 / 幻觉信号埋点。"""
from __future__ import annotations

from app.services.guardrails import (
    extract_citations,
    route_validator,
    track_hallucination_signal,
    validate_report_citations,
)


# ---------- 引用编号提取 ----------

def test_extract_citations_basic() -> None:
    assert extract_citations("平台采用 Milvus 检索 [1]，配合图查询 [2][3]。") == [1, 2, 3]


def test_extract_citations_three_digit_and_none() -> None:
    assert extract_citations("引用 [100] 与 [007]") == [100, 7]
    assert extract_citations("无任何引用") == []


# ---------- 引用校验（幻觉信号） ----------

def test_validate_in_range_ok() -> None:
    res = validate_report_citations("结论见 [1] 与 [2]。", material_count=5)
    assert res["valid"] is True
    assert res["out_of_bounds"] == []
    assert res["warnings"] == []


def test_validate_out_of_bounds_detected() -> None:
    """引用编号超出素材范围 → 超标引用（幻觉风险信号）。"""
    res = validate_report_citations("结论见 [3][9]。", material_count=2)
    assert res["valid"] is False
    assert res["out_of_bounds"] == [3, 9]
    assert any("超出素材范围" in w for w in res["warnings"])


def test_validate_no_citations_with_material() -> None:
    """有素材但零引用 → 提示性告警（不判 invalid）。"""
    res = validate_report_citations("这是一段没有任何编号引用的分析。", material_count=3)
    assert res["valid"] is False
    assert res["citations"] == []
    assert any("零引用" in w for w in res["warnings"])


def test_validate_zero_material_no_warning() -> None:
    """素材为 0 时不应产生「零引用」告警。"""
    res = validate_report_citations("无素材报告 [1]。", material_count=0)
    assert res["valid"] is False  # 引用 [1] 超出 0 条素材
    assert res["out_of_bounds"] == [1]
    assert not any("零引用" in w for w in res["warnings"])


def test_validate_dedup_oob_sorted() -> None:
    """重复的超标编号去重且升序。"""
    res = validate_report_citations("[7] 与 [7] 和 [3]", material_count=2)
    assert res["out_of_bounds"] == [3, 7]


# ---------- 路由枚举校验 ----------

def test_route_validator_allowed(caplog) -> None:
    res = route_validator("analyst", {"collector", "analyst", "finish"})
    assert res == {"valid": True, "next_node": "analyst", "allowed": ["analyst", "collector", "finish"]}


def test_route_validator_out_of_enum_warns(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        res = route_validator("hack", {"collector", "finish"})
    assert res["valid"] is False
    assert "hack" in caplog.text


# ---------- 幻觉信号埋点（写入 TraceLogger） ----------

def test_track_hallucination_signal_records(monkeypatch) -> None:
    """track_hallucination_signal → TraceLogger.record(guardrail, ...)。"""
    from app.services import trace_logger as tl

    captured: list[dict] = []

    class _FakeTrace:
        def record(self, kind: str, data: dict) -> None:
            captured.append({"kind": kind, **data})

    monkeypatch.setattr(tl, "trace", _FakeTrace())
    track_hallucination_signal("citation_oob", {"detail": {"oob": [9]}})
    # track_hallucination_signal 调用 trace.record("guardrail", {"kind": kind, **detail})
    assert captured == [{"kind": "guardrail", "kind": "citation_oob", "detail": {"oob": [9]}}]
