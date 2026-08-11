"""W9 MCP 注册中心测试：握手/隔离/工具适配/调用/回退。"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.tool_factory import get_agent_tools, reset_tools_cache
from app.services.mcp_registry import build_langchain_tools, registry


# ---------- 单元：配置解析 / 调用边界 ----------

def test_endpoints_json_decode_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP_SERVERS 非合法 JSON → 空端点（安全降级本地）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "mcp_servers", "not-json{{")
    assert registry.endpoints == []


def test_endpoints_parse_strip_and_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "mcp_servers", '["http://a:1/", " http://b:2 ", ""]')
    assert registry.endpoints == ["http://a:1", "http://b:2"]


def test_registry_call_unknown_tool_raises() -> None:
    """无任何端点提供目标工具 → KeyError（Agent 据此回退本地实现）。"""
    registry._clients.clear()  # noqa: SLF001 — 隔离其它测试的缓存
    registry._tools.clear()  # noqa: SLF001
    registry._status.clear()  # noqa: SLF001

    async def _run() -> None:
        with pytest.raises(KeyError, match="远端工具不存在"):
            await registry.call("ghost_tool", {})
        assert not registry.ready  # 空注册表 → 未就绪

    asyncio.run(_run())


def test_ensure_registry_without_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """无配置端点 → ensure_registry 直接返回空（不握手不报错）。"""
    from app.core.config import settings
    from app.services.mcp_registry import ensure_registry

    monkeypatch.setattr(settings, "mcp_servers", "[]")
    registry._clients.clear()  # noqa: SLF001
    registry._tools.clear()  # noqa: SLF001
    registry._status.clear()  # noqa: SLF001
    assert asyncio.run(ensure_registry()) == []


def test_build_langchain_tools_args_schema_fields() -> None:
    """远端 inputSchema properties/required → StructuredTool 参数模型。"""
    from mcp.types import Tool

    fake = Tool(name="collect2", description="采集", inputSchema={
        "type": "object",
        "properties": {"url": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["url"],
    })
    tools = build_langchain_tools([fake])
    fields = tools[0].args_schema.model_fields
    assert "url" in fields and "limit" in fields
    assert fields["url"].is_required()
    assert not fields["limit"].is_required()


def test_remote_tool_invoke_normalizes_mcp_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """远端调用结果归一：text 内容按行合并；无 content → str()。"""
    from mcp.types import Tool

    class _Text:
        type = "text"

        def __init__(self, text: str) -> None:
            self.text = text

    class _Res:
        content: list

        def __init__(self, content: list) -> None:
            self.content = content

    fake_tool = Tool(name="echo3", description="回显", inputSchema={
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    })
    tools = build_langchain_tools([fake_tool])

    async def _fake_call(name: str, args: dict):
        assert name == "echo3"
        assert args == {"q": "hi"}
        return _Res(content=[_Text("第一段"), _Text("第二段")])

    monkeypatch.setattr(registry, "call", _fake_call)
    out = asyncio.run(tools[0].ainvoke({"q": "hi"}))
    assert out == "第一段\n第二段"

    # 非 text 内容结构（无 .text 属性）→ 过滤后为空串
    class _Bare:
        type = "text"  # noqa: A003

    async def _fake_call_bare(name: str, args: dict):
        return _Res(content=[_Bare()])

    monkeypatch.setattr(registry, "call", _fake_call_bare)
    assert asyncio.run(tools[0].ainvoke({"q": "x"})) == ""

    # 无 content 属性 → str(result) 兜底
    async def _fake_call_plain(name: str, args: dict):
        return {"json": "ok"}

    monkeypatch.setattr(registry, "call", _fake_call_plain)
    assert asyncio.run(tools[0].ainvoke({"q": "x"})) == "{'json': 'ok'}"


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


# ---------- 集成：真实 HTTP 握手（随机端口 FastMCP server） ----------

@pytest.fixture(scope="module")
def http_mcp_server() -> str:
    """线程内 FastMCP HTTP 服务（随机端口），返回 endpoint（供 registry 握手）。"""
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    from fastmcp import FastMCP

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = FastMCP("PytestMCP")

    @server.tool
    async def echo(text: str) -> str:
        return f"echo:{text}"

    def _run() -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())
        server.run(transport="http", host="127.0.0.1", port=port)

    threading.Thread(target=_run, daemon=True).start()
    # 等待端口就绪（GET /mcp 返回 405 也算服务已监听）
    deadline = time.time() + 15
    probe = f"http://127.0.0.1:{port}/mcp"
    while time.time() < deadline:
        try:
            urllib.request.urlopen(probe, timeout=1)
            break
        except urllib.error.HTTPError:
            break  # 405 = 服务已监听，正常响应
        except Exception:  # noqa: BLE001 — 端口未就绪
            time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"


def _reset_registry() -> None:
    """清理其它测试在注册中心留下的缓存。"""
    registry._clients.clear()  # noqa: SLF001
    registry._tools.clear()  # noqa: SLF001
    registry._status.clear()  # noqa: SLF001


def test_registry_http_refresh_and_call(http_mcp_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 握手：发现工具 + status 记录 + remote_tools + call_tool 转发。"""
    import json as _json

    from app.core.config import settings

    monkeypatch.setattr(settings, "mcp_servers", _json.dumps([http_mcp_server]))
    _reset_registry()

    async def _run() -> str:
        statuses = await registry.refresh()
        assert len(statuses) == 1
        st = statuses[0]
        assert st.ok is True
        assert any(t["name"] == "echo" for t in st.tools)
        assert st.latency_ms >= 0
        assert registry.ready is True
        assert "echo" in [t.name for t in registry.remote_tools()]
        result = await registry.call("echo", {"text": "hi"})
        return "\n".join(
            str(c.text) for c in result.content if getattr(c, "type", "") == "text"
        )

    assert asyncio.run(_run()) == "echo:hi"


def test_registry_handshake_failure_isolated(
    http_mcp_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单端点宕机 → 隔离（error 记录 + 重试后放弃），不影响健康端点。"""
    import json as _json

    import app.services.mcp_registry as mreg
    from app.core.config import settings

    monkeypatch.setattr(mreg, "HANDSHAKE_TIMEOUT", 0.5)  # 加速失败探测
    monkeypatch.setattr(settings, "mcp_servers", _json.dumps(
        ["http://127.0.0.1:1", http_mcp_server]
    ))
    _reset_registry()

    async def _run() -> tuple[list, bool]:
        statuses = await registry.refresh()
        return statuses, registry.ready

    statuses, ready = asyncio.run(_run())
    assert len(statuses) == 2
    assert statuses[0].ok is False and statuses[0].error  # 坏端点：错误有记录
    assert statuses[1].ok is True                          # 好端点不受影响
    assert ready is True                                   # 有健康端点 → ready


def test_registry_refresh_drops_offline_endpoints(
    http_mcp_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端点下线后 refresh → 注册表清理旧缓存（status/clients/tools）。"""
    import json as _json

    import app.services.mcp_registry as mreg
    from app.core.config import settings

    monkeypatch.setattr(mreg, "HANDSHAKE_TIMEOUT", 0.5)
    monkeypatch.setattr(settings, "mcp_servers", _json.dumps([http_mcp_server]))
    _reset_registry()

    async def _first() -> None:
        await registry.refresh()
        assert len(registry._status) == 1  # noqa: SLF001

    asyncio.run(_first())

    # 端点全下线 → 第二次 refresh 后缓存被清理
    monkeypatch.setattr(settings, "mcp_servers", "[]")
    _reset_registry()
    asyncio.run(registry.refresh())
    assert registry.status() == []