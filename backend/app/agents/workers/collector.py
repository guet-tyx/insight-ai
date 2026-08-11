"""Collector 专家子图：多工具智能路由（RSS / 静态抓取 / 浏览器） + 强类型结构化提取（W6）。

路由策略（对应计划「RSS API 与 Browser Use 智能路由切换」）：
  1. fetch_rss      — RSS/Atom 快速解析（feedparser，快、无反爬压力，首选）
  2. fetch_static   — 静态页 httpx 抓取（轻量；JS 渲染/缺内容时换浏览器）
  3. collect_webpage— browser-use 动态采集（兜底/复杂页）
LLM（ReAct）决策 + 系统提示词引导 RSS 特征优先；工具失败自动降级下一种
= 计划风险表「DOM 变化导致抽取失败：回退机制改为 LLM 纯文本重组」落地。

私有状态（CollectorState）：task_requirement / url / raw_artifacts /
retry_count / browser_payload / source_type —— 不进入父图 checkpoint。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

from app.agents.state import CollectorState
from app.core.config import settings
from app.services.collector_service import collect as run_collect
from app.services.rss_service import fetch_rss, looks_like_rss_url

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"(https?://)?(?:[\w-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:[/?#][^\s，。;；]*)?")
ALLOW_INTERNAL = settings.collector_allow_internal

COLLECTOR_PROMPT = """你是数据采集专家（Collector Agent）。根据任务指令选择最合适的采集工具：

1. fetch_rss：目标 URL 是 RSS/Atom 提要（路径含 feed/rss/.xml，或指令含"订阅源/feed"）时首选，
   解析快且结构化（返回 title/link/published/summary 列表）。
2. fetch_static：普通静态网页（内容不依赖 JavaScript 渲染）时使用，轻量快速。
3. collect_webpage：动态渲染/复杂网页，或 fetch_static 内容不足时使用；约需 20-60 秒。

规则：优先低成本工具；一个工具失败或内容不足时换下一个（自动降级）；
⚠️ 若 fetch_static 返回"反爬拦截/安全验证/401/403/429"错误，**必须立即改用
collect_webpage（真实浏览器）重试**，不要放弃采集；
若任务未给出 URL，直接说明缺失信息。结果保持事实，不要编造。"""


async def _fetch_static_impl(url: str) -> str:
    """静态页抓取：httpx 拉取，去标签保留正文文本。

    反爬识别：403/4xx 或验证页特征 → 抛出明确信号，促使 LLM 回退浏览器。
    """
    import httpx
    import re as _re

    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 InsightAI/1.0"}) as client:
        resp = await client.get(url)
        if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
            raise ValueError(
                f"静态抓取被目标站点反爬拦截（HTTP {resp.status_code}），"
                "该站点需要真实浏览器渲染/通过反爬：请改用 collect_webpage 工具"
            )
        resp.raise_for_status()
        html = resp.text
    # 验证页特征：极短页面且含验证关键词
    if len(html) < 200 and ("验证" in html or "captcha" in html.lower() or "安全验证" in html):
        raise ValueError("命中站点安全验证页，请改用 collect_webpage 浏览器采集")
    # 简单正文提取：去 script/style/标签，折叠空白
    text = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = _re.sub(r"(?s)<[^>]+>", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    if len(text) < 50:
        raise ValueError(f"静态抓取内容过少（{len(text)} 字符），可能需要浏览器渲染")
    return text[:4000]


@tool
async def fetch_rss_tool(feed_url: str) -> str:
    """解析 RSS/Atom 订阅源，返回结构化条目（title/link/published/summary）。

    目标为订阅源（feed/rss/.xml）时首选本工具，速度快且不耗浏览器。
    """
    extract = await fetch_rss(feed_url)
    return json.dumps(extract.model_dump(), ensure_ascii=False)


@tool
async def fetch_static(url: str) -> str:
    """抓取静态网页正文文本（轻量快速，不执行 JavaScript）。

    页面依赖 JS 渲染或内容不足时，改用 collect_webpage。
    """
    return await _fetch_static_impl(url)


@tool
async def collect_webpage(url: str, instruction: str) -> str:
    """采集动态/复杂网页（真实浏览器执行，约 20-60 秒）。"""
    # 强制 web 路径（避免工具层二次特征路由，路由决策由 Agent 完成）
    data = await run_collect(url, instruction, allow_internal=ALLOW_INTERNAL, source="web")
    return json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)


def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.llm_base_url,
        temperature=0,
    )


def _extract_url(task: str) -> str:
    """从任务指令中提取 URL（支持裸域名，自动补 https://）。"""
    m = URL_PATTERN.search(task)
    if not m:
        return ""
    url = m.group(0).rstrip(".,)")
    return url if url.startswith("http") else f"https://{url}"


async def collector_node(state: CollectorState) -> dict[str, Any]:
    """ReAct 多工具路由采集；结果归一进 raw_artifacts（含 source_type）。"""
    task = (state.get("task_requirement") or "").strip()
    url = state.get("url") or _extract_url(task)
    if not url:
        return {"raw_artifacts": [{"task": task, "error": "任务未提供 URL，无法采集"}]}

    # RSS 特征 → 系统提示词引导（LLM 仍可自主降级）
    prompt = COLLECTOR_PROMPT
    if looks_like_rss_url(url) or "订阅" in task or "feed" in task.lower():
        prompt += "\n⚠️ 目标明显是 RSS/Atom 订阅源：必须首选 fetch_rss_tool。"

    agent = create_react_agent(_llm(), tools=[fetch_rss_tool, fetch_static, collect_webpage],
                               prompt=prompt)
    artifact: dict[str, Any] = {"task": task, "url": url}
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": task}]})
        answer = "".join(
            (m.content or "") for m in result.get("messages", [])
            if getattr(m, "type", "") == "ai" and not getattr(m, "tool_calls", None) and m.content
        ) or "采集完成但未产出文本"
        artifact["data"] = answer[:8000]
        # 规则兜底：LLM 路由结果疑似反爬失败 → 强制浏览器补偿采集一次
        if any(k in answer for k in ("反爬", "403", "429", "安全验证", "无法访问", "HTTPStatusError")):
            logger.warning("ReAct 路由疑似反爬失败，强制浏览器补偿采集：%s", url)
            fallback = await run_collect(url, task, allow_internal=ALLOW_INTERNAL, source="web")
            artifact["data"] = (
                json.dumps(fallback, ensure_ascii=False) if isinstance(fallback, dict) else str(fallback)
            )[:8000]
            artifact["fallback"] = "browser"
    except Exception as exc:  # noqa: BLE001 — 采集失败转为产物字段（不打断主图）
        artifact["error"] = f"{type(exc).__name__}: {exc}"[:500]
        # 异常兜底：LLM 链路整体失败时也尝试浏览器补偿
        try:
            fallback = await run_collect(url, task, allow_internal=ALLOW_INTERNAL, source="web")
            artifact["data"] = (
                json.dumps(fallback, ensure_ascii=False) if isinstance(fallback, dict) else str(fallback)
            )[:8000]
            artifact["fallback"] = "browser"
            artifact.pop("error", None)
        except Exception as exc2:  # noqa: BLE001
            artifact["error"] = f"{type(exc).__name__}: {exc}"[:500]
    artifact["source_type"] = "rss" if looks_like_rss_url(url) else "web"
    return {
        "raw_artifacts": [artifact],
        "retry_count": (state.get("retry_count") or 0) + 1,
        "source_type": artifact["source_type"],
    }


def build_collector_subgraph():
    """构建 Collector 子图（私有状态 CollectorState，ReAct 多工具节点）。"""
    workflow = StateGraph(CollectorState)
    workflow.add_node("collect", collector_node)
    workflow.add_edge(START, "collect")
    workflow.add_edge("collect", END)
    return workflow.compile()