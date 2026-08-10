"""Research 专家子图：知识库检索（W2 RAG）+ 实体/语义产出（W7 图谱补全）。

子图输入：task_requirement
子图输出：extracted_entities / semantic_chunks（映射进 GlobalState）
W5 检索语义：知识库 Top-K 片段 + 轻量实体抽取；W7 升级为 Neo4j 图谱 + RRF。
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.state import ResearchState
from app.core.config import settings
from app.services.retrieval_service import search as knowledge_search_svc

logger = logging.getLogger(__name__)

TOP_K = 5


def research_node(state: ResearchState) -> dict[str, Any]:
    """检索知识库并产出语义片段 + 粗粒度实体占位（供 Analyst 引用）。"""
    task = (state.get("task_requirement") or "").strip()
    if not task:
        return {"semantic_chunks": [], "extracted_entities": []}
    if not settings.siliconflow_api_key:
        return {"semantic_chunks": [], "extracted_entities": [{"error": "Embedding 未配置"}]}

    hits = knowledge_search_svc(task, top_k=TOP_K)
    chunks = [
        {
            "text": h.chunk_text,
            "score": h.score,
            "doc_id": h.doc_id,
            "page": h.page_number,
            "header": h.parent_header,
        }
        for h in hits
    ]
    # W7 升级点：这里的片段将送 LLM 实体/关系抽取 → Neo4j 写入与 Cypher 查询
    entities = [{"source": "knowledge", "kind": "chunk", "count": len(chunks)}]
    logger.info("Research 检索命中 %d 条（task=%s）", len(chunks), task[:40])
    return {"semantic_chunks": chunks, "extracted_entities": entities}


def build_research_subgraph():
    """构建 Research 子图（私有状态 ResearchState，单节点）。"""
    workflow = StateGraph(ResearchState)
    workflow.add_node("retrieve", research_node)
    workflow.add_edge(START, "retrieve")
    workflow.add_edge("retrieve", END)
    return workflow.compile()