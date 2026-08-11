"""W9 MCP 注册中心测试：握手/隔离/工具适配/调用/回退。"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.tool_factory import get_agent_tools, reset_tools_cache
from app.services.mcp_registry import build_langchain_tools, registry


# ---------- 单元：工具 Schema → BaseTool 适配 ----------

def test_build_langchain_tools_shape() -> None:
    """远端 Tool schema → langchain BaseTool（StructuredTool 包装）。"""
    from mcp.types import Tool

    fake_tool = Tool(name="echo", description="回显", inputSchema={"type": "object"})
    tools = build_langchain_tools([fake_tool])
    assert len(tools) == 1
    assert tools[0].name == "echo"
    assert "回显" in tools[0].description
    assert hasattr(tools[0], "invoke") or hasattr(tools[0], "ainvoke")


# ---------- 集成：内存 transport 起 BrowserMCP → 注册中心调用 ----------

@pytest.mark.flaky(reruns=2, reruns_delay=2)
def test_registry_memory_transport_call(local_test_page: str) -> None:
    """FastMCPTransport 内存直连：发现工具 + 调用 collect_webpage（本地测试页）。"""
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports.memory import FastMCPTransport

    async def _run() -> str:
        from app.services.collector_service import collect as run_collect

        # 用真实 BrowserMCP 逻辑的最小 server（避免启动浏览器进程耗时）
        server = FastMCP("TestBrowserMCP")

        @server.tool
        async def collect_webpage(url: str, instruction: str) -> dict:
            result = await run_collect(url, instruction, source="tls", max_steps=5,
                                       allow_internal=True)
            return result if isinstance(result, dict) else {"text": str(result)}

        transport = FastMCPTransport(server)
        client = await Client(transport=transport).__aenter__()
        await client.initialize()
        tools = await client.list_tools()
        assert any(t.name == "collect_webpage" for t in tools)

        result = await client.call_tool("collect_webpage", {
            "url": local_test_page, "instruction": "提取页面内容",
        })
        text = "\n".join(
            str(c.text) for c in result.content if getattr(c, "type", "") == "text"
        )
        await client.close()
        return text

    out = asyncio.run(_run())
    assert "Insight AI" in out  # 本地测试页内容经 MCP 工具链路返回


# ---------- 双模式：MCP 优先 / 本地回退 ----------

@pytest.mark.flaky(reruns=2, reruns_delay=2)
def test_tool_factory_mcp_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP 端点可用时工具来自远端（名称含远端工具）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "mcp_servers", '["http://127.0.0.1:8101"]')
    reset_tools_cache()

    async def _run() -> tuple[list, str]:
        from app.agents.tool_factory import current_mode

        tools = await get_agent_tools()
        return tools, current_mode()

    tools, mode = asyncio.run(_run())
    assert mode == "mcp"
    names = {t.name for t in tools}
    assert "collect_webpage" in names and "fetch_rss" in names  # 远端 BrowserMCP 工具


def test_tool_factory_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP 端点不可用 → 回退本地工具（同名）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "mcp_servers", "[]")
    reset_tools_cache()

    async def _run() -> tuple[list, str]:
        from app.agents.tool_factory import current_mode

        tools = await get_agent_tools()
        return tools, current_mode()

    tools, mode = asyncio.run(_run())
    assert mode == "local"
    names = {t.name for t in tools}
    assert "knowledge_search" in names and "collect_webpage" in names


# ---------- API 鉴权 ----------

def test_system_mcp_requires_auth() -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/system/mcp").status_code == 401
        assert client.post("/api/v1/system/mcp/refresh").status_code == 401