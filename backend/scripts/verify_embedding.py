"""嵌入链路验证脚本：硅基流动 BAAI/bge-m3 → Milvus 写入与检索

用法（backend/ 目录下）：
    uv run python scripts/verify_embedding.py
依赖 backend/.env 中的 SILICONFLOW_API_KEY 等配置（可先复制 .env.example 为 .env）
"""
from __future__ import annotations

import os
import sys

# 允许从项目根加载 .env
sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))

from langchain_openai import OpenAIEmbeddings  # noqa: E402
from pymilvus import MilvusClient  # noqa: E402

COLLECTION = "embedding_smoke_test"
DIM = int(os.getenv("EMBEDDING_DIM", "1024"))


def main() -> None:
    api_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key or api_key.startswith("sk-xxx"):
        print("[FAIL] 未配置 SILICONFLOW_API_KEY，请先注册 https://cloud.siliconflow.cn 并填入 backend/.env")
        sys.exit(1)

    model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    base_url = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")

    # 1) 嵌入
    # check_embedding_ctx_length=False 必须关闭：
    # langchain-openai 默认会本地 tiktoken 化并发送 token ID 数组，
    # 硅基流动这类 OpenAI 兼容服务无法正确解析，导致向量失真
    emb = OpenAIEmbeddings(
        model=model, base_url=base_url, api_key=api_key,
        check_embedding_ctx_length=False,
    )
    docs = [
        "Insight AI 是一个基于 LangGraph 多智能体与 MCP 架构的情报分析平台。",
        "Milvus 是开源的分布式向量数据库，支持 HNSW 索引与余弦相似度检索。",
        "Neo4j 使用 Cypher 查询语言进行图拓扑分析。",
        "完全无关的英文句子 about coffee brewing temperature.",
    ]
    vectors = emb.embed_documents(docs)
    print(f"[OK] 嵌入模型 {model} 返回 {len(vectors)} 条向量, 维度={len(vectors[0])} (预期 {DIM})")
    assert all(len(v) == DIM for v in vectors), "维度与 EMBEDDING_DIM 不一致"

    # 2) Milvus 写入 + 检索
    mc = MilvusClient(uri=os.getenv("MILVUS_URI", "http://127.0.0.1:19530"))
    mc.drop_collection(COLLECTION) if mc.has_collection(COLLECTION) else None
    mc.create_collection(COLLECTION, dimension=DIM, metric_type="COSINE")
    mc.insert(COLLECTION, [{"id": i, "vector": v, "text": t} for i, (v, t) in enumerate(zip(vectors, docs))])
    mc.flush(COLLECTION)

    q = emb.embed_query("Insight AI 情报分析平台是什么？")
    hits = mc.search(COLLECTION, data=[q], limit=2, output_fields=["text"])
    top = hits[0][0]
    print(f"[OK] 检索 Top1: score={top['distance']:.4f}")
    print(f"     命中文本: {top['entity']['text'][:40]}...")
    mc.drop_collection(COLLECTION)
    print("\n[PASS] 嵌入 → Milvus 全链路验证通过")


if __name__ == "__main__":
    main()
