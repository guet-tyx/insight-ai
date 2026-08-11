"""知识库相关 Pydantic Schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UploadResponse(BaseModel):
    doc_id: str
    filename: str
    status: str = "processing"


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    doc_id: str
    filename: str
    status: str
    chunk_count: int
    graph_count: int = 0  # W7: 图谱实体数
    error: str | None = None
    created_at: datetime


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000, description="检索问题")
    top_k: int = Field(default=5, ge=1, le=10, description="召回片段数")


class SourceHit(BaseModel):
    """检索命中的知识片段（携带溯源信息）。"""

    chunk_text: str
    score: float
    doc_id: str
    page_number: int
    parent_header: str = ""
    source_type: str = "vector"  # W7: vector（向量召回）/ graph（图谱路径）


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceHit]
