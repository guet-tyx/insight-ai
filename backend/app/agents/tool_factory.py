"""Agent 工具工厂（W9 双模式）：MCP 远端优先，本地实现回退。

- MCP 优先：registry 可用时 Agent 挂载远端工具（Browser/Vector/Graph MCP）
- 本地回退：注册中心无可用端点 → 本地 @tool（行为一致）
"""
from __future__ import annotations

import logging

from app.services.mcp_registry import build_langchain_tools, ensure_registry, registry

logger = logging.getLogger(__name__)

_tools_cache: list | None = None
_mode = "local"


def _local_tools() -> list:
    """本地工具集（与远端同名的实现）。"""
    from app.services.agent_service import collect_webpage, knowledge_search

    return [knowledge_search, collect_webpage]


async def get_agent_tools() -> list:
    """获取 Agent 工具列表（MCP 优先，缓存；refresh 后自动更新）。"""
    global _tools_cache, _mode
    if _tools_cache is not None:
        return _tools_cache
    # 无配置端点 → 直接本地（不依赖 registry.ready 的历史缓存）
    if not registry.endpoints:
        _tools_cache = _local_tools()
        _mode = "local"
        logger.info("Agent 工具模式：本地实现（未配置 MCP 端点）")
        return _tools_cache
    try:
        statuses = await ensure_registry()
        if registry.ready:
            remote = registry.remote_tools()
            remote_tools = build_langchain_tools(remote)  # mcp.Tool → BaseTool
            remote_names = {t.name for t in remote}
            # 双模式按工具合并：远端优先，缺口用本地实现补齐
            local = _local_tools()
            merged = list(remote_tools) + [t for t in local if t.name not in remote_names]
            _tools_cache = merged
            _mode = "mcp"
            logger.info("Agent 工具模式：MCP 远端 + 本地补齐（%d 工具，远端 %d/%d 服务）",
                        len(merged), len(remote), sum(1 for s in statuses if s.ok))
            return _tools_cache
    except Exception as exc:  # noqa: BLE001 — MCP 任何故障回退本地
        logger.warning("MCP 工具初始化失败（%s），回退本地工具", exc)
    _tools_cache = _local_tools()
    _mode = "local"
    logger.info("Agent 工具模式：本地实现（%d 工具）", len(_tools_cache))
    return _tools_cache


def current_mode() -> str:
    return _mode


def reset_tools_cache() -> None:
    """测试/刷新后重置缓存。"""
    global _tools_cache, _mode
    _tools_cache = None
    _mode = "local"