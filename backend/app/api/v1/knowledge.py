"""知识库路由（占位）：W2 实现 PDF 上传、解析入库与向量问答。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/documents")
def list_documents() -> None:
    """查询已入库文档列表。"""
    raise HTTPException(status_code=501, detail="W2 实现：PyMuPDF 解析 + Milvus 写入")


@router.post("/documents/upload")
def upload_document() -> None:
    """上传 PDF 文档并触发解析入库。"""
    raise HTTPException(status_code=501, detail="W2 实现：文档上传与向量化")


@router.post("/query")
def query_knowledge() -> None:
    """知识库向量检索问答。"""
    raise HTTPException(status_code=501, detail="W2 实现：基础 RAG 问答")