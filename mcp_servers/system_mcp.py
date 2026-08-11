"""SystemConfigMCP —— 系统配置只读资源 MCP Server（FastMCP 3.x，Streamable HTTP）。

暴露原语（对应计划 W9 表格：SystemConfigMCP @mcp.resource，只读）：
- system://config: 知识库 Schema 与模型配置（不暴露任何密钥）

启动：cd backend && uv run python ../mcp_servers/system_mcp.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from fastmcp import FastMCP  # noqa: E402

from app.core.config import settings  # noqa: E402

mcp = FastMCP(
    name="SystemConfigMCP",
    instructions="Insight AI 系统配置只读服务",
)

_started_at = time.time()


@mcp.resource("system://config")
def system_config() -> str:
    """系统配置摘要（只读，脱敏：不暴露任何 API Key/密码）。"""
    return json.dumps({
        "service": "SystemConfigMCP",
        "app": settings.app_name,
        "version": settings.app_version,
        "llm_model": settings.llm_model,
        "llm_model_lite": settings.llm_model_lite,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "milvus_uri": settings.milvus_uri,
        "neo4j_uri": settings.neo4j_uri,
        "checkpointer_backend": settings.checkpointer_backend,
        "collector_profile_mode": settings.collector_browser_profile or "isolated",
        "uptime_seconds": int(time.time() - _started_at),
    }, ensure_ascii=False)


if __name__ == "__main__":
    # 端口解析：uv run python ../mcp_servers/xxx_mcp.py --port 8101
    _port = 8000
    if "--port" in sys.argv:
        _port = int(sys.argv[sys.argv.index("--port") + 1])
    mcp.run(transport="http", host="127.0.0.1", port=_port)