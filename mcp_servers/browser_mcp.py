"""BrowserMCP —— 浏览器采集能力 MCP Server（FastMCP 3.x，Streamable HTTP）。

暴露原语（对应计划 W9 表格：BrowserMCP @mcp.tool）：
- collect_webpage: 动态网页采集（stealth 浏览器/CDP 指纹对抗 + 反爬策略）
- fetch_static: TLS 指纹静态抓取（curl_cffi Chrome 指纹）
- fetch_rss: RSS/Atom 订阅源解析
资源：browser://health 服务状态（只读）

沙盒边界：复用 collector_service.validate_url（SSRF 内网拦截），
工具无本地文件写入。

启动：cd backend && uv run python ../mcp_servers/browser_mcp.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# 复用 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from fastmcp import FastMCP

from app.core.browser_agent import CollectorError
from app.services.collector_service import collect as run_collect
from app.services.rss_service import fetch_rss as run_fetch_rss
from app.services.tls_fetch import tls_fetch

mcp = FastMCP(
    name="BrowserMCP",
    instructions="Insight AI 浏览器采集服务：动态网页/静态页/RSS 采集",
)

_started_at = time.time()


@mcp.tool
async def collect_webpage(url: str, instruction: str, max_steps: int = 20) -> dict:
    """采集动态/复杂网页内容（真实浏览器，支持反爬对抗）。

    - url: 目标网页 URL（http/https，内网地址将被拒绝）
    - instruction: 自然语言提取指令
    - max_steps: Agent 最大执行步数（默认 20）
    返回结构化结果 {url, text|title|key_points...}；验证码命中返回 status=captcha。
    """
    try:
        data = await run_collect(url, instruction, source="web", max_steps=max_steps)
        return data if isinstance(data, dict) else {"url": url, "data": str(data)}
    except CollectorError as exc:
        return {"url": url, "error": str(exc)}
    except Exception as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool
async def fetch_static(url: str) -> dict:
    """TLS 指纹静态抓取（curl_cffi Chrome 指纹，快路径 <1s）。

    适用于不依赖 JS 渲染的页面（百度百科/CSDN/博客园等公开内容页）。
    """
    try:
        result = await tls_fetch(url)
        if result.status_code in (401, 403, 429) or result.status_code >= 500:
            return {"url": url, "error": f"被目标站点拦截（HTTP {result.status_code}）"}
        return {"url": url, "text": result.text[:8000], "status_code": result.status_code}
    except Exception as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool
async def fetch_rss(feed_url: str) -> dict:
    """解析 RSS/Atom 订阅源，返回结构化条目列表（title/link/published/summary）。"""
    try:
        extract = await run_fetch_rss(feed_url)
        return extract.model_dump()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.resource("browser://health")
def browser_health() -> str:
    """浏览器采集服务的运行状态（只读）。"""
    from app.core.stealth_browser import stealth_manager

    return json.dumps(
        {
            "service": "BrowserMCP",
            "uptime_seconds": int(time.time() - _started_at),
            "stealth_cdp": bool(stealth_manager.cdp_url),
            "profile_mode": stealth_manager.profile_mode,
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    # 端口/监听地址解析：uv run python ../mcp_servers/xxx_mcp.py --port 8101 [--host 0.0.0.0]
    # 本机默认 127.0.0.1（安全）；Docker 容器内需 --host 0.0.0.0 供其它容器经服务名访问
    _port = 8000
    _host = "127.0.0.1"
    if "--port" in sys.argv:
        _port = int(sys.argv[sys.argv.index("--port") + 1])
    if "--host" in sys.argv:
        _host = sys.argv[sys.argv.index("--host") + 1]
    mcp.run(
        transport="http", host=_host, port=_port
    )  # Streamable HTTP（默认 8000；可 --port 指定）
