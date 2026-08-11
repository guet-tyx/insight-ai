"""MCPPluginRegistry —— MCP 工具服务注册中心（W9 热插拔）。

- 配置驱动端点（.env MCP_SERVERS，JSON 数组）
- 握手超时+重试（计划风险表）：单端点宕机隔离，不影响其它端点
- Capabilities Discovery：list_tools() 缓存远端工具 Schema
- 远端工具 → langchain BaseTool 适配（供 LangGraph Agent 直接挂载）
- 双模式：registry 无可用端点时调用方回退本地实现
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 5.0      # 握手超时（秒）
MAX_RETRIES = 2              # 单端点重试次数
TOOL_CALL_TIMEOUT = 180.0    # 远端工具调用超时（浏览器采集可达分钟级）


class ServerStatus(BaseModel):
    """单端点诊断状态。"""

    endpoint: str
    ok: bool = False
    name: str = ""
    tools: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None


class MCPRegistry:
    """工具服务注册中心（进程内单例）。"""

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}      # endpoint -> fastmcp.Client
        self._tools: dict[str, list[Any]] = {}  # endpoint -> [mcp.types.Tool]
        self._status: dict[str, ServerStatus] = {}
        self._lock = asyncio.Lock()
        self._refreshed_at: float = 0.0

    @property
    def endpoints(self) -> list[str]:
        """配置端点列表（.env MCP_SERVERS JSON 数组；空 → 无 MCP）。"""
        raw = settings.mcp_servers or "[]"
        try:
            parsed = json.loads(raw)
            return [str(e).strip().rstrip("/") for e in parsed if str(e).strip()]
        except json.JSONDecodeError:
            logger.warning("MCP_SERVERS 配置非合法 JSON：%s", raw[:80])
            return []

    async def _connect_endpoint(self, endpoint: str) -> ServerStatus:
        """单端点握手（超时+重试，失败隔离）。"""
        from fastmcp import Client
        from fastmcp.client.transports.http import StreamableHttpTransport

        status = ServerStatus(endpoint=endpoint)
        for attempt in range(1, MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                transport = StreamableHttpTransport(url=f"{endpoint}/mcp")
                client = await asyncio.wait_for(
                    Client(transport=transport).__aenter__(),
                    timeout=HANDSHAKE_TIMEOUT,
                )
                await asyncio.wait_for(client.initialize(), timeout=HANDSHAKE_TIMEOUT)
                tools = await asyncio.wait_for(client.list_tools(), timeout=HANDSHAKE_TIMEOUT)
                status.ok = True
                status.name = str(getattr(client, "server_info", {}).get("name", endpoint))
                status.tools = [t.model_dump(exclude_none=True) for t in tools]
                status.latency_ms = int((time.monotonic() - start) * 1000)
                self._clients[endpoint] = client
                self._tools[endpoint] = tools
                logger.info("MCP 握手成功 %s（%d 工具，%dms）", endpoint, len(tools), status.latency_ms)
                return status
            except Exception as exc:  # noqa: BLE001 — 单端点失败隔离
                status.error = f"{type(exc).__name__}: {exc}"[:200]
                logger.warning("MCP 握手失败 %s（第 %d/%d 次）：%s",
                               endpoint, attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1.0)
        self._clients.pop(endpoint, None)
        self._tools.pop(endpoint, None)
        return status

    async def refresh(self) -> list[ServerStatus]:
        """全量握手刷新（幂等；新端点热插拔入口）。"""
        async with self._lock:
            statuses = []
            for endpoint in self.endpoints:
                statuses.append(await self._connect_endpoint(endpoint))
                self._status[endpoint] = statuses[-1]
            self._refreshed_at = time.time()
            # 清理已下线的端点
            for old in list(self._status):
                if old not in self.endpoints:
                    self._status.pop(old, None)
                    self._clients.pop(old, None)
                    self._tools.pop(old, None)
            return statuses

    def status(self) -> list[ServerStatus]:
        """诊断快照（无握手，读缓存）。"""
        return list(self._status.values())

    @property
    def ready(self) -> bool:
        return any(s.ok for s in self._status.values())

    def remote_tools(self) -> list[Any]:
        """全部可用远端工具（mcp.types.Tool）。"""
        return [t for tools in self._tools.values() for t in tools]

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """调用远端工具（按注册顺序找第一个提供该工具的端点）。"""
        for endpoint, client in self._clients.items():
            if any(t.name == tool_name for t in self._tools.get(endpoint, [])):
                result = await asyncio.wait_for(
                    client.call_tool(tool_name, arguments),
                    timeout=TOOL_CALL_TIMEOUT,
                )
                return result
        raise KeyError(f"远端工具不存在: {tool_name}")


registry = MCPRegistry()


# ---------- langchain BaseTool 适配 ----------

def build_langchain_tools(remote_tools: list[Any]):
    """把 MCP 远端工具包装为 langchain BaseTool 列表（供 Agent 挂载）。

    参数 Schema 用 JSON Schema → pydantic model 动态构造（stub 足够，
    实际校验发生在 call_tool 的 arguments dict 传递时）。
    """
    from langchain_core.tools import BaseTool, StructuredTool

    tools: list[BaseTool] = []
    from pydantic import create_model

    for tool in remote_tools:
        name = tool.name
        description = tool.description or f"MCP 远端工具 {name}"

        async def _run(_n: str = name, **kwargs: Any) -> Any:
            # langchain 按参数名以 kwargs 调用；远端按 dict 转发
            result = await registry.call(_n, kwargs)
            # MCP CallToolResult 归一为字符串（防御：text 字段缺失按空串处理）
            if hasattr(result, "content") and isinstance(result.content, list):
                return "\n".join(
                    str(getattr(c, "text", "")) for c in result.content
                    if getattr(c, "type", "") == "text"
                )
            return str(result)

        # 用远端 inputSchema 显式构造参数模型（避免函数签名推断丢参）
        input_schema = tool.inputSchema or {"type": "object", "properties": {}}
        props = input_schema.get("properties", {}) or {}
        required = set(input_schema.get("required", []) or [])
        fields = {}
        for pname, pdef in props.items():
            ptype = {
                "string": str, "integer": int, "number": float,
                "boolean": bool, "object": dict, "array": list,
            }.get(pdef.get("type"), Any)
            default = ... if pname in required else None
            fields[pname] = (ptype, default)
        args_model = create_model(f"Remote_{name}", **fields) if fields else None

        tools.append(
            StructuredTool.from_function(
                coroutine=_run,
                name=name,
                description=description,
                args_schema=args_model,
            )
        )
    return tools


async def ensure_registry() -> list[ServerStatus]:
    """首次使用时自动握手（幂等），返回状态列表。"""
    if not registry.ready and registry.endpoints:
        await registry.refresh()
    return registry.status()