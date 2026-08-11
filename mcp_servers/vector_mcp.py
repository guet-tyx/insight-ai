"""VectorStoreMCP —— Milvus 向量检索 MCP Server（FastMCP 3.x，Streamable HTTP）。

暴露原语（对应计划 W9 表格：VectorStoreMCP @mcp.tool）：
- knowledge_search: 混合检索（向量+图谱 RRF 融合）
- store_chunk: 向量写入（文档片段入库）
资源：vector://collections 集合状态（只读，不暴露敏感配置）

启动：cd backend && uv run python ../mcp_servers/vector_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from fastmcp import FastMCP

from app.services.ingest_service import COLLECTION, get_milvus_client
from app.services.retrieval_service import search as hybrid_search

mcp = FastMCP(
    name="VectorStoreMCP",
    instructions="Insight AI 向量检索服务：知识库混合检索与片段写入",
)

_started_at = time.time()


@mcp.tool
async def knowledge_search(query: str, top_k: int = 5) -> dict:
    """知识库混合检索（Milvus 向量 + Neo4j 图谱 → RRF 融合 Top-K）。

    - query: 检索问题/关键词
    - top_k: 返回片段数（1-10）
    返回带溯源的结果列表（source_type: vector/graph）。
    """
    try:
        hits = hybrid_search(query, top_k=max(1, min(top_k, 10)))
        return {"query": query, "hits": [h.model_dump() for h in hits]}
    except Exception as exc:
        return {"query": query, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool
async def store_chunk(doc_id: str, text: str, metadata: dict | None = None) -> dict:
    """向量写入：单片段入库（嵌入 + 写入 Milvus）。

    - doc_id: 文档标识（≤32 字符）
    - text: 片段文本
    - metadata: 可选 {page_number, parent_header}
    返回写入状态与片段数。
    """
    try:
        from app.services.ingest_service import _embedder

        emb = _embedder()
        vector = await asyncio.get_running_loop().run_in_executor(
            None, lambda: emb.embed_query(text)
        )
        meta = metadata or {}
        mc = get_milvus_client()
        mc.insert(
            COLLECTION,
            [
                {
                    "vector": vector,
                    "text": text,
                    "doc_id": doc_id[:32],
                    "page_number": int(meta.get("page_number", 0)),
                    "parent_header": str(meta.get("parent_header", ""))[:512],
                    "chunk_index": int(meta.get("chunk_index", 0)),
                }
            ],
        )
        mc.flush(COLLECTION)
        return {"status": "ok", "doc_id": doc_id[:32], "chunk_bytes": len(text)}
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


@mcp.resource("vector://collections")
def vector_collections() -> str:
    """向量集合状态（只读）。"""
    try:
        mc = get_milvus_client()
        return json.dumps(
            {
                "service": "VectorStoreMCP",
                "collections": mc.list_collections(),
                "uptime_seconds": int(time.time() - _started_at),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


if __name__ == "__main__":
    # 端口/监听地址解析：uv run python ../mcp_servers/xxx_mcp.py --port 8101 [--host 0.0.0.0]
    # 本机默认 127.0.0.1（安全）；Docker 容器内需 --host 0.0.0.0 供其它容器经服务名访问
    _port = 8000
    _host = "127.0.0.1"
    if "--port" in sys.argv:
        _port = int(sys.argv[sys.argv.index("--port") + 1])
    if "--host" in sys.argv:
        _host = sys.argv[sys.argv.index("--host") + 1]
    mcp.run(transport="http", host=_host, port=_port)
