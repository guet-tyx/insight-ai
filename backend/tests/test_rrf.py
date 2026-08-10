"""RRF 融合引擎单元测试：数学正确性 / 退化路径 / 排序。"""
from __future__ import annotations

from app.services.rrf import rrf_fuse, rrf_score


def test_rrf_single_path_score() -> None:
    """单路径单片段：score = 1/(k+rank)，k 默认 60（计划公式）。"""
    assert rrf_score(1) == pytest.approx(1 / 61)
    assert rrf_score(2) == pytest.approx(1 / 62)
    assert rrf_score(5, k=60) == pytest.approx(1 / 65)


def test_rrf_boosted_by_dual_path() -> None:
    """同一片段同时被两路召回 → 分数叠加（1/61 + 1/61）。"""
    vector = [("d1", "文本A", 0.9), ("d2", "文本B", 0.8)]
    graph = [("d1", "文本A", 0.5)]
    fused = rrf_fuse(vector, graph, top_n=5)
    d1 = next(r for r in fused if r["doc_id"] == "d1")
    d2 = next(r for r in fused if r["doc_id"] == "d2")
    assert d1["score"] == pytest.approx(2 / 61)  # 双路叠加
    assert d2["score"] == pytest.approx(1 / 62)  # 仅向量路 rank=2
    assert d1["score"] > d2["score"]  # 双路命中者优先
    assert d1["source_type"] == "graph"  # 图路径排名存在即标记 graph


def test_rrf_graph_only_degradation() -> None:
    """图路径非空、向量为空 → 纯图结果。"""
    fused = rrf_fuse([], [("g1", "路径A", 0.3)], top_n=5)
    assert len(fused) == 1
    assert fused[0]["doc_id"] == "g1"
    assert fused[0]["source_type"] == "graph"


def test_rrf_vector_only_degradation() -> None:
    """向量非空、图路径为空 → 纯向量结果（W2 行为保持）。"""
    fused = rrf_fuse([("v1", "片段1", 0.7)], [], top_n=5)
    assert len(fused) == 1
    assert fused[0]["source_type"] == "vector"


def test_rrf_top_n_cutoff() -> None:
    """Top-N 截断。"""
    vector = [(f"d{i}", f"文本{i}", 1.0 - i * 0.01) for i in range(10)]
    fused = rrf_fuse(vector, [], top_n=3)
    assert len(fused) == 3
    # 排名越前 RRF 分数越高
    scores = [r["score"] for r in fused]
    assert scores == sorted(scores, reverse=True)
    assert fused[0]["doc_id"] == "d0"


def test_rrf_empty_inputs() -> None:
    assert rrf_fuse([], []) == []


import pytest  # noqa: E402