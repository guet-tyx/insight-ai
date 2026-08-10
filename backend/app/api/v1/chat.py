"""对话路由（占位）：W4 实现 SSE 流式对话（Agent 工具调用链路）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/sessions")
def create_session() -> None:
    """创建对话会话。"""
    raise HTTPException(status_code=501, detail="W4 实现：会话管理与 SSE 流式对话")


@router.post("/sessions/{session_id}/messages")
def send_message(session_id: str) -> None:
    """发送消息并流式返回 Agent 执行结果。"""
    raise HTTPException(status_code=501, detail="W4 实现：SSE 流式传输")