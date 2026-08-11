"""健康探针：容器编排 / 负载均衡存活检查用。"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """存活探针，返回服务与数据库状态快照。"""
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
    }
