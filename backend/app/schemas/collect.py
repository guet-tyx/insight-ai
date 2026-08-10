"""采集相关 Pydantic Schema。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CollectRequest(BaseModel):
    url: str = Field(min_length=5, max_length=2048, description="目标网页 URL（http/https）")
    instruction: str = Field(min_length=1, max_length=2000, description="自然语言提取指令")
    output_schema: dict[str, Any] | None = Field(
        default=None,
        description="可选 JSON Schema（type=object + properties），开启强类型结构化输出",
    )
    max_steps: int = Field(default=30, ge=5, le=100, description="Agent 最大执行步数")


class CollectStartResponse(BaseModel):
    task_id: str
    url: str
    status: str = "running"


class CollectTaskOut(BaseModel):
    task_id: str
    url: str
    status: str  # running / ready / failed
    data: Any | None = None
    error: str | None = None
    created_at: datetime