"""文档入库：语义分块 → 批量嵌入（硅基流动 bge-m3）→ Milvus 写入 → 状态流转。

依赖外部：
- Milvus（docker compose 启动，集合名 insight_knowledge）
- SiliconFlow API（EMBEDDING_MODEL / SILICONFLOW_API_KEY，见 .env）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from langchain_openai import OpenAIEmbeddings
from pymilvus import DataType, MilvusClient

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.document import Document
from app.services.pdf_parser import ParsedChunk, parse_pdf_bytes

logger = logging.getLogger(__name__)

COLLECTION = "insight_knowledge"
EMBED_BATCH_SIZE = 50  # 单批嵌入条数

# 供测试快速发现
__all__ = ["COLLECTION", "ensure_collection", "get_milvus_client", "ingest_document"]


def get_milvus_client() -> MilvusClient:
    return MilvusClient(uri=settings.milvus_uri)


def _embedder() -> OpenAIEmbeddings:
    """bge-m3 嵌入器。

    ⚠️ check_embedding_ctx_length=False 必须关闭：langchain-openai 默认会本地
    token 化并发送 token ID 数组，硅基流动等兼容服务无法正确解析（见环境报告§9）。
    """
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.siliconflow_base_url,
        api_key=settings.siliconflow_api_key,
        check_embedding_ctx_length=False,
    )


def ensure_collection() -> None:
    """幂等创建向量集合（HNSW + COSINE，M=16 / efConstruction=200）。

    pymilvus 3.x 若拒绝自定义 HNSW 参数则回落 API 默认索引，保证可用性。
    """
    mc = get_milvus_client()
    if mc.has_collection(COLLECTION):
        return
    schema = mc.create_schema(auto_id=True)  # 自增主键，doc_id 作为业务键
    schema.add_field("id", DataType.INT64, is_primary=True)  # 3.x 需显式声明主键字段
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=settings.embedding_dim)
    schema.add_field("text", DataType.VARCHAR, max_length=65535)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
    schema.add_field("page_number", DataType.INT64)
    schema.add_field("parent_header", DataType.VARCHAR, max_length=512)
    schema.add_field("chunk_index", DataType.INT64)
    try:
        index_params = mc.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        mc.create_collection(COLLECTION, schema=schema, index_params=index_params)
    except Exception:
        logger.warning("自定义 HNSW 索引参数创建失败，回落默认索引", exc_info=True)
        mc.create_collection(COLLECTION, schema=schema)
    logger.info("Milvus 集合 %s 就绪", COLLECTION)


def _embed_chunks(chunks: list[ParsedChunk]) -> list[list[float]]:
    """批量嵌入全部 chunk（无 key 时抛错，由调用方转为 failed 状态）。"""
    emb = _embedder()
    vectors: list[list[float]] = []
    for i in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = [c.text for c in chunks[i : i + EMBED_BATCH_SIZE]]
        vectors.extend(emb.embed_documents(batch, chunk_size=EMBED_BATCH_SIZE))
    return vectors


def ingest_document(doc_id: str, file_bytes: bytes, filename: str) -> None:
    """后台入库任务：解析 → 嵌入 → 写入 Milvus → 更新注册表状态。

    由 FastAPI BackgroundTasks 在响应返回后调用；独立 Session，
    任何异常都会把文档标记为 failed 并记录摘要。
    """
    now = datetime.now(timezone.utc).isoformat()
    db = SessionLocal()
    try:
        record = db.get(Document, doc_id)
        if record is None:
            logger.warning("入库任务找不到文档记录 %s，跳过", doc_id)
            return
        try:
            chunks = parse_pdf_bytes(file_bytes, doc_id)
            if not chunks:
                raise ValueError("PDF 未提取到文本内容")
            vectors = _embed_chunks(chunks)

            mc = get_milvus_client()
            rows = [
                {
                    "vector": vec,
                    "text": chunk.text,
                    "doc_id": doc_id,
                    "page_number": chunk.page_number,
                    "parent_header": chunk.parent_header,
                    "chunk_index": chunk.chunk_index,
                }
                for chunk, vec in zip(chunks, vectors)
            ]
            for i in range(0, len(rows), EMBED_BATCH_SIZE):
                mc.insert(COLLECTION, rows[i : i + EMBED_BATCH_SIZE])
            mc.flush(COLLECTION)

            record.status = "ready"
            record.chunk_count = len(chunks)
            logger.info("文档 %s(%s) 入库完成：%d 个分块", filename, doc_id, len(chunks))
        except Exception as exc:  # noqa: BLE001 — 后台任务需吞掉异常转为 failed
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"[:500]
            logger.error("文档 %s 入库失败：%s", doc_id, exc, exc_info=True)
        db.commit()
    finally:
        db.close()