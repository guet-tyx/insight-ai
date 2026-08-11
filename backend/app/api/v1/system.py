"""系统路由：MCP 注册中心诊断与热插拔刷新（W9）+ Trace 诊断面板（W10/W11）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

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


@router.get("/trace")
async def trace_summary(_: User = Depends(get_current_user)) -> dict:
    """本地 Trace 摘要（W10）：事件统计/工具失败率/延迟分位/幻觉信号。"""
    from app.services.trace_logger import trace

    return trace.summary()


@router.get("/trace/ui", response_class=HTMLResponse)
async def trace_panel() -> HTMLResponse:
    """Trace 诊断面板（W11）：自包含 HTML（深色卡片 + JWT 轮询）。

    首帧由服务端渲染（Docker 部署后直接访问）；登录态（localStorage
    insight_token）下每 3s 自动刷新。数据无敏感 Key，只读诊断。
    """
    from app.services.trace_logger import panel_html, trace

    return HTMLResponse(content=panel_html(trace.summary()))
