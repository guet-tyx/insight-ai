"""知识库检索与问答：向量 Top-K 召回 + LLM 引证式回答（SenseNova 网关）。"""
from __future__ import annotations

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pymilvus import MilvusClient

from app.core.config import settings
from app.schemas.document import QueryResponse, SourceHit
from app.services.ingest_service import COLLECTION, ensure_collection, get_milvus_client

# 输出字段：召回片段 + 溯源元数据
_OUTPUT_FIELDS = ["text", "doc_id", "page_number", "parent_header"]

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


def search(query: str, top_k: int = 5) -> list[SourceHit]:
    """向量检索 Top-K 片段（余弦相似度，Milvus）。"""
    ensure_collection()
    mc: MilvusClient = get_milvus_client()
    qvec = _embed_query(query)
    hits = mc.search(
        COLLECTION,
        data=[qvec],
        limit=top_k,
        output_fields=_OUTPUT_FIELDS,
    )
    return [
        SourceHit(
            chunk_text=hit["entity"]["text"],
            score=float(hit["distance"]),
            doc_id=hit["entity"]["doc_id"],
            page_number=int(hit["entity"]["page_number"]),
            parent_header=hit["entity"].get("parent_header", ""),
        )
        for hit in hits[0]
    ]


def answer(query: str, top_k: int = 5) -> QueryResponse:
    """检索 + LLM 引证式回答（完整 RAG 闭环）。"""
    sources = search(query, top_k=top_k)
    if not sources:
        return QueryResponse(answer="知识库中未检索到相关内容。", sources=[])

    context = "\n".join(
        f"[{i + 1}]《{src.parent_header}》(文档 {src.doc_id[:8]}，第 {src.page_number} 页)：{src.chunk_text}"
        for i, src in enumerate(sources)
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