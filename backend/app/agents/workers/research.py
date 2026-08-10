"""Research 专家子图：混合检索（W7：向量 + 图谱 → RRF）+ 实体产出。

子图输入：task_requirement
子图输出：extracted_entities / semantic_chunks（映射进 GlobalState）
W7 升级：检索走 hybrid（Milvus + Neo4j 1-2 跳路径，RRF 融合）；
extracted_entities 产出实体清单（供 Analyst 引用图谱路径）。
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.state import ResearchState
from app.core.config import settings
from app.services.retrieval_service import search as hybrid_search_svc

logger = logging.getLogger(__name__)

TOP_K = 5


def research_node(state: ResearchState) -> dict[str, Any]:
    """混合检索（向量+图谱）并产出语义片段与实体清单。"""
    task = (state.get("task_requirement") or "").strip()
    if not task:
        return {"semantic_chunks": [], "extracted_entities": []}
    if not settings.siliconflow_api_key:
        return {"semantic_chunks": [], "extracted_entities": [{"error": "Embedding 未配置"}]}

    hits = hybrid_search_svc(task, top_k=TOP_K)
    chunks = [
        {
            "text": h.chunk_text,
            "score": h.score,
            "doc_id": h.doc_id,
            "page": h.page_number,
            "header": h.parent_header,
            "source_type": h.source_type,  # vector / graph
        }
        for h in hits
    ]
    entities = [
        {"source": h.source_type, "kind": "chunk", "count": len(chunks)}
    ]
    graph_paths = [h for h in hits if h.source_type == "graph"]
    if graph_paths:
        entities = [
            *entities,
            {"source": "graph", "kind": "path", "count": len(graph_paths)},
        ]
    logger.info("Research 混合检索命中 %d 条（图路径 %d）", len(chunks), len(graph_paths))
    return {"semantic_chunks": chunks, "extracted_entities": entities}


def build_research_subgraph():
    """构建 Research 子图（私有状态 ResearchState，单节点）。"""
    workflow = StateGraph(ResearchState)
    workflow.add_node("retrieve", research_node)
    workflow.add_edge(START, "retrieve")
    workflow.add_edge("retrieve", END)
    return workflow.compile()