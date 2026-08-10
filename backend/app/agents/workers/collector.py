"""Collector 专家子图：网页采集（browser-use），私有状态隔离。

子图输入：task_requirement（Supervisor 拆解的任务指令，含 URL）
子图输出：raw_artifacts（采集结果，映射进 GlobalState）
私有字段（retry_count/browser_payload）不进入父图 checkpoint。
W5 为单步采集（任务含 URL 即采）；W6 深化为多步 ReAct + RSS 路由。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.state import CollectorState
from app.core.config import settings
from app.services.collector_service import collect as run_collect

logger = logging.getLogger(__name__)

# 支持带协议或裸域名（Supervisor 子任务常用 "example.com" 写法）
URL_PATTERN = re.compile(r"(https?://)?(?:[\w-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:[/?#][^\s，。;；]*)?")

# 本地演示开关（.env COLLECTOR_ALLOW_INTERNAL=true）；默认严格禁内网（SSRF 防护）
ALLOW_INTERNAL = settings.collector_allow_internal


def _extract_url(task: str) -> str:
    """从任务指令中提取 URL（支持 http(s):// 前缀或裸域名，自动补协议）。"""
    m = URL_PATTERN.search(task)
    if not m:
        return ""
    url = m.group(0).rstrip(".,)")
    return url if url.startswith("http") else f"https://{url}"


async def collector_node(state: CollectorState) -> dict[str, Any]:
    """执行一次网页采集；失败记录到产物（含 error 字段）并累加私有重试计数。

    async 节点：直接 await 采集协程（避免在异步图内嵌套 asyncio.run 导致
    事件循环冲突），失败转为产物字段而非向上抛。
    """
    task = (state.get("task_requirement") or "").strip()
    url = state.get("url") or _extract_url(task)
    if not url:
        return {"raw_artifacts": [{"task": task, "error": "任务未提供 URL，无法采集"}]}

    logger.info("Collector 采集: %s", url)
    artifact: dict[str, Any] = {"task": task, "url": url}
    try:
        data = await run_collect(url, task, allow_internal=ALLOW_INTERNAL)
        if isinstance(data, dict):
            artifact.update(data)
        else:
            artifact["data"] = str(data)
    except Exception as exc:  # noqa: BLE001 — 采集失败转为产物字段
        artifact["error"] = f"{type(exc).__name__}: {exc}"
    return {
        "raw_artifacts": [artifact],
        "retry_count": (state.get("retry_count") or 0) + 1,
    }


def build_collector_subgraph():
    """构建 Collector 子图（私有状态 CollectorState，单节点）。"""
    workflow = StateGraph(CollectorState)
    workflow.add_node("collect", collector_node)
    workflow.add_edge(START, "collect")
    workflow.add_edge("collect", END)
    return workflow.compile()