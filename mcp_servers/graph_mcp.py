"""GraphStoreMCP —— Neo4j 图查询 MCP Server（FastMCP 3.x，Streamable HTTP）。

暴露原语（对应计划 W9 表格：GraphStoreMCP @mcp.tool）：
- graph_query: 知识图谱 1-2 跳拓扑查询（Cypher 参数化防注入）
资源：graph://stats 图谱统计（只读）

启动：cd backend && uv run python ../mcp_servers/graph_mcp.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from fastmcp import FastMCP

from app.services.graph_service import get_driver, graph_search

mcp = FastMCP(
    name="GraphStoreMCP",
    instructions="Insight AI 知识图谱服务：实体关系拓扑查询",
)

_started_at = time.time()


@mcp.tool
async def graph_query(query: str, max_hops: int = 2) -> dict:
    """知识图谱拓扑查询（1-2 跳路径，Cypher 全参数化防注入）。

    - query: 自然语言查询（内部自动提取核心实体名定位节点）
    - max_hops: 路径跳数上限（1-3）
    返回路径文本列表（A--[DEVELOPED]-->B 形式，含证据引用）。
    """
    try:
        paths = graph_search(query, max_hops=max(1, min(max_hops, 3)))
        return {"query": query, "paths": paths}  # noqa: TRY300 — 成功路径直接返回
    except Exception as exc:
        return {"query": query, "error": f"{type(exc).__name__}: {exc}"}


@mcp.resource("graph://stats")
def graph_stats() -> str:
    """图谱统计信息（只读）。"""
    try:
        driver = get_driver()
        with driver.session() as s:
            nodes = s.run("MATCH (n:Entity) RETURN count(n) AS c").single()["c"]
            rels = s.run("MATCH ()-[r:REL]->() RETURN count(r) AS c").single()["c"]
        driver.close()
        return json.dumps(
            {
                "service": "GraphStoreMCP",
                "entity_nodes": nodes,
                "relations": rels,
                "uptime_seconds": int(time.time() - _started_at),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


if __name__ == "__main__":
    # 端口/监听地址解析：uv run python ../mcp_servers/xxx_mcp.py --port 8101 [--host 0.0.0.0]
    # 本机默认 127.0.0.1（安全）；Docker 容器内需 --host 0.0.0.0 供其它容器经服务名访问
    _port = 8000
    _host = "127.0.0.1"
    if "--port" in sys.argv:
        _port = int(sys.argv[sys.argv.index("--port") + 1])
    if "--host" in sys.argv:
        _host = sys.argv[sys.argv.index("--host") + 1]
    mcp.run(transport="http", host=_host, port=_port)
