"""知识库路由（W2）：文档上传（后台异步入库）/ 列表 / 状态 / 向量问答。"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.document import Document
from app.models.user import User
from app.schemas.document import DocumentOut, QueryRequest, QueryResponse, UploadResponse
from app.services.ingest_service import ingest_document

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB
ALLOWED_SUFFIX = ".pdf"


@router.post("/documents/upload", response_model=UploadResponse, status_code=202)
async def upload_document(
    file: UploadFile = File(description="PDF 文档"),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> UploadResponse:
    """上传 PDF：202 立即返回，解析/向量化在后台执行（快速响应，规避大文件超时）。"""
    filename = file.filename or "unnamed.pdf"
    if not filename.lower().endswith(ALLOWED_SUFFIX):
        raise HTTPException(status_code=422, detail="仅支持 PDF 文件")
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 20MB 上限")
    if not content:
        raise HTTPException(status_code=422, detail="文件内容为空")

    record = Document(doc_id=uuid4().hex, filename=filename, status="processing")
    db.add(record)
    db.commit()

    # 响应返回后由 FastAPI 执行（TestClient 同样会执行，测试可同步断言）
    background_tasks.add_task(ingest_document, record.doc_id, content, filename)
    return UploadResponse(doc_id=record.doc_id, filename=filename, status="processing")


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[Document]:
    """已入库文档列表（含处理状态）。"""
    return list(db.scalars(select(Document).order_by(Document.created_at.desc())))


@router.get("/documents/{doc_id}", response_model=DocumentOut)
def get_document(
    doc_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Document:
    """单文档状态（上传后轮询用：processing → ready / failed）。"""
    record = db.get(Document, doc_id)
    if record is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return record


@router.post("/query", response_model=QueryResponse)
def query_knowledge(
    payload: QueryRequest,
    _: User = Depends(get_current_user),
) -> QueryResponse:
    """向量检索 + LLM 引证式回答（RAG 闭环）。"""
    if not settings.siliconflow_api_key:
        raise HTTPException(status_code=503, detail="Embedding 服务未配置（SILICONFLOW_API_KEY）")
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="LLM 服务未配置（OPENAI_API_KEY）")
    from app.services.retrieval_service import answer

    return answer(payload.query, top_k=payload.top_k)