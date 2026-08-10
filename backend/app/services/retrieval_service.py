"""知识库检索与问答：混合检索（Milvus 向量 + Neo4j 图谱 → RRF 融合）+ LLM 引证式回答。

W7 升级：search() 内部从「纯向量」升级为「hybrid 双路 + RRF」；
图查询失败/空库自动退化为纯向量（兼容 W2 行为）。
"""
from __future__ import annotations

import logging

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pymilvus import MilvusClient

from app.core.config import settings
from app.schemas.document import QueryResponse, SourceHit
from app.services.ingest_service import COLLECTION, ensure_collection, get_milvus_client
from app.services.rrf import rrf_fuse

logger = logging.getLogger(__name__)

# 输出字段：召回片段 + 溯源元数据
_OUTPUT_FIELDS = ["text", "doc_id", "page_number", "parent_header"]

# 双路召回规模（计划：向量 Top-K=20，图 1-2 跳）
VECTOR_RECALL_K = 20
FINAL_TOP_N = 5

SYSTEM_PROMPT = """你是 Insight AI 情报分析助手的知识库问答环节。
仅依据【参考片段】中的内容回答用户问题，不要编造片段之外的 Fact。
回答中如引用了某个片段，请在句末标注对应编号，例如 [1][2]。
若参考片段不足以回答问题，请明确说明"知识库中未找到相关信息"。
使用与用户问题相同的语言回答。"""


def _embed_query(query: str) -> list[float]:
    emb = OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.siliconflow_base_url,
        api_key=settings.siliconflow_api_key,
        check_embedding_ctx_length=False,  # 必须显式关闭，见 ingest_service 注释
    )
    return emb.embed_query(query)


def _vector_search(query: str, top_k: int = VECTOR_RECALL_K) -> list[tuple[str, str, float, int, str]]:
    """Milvus 向量召回 → (doc_id, text, distance, page, header) 列表（供 RRF）。"""
    ensure_collection()
    mc: MilvusClient = get_milvus_client()
    qvec = _embed_query(query)
    hits = mc.search(COLLECTION, data=[qvec], limit=top_k, output_fields=_OUTPUT_FIELDS)
    return [
        (
            hit["entity"]["doc_id"],
            hit["entity"]["text"],
            float(hit["distance"]),
            int(hit["entity"].get("page_number", 0)),
            hit["entity"].get("parent_header", ""),
        )
        for hit in hits[0]
    ]


def _graph_search(query: str) -> list[tuple[str, str, float]]:
    """Neo4j 图谱拓扑召回 → (doc_id, text, score) 列表（供 RRF）；失败返回 []。"""
    try:
        from app.services.graph_service import graph_search

        paths = graph_search(query, max_hops=2)
    except Exception as exc:  # noqa: BLE001 — 图查询失败退化为纯向量
        logger.warning("图谱查询失败，退化纯向量：%s", exc)
        return []
    return [(p["doc_id"], p["text"], p["score"]) for p in paths]


def search(query: str, top_k: int = 5) -> list[SourceHit]:
    """混合检索：向量 Top-20 + 图谱路径 → RRF(k=60) → Top-N。

    返回 SourceHit 列表（新增 source_type 字段：vector/graph；兼容旧调用方）。
    """
    vector_hits = _vector_search(query)
    graph_hits = _graph_search(query)
    # RRF 仅消费 (doc_id, text, score)；向量页/标题元数据在融合后按 doc_id+text 回填
    fused = rrf_fuse(
        [(h[0], h[1], h[2]) for h in vector_hits],
        graph_hits,
        top_n=max(top_k, FINAL_TOP_N),
    )

    results = []
    for item in fused:
        # 图谱路径段无 page/header 信息 → 用图路径文本替代 chunk_text
        if item["source_type"] == "graph":
            results.append(
                SourceHit(
                    chunk_text=item["text"],
                    score=item["score"],
                    doc_id=item["doc_id"] or "graph",
                    page_number=0,
                    parent_header="图谱路径",
                    source_type="graph",
                )
            )
        else:
            # 从向量原始命中找回 page/header 元数据
            page = 0
            header = ""
            for h in vector_hits:
                if h[0] == item["doc_id"] and h[1] == item["text"]:
                    page = h[3]
                    header = h[4]
                    break
            results.append(
                SourceHit(
                    chunk_text=item["text"],
                    score=item["score"],
                    doc_id=item["doc_id"],
                    page_number=page,
                    parent_header=header,
                    source_type="vector",
                )
            )
    return results


def answer(query: str, top_k: int = 5) -> QueryResponse:
    """检索 + LLM 引证式回答（完整 RAG 闭环，消费 hybrid 结果）。"""
    sources = search(query, top_k=top_k)
    if not sources:
        return QueryResponse(answer="知识库中未检索到相关内容。", sources=[])

    context = "\n".join(
        f"[{i + 1}] ({s.source_type} · {s.parent_header or s.doc_id[:8]}"
        f"{('，第 ' + str(s.page_number) + ' 页') if s.page_number else ''})：{s.chunk_text}"
        for i, s in enumerate(sources)
    )
    llm = ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.llm_base_url,
        temperature=0.3,
    )
    resp = llm.invoke(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"【参考片段】\n{context}\n\n【问题】{query}"},
        ]
    )
    return QueryResponse(answer=resp.content, sources=sources)