"""采集相关 Pydantic Schema（含 W6 预置领域输出 Schema）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CollectRequest(BaseModel):
    url: str = Field(min_length=5, max_length=2048, description="目标 URL（http/https，支持 RSS feed）")
    instruction: str = Field(min_length=1, max_length=2000, description="自然语言提取指令")
    source: str = Field(
        default="auto",
        description="采集路由：auto（特征识别）/ rss（强制 RSS 解析）/ web（强制浏览器）",
    )
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


# ---------- W6：预置领域输出 Schema ----------

class RssItem(BaseModel):
    """RSS/Atom 条目（规范化输出）。"""

    title: str
    link: str
    published: str = ""
    summary: str = ""
    source_url: str = ""


class RssExtract(BaseModel):
    """RSS 采集默认输出（items 为规范化条目列表）。"""

    items: list[RssItem]
    feed_title: str = ""


class WebExtract(BaseModel):
    """网页采集默认输出（供强类型结构化验证）。"""

    title: str
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    extracted_at: str = ""