"""Reciprocal Rank Fusion（RRF）融合引擎（纯函数，可单测）。

计划公式：RRF_Score(d) = Σ_{m∈M} 1/(k + r_m(d))
- M：检索路径集合（向量检索 + 图谱检索）
- r_m(d)：片段 d 在路径 m 中的排名（从 1 开始）
- k：平滑常数（默认 60，按计划）

设计：
- 输入两路（或多路）带排名的候选列表，元素为 (doc_id, text, score)
- 片段粒度融合：graph 路径段与 vector chunk 按 doc_id/文本标识归一为候选集
- 输出按 RRF 分数降序的 Top-N 融合结果（含各路径排名信息，供溯源）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RRF_K = 60  # RRF 平滑常数（计划默认）


@dataclass
class RankedSource:
    """统一候选片段（跨路径去重标识 = (doc_id, text)）。"""

    doc_id: str
    text: str
    rank_scores: dict[str, int] = field(default_factory=dict)  # path -> rank


def _merge_by_identity(candidates: list[tuple[str, str, float]], path: str) -> dict[tuple[str, str], RankedSource]:
    """把某路径的 (doc_id, text, score) 列表按 (doc_id, text) 归一为候选。"""
    merged: dict[tuple[str, str], RankedSource] = {}
    for rank, (doc_id, text, _score) in enumerate(candidates, start=1):
        key = (doc_id, text)
        if key not in merged:
            merged[key] = RankedSource(doc_id=doc_id, text=text)
        merged[key].rank_scores[path] = rank
    return merged


def rrf_fuse(
    vector_hits: list[tuple[str, str, float]],
    graph_hits: list[tuple[str, str, float]],
    k: int = RRF_K,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """融合向量 + 图谱两路候选，返回按 RRF 分数降序的 Top-N。

    每项结构：{doc_id, text, score(rrf), vector_rank, graph_rank, source_type}
    - 单路径为空时自动退化为另一路径（纯向量/纯图均可用）
    - rrf score = Σ 1/(k + rank)；无该路径排名时该项为 0
    """
    merged = _merge_by_identity(vector_hits, "vector")
    for key, src in _merge_by_identity(graph_hits, "graph").items():
        if key in merged:
            merged[key].rank_scores.update(src.rank_scores)
        else:
            merged[key] = src

    results = []
    for src in merged.values():
        rrf_score = sum(1.0 / (k + rank) for rank in src.rank_scores.values())
        results.append(
            {
                "doc_id": src.doc_id,
                "text": src.text,
                "score": rrf_score,
                "vector_rank": src.rank_scores.get("vector"),
                "graph_rank": src.rank_scores.get("graph"),
                "source_type": "graph" if src.rank_scores.get("graph") else "vector",
            }
        )
    # 按 RRF 分数降序；并列时图谱路径（显式事实关联）优先于向量并列项
    results.sort(key=lambda r: (r["score"], 1 if r["source_type"] == "graph" else 0), reverse=True)
    return results[:top_n]


def rrf_score(rank: int, k: int = RRF_K) -> float:
    """单路径单片段 RRF 分数（便于单测数学断言）。"""
    return 1.0 / (k + rank)