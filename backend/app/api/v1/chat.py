"""对话路由（W4）：会话管理 + SSE 流式 Agent 问答（替换 501 占位）。"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services import agent_service

router = APIRouter(prefix="/chat", tags=["chat"])


class SessionOut(BaseModel):
    session_id: str


class ChatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000, description="用户消息")


class ChatMessageOut(BaseModel):
    role: str  # user / assistant
    content: str


@router.post("/sessions", response_model=SessionOut, status_code=201)
def create_session(_: User = Depends(get_current_user)) -> SessionOut:
    """创建会话：返回 session_id，后续消息携带该 ID 保持多轮上下文。"""
    return SessionOut(session_id=uuid4().hex)


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageOut])
def list_messages(
    session_id: str,
    _: User = Depends(get_current_user),
    __: Session = Depends(get_db),  # 保持依赖签名一致性（占位）
) -> list[ChatMessageOut]:
    """会话内最近对话历史（MemorySaver 检查点；未知会话 404）。"""
    try:
        agent = agent_service.get_agent()
        state = agent.get_state({"configurable": {"thread_id": session_id}})
        messages = (state.values or {}).get("messages", [])
    except Exception as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    if not messages:
        raise HTTPException(status_code=404, detail="会话不存在")
    history: list[ChatMessageOut] = []
    for msg in messages:
        if getattr(msg, "type", "") in ("human", "ai") and not getattr(msg, "tool_calls", None):
            content = agent_service._extract_token_text(getattr(msg, "content", ""))
            if content.strip():
                history.append(
                    ChatMessageOut(
                        role="user" if msg.type == "human" else "assistant", content=content
                    )
                )
    return history[-20:]  # 最近 20 条


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    payload: ChatMessageRequest,
    _: User = Depends(get_current_user),
) -> StreamingResponse:
    """发送消息并 SSE 流式返回 Agent 执行事件（详见 docs/api-v1.md 事件协议）。"""
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="LLM 服务未配置（OPENAI_API_KEY）")

    async def event_stream():
        async for frame in agent_service.stream_sse(session_id, payload.message):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx/中间层缓冲，逐帧转发
        },
    )
