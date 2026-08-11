"""系统路由：MCP 注册中心诊断与热插拔刷新（W9）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.models.user import User
from app.services.mcp_registry import registry

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/mcp")
async def mcp_status(_: User = Depends(get_current_user)) -> dict:
    """MCP 注册中心诊断：各服务状态/工具清单/延迟（只读缓存）。"""
    statuses = registry.status()
    return {
        "ready": registry.ready,
        "servers": [s.model_dump() for s in statuses],
        "total_tools": len(registry.remote_tools()),
    }


@router.post("/mcp/refresh")
async def mcp_refresh(_: User = Depends(get_current_user)) -> dict:
    """热插拔刷新：重新握手全部端点（新增 MCP Server 无需重启主服务）。"""
    from app.agents.tool_factory import reset_tools_cache

    statuses = await registry.refresh()
    reset_tools_cache()  # Agent 下次取工具时重建（MCP 优先）
    return {
        "refreshed": True,
        "servers": [s.model_dump() for s in statuses],
    }