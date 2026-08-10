"""RSS/Atom 提要解析服务（feedparser 封装）。

- 支持 RSS 2.0 / Atom 等常见格式（feedparser 自动识别）
- 条目规范化：title / link / published / summary / source_url
- 解析失败或空提要抛出 ValueError（由调用方转为采集失败记录）
"""
from __future__ import annotations

import logging
from typing import Any

import feedparser
import httpx

from app.schemas.collect import RssExtract, RssItem

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 2000
FETCH_TIMEOUT = 15.0


def _normalize_time(value: Any) -> str:
    """把 feedparser 的发布时间结构规范化为 ISO 字符串（无则空）。"""
    try:
        published = value.get("published_parsed") or value.get("updated_parsed")
        if published:
            import time as _time

            return _time.strftime("%Y-%m-%dT%H:%M:%S", published)
    except Exception:  # noqa: BLE001 — 时间解析失败不阻断
        pass
    return ""


async def fetch_rss(feed_url: str) -> RssExtract:
    """抓取并解析 RSS/Atom 提要；返回规范化条目集合。

    失败场景（抛出 ValueError，调用方捕获转为采集错误记录）：
    - 网络/超时；非 XML/非法提要（feedparser 无法识别）
    - 无任何条目（空提要）
    """
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(feed_url)
        resp.raise_for_status()
        raw = resp.text

    parsed = feedparser.parse(raw)
    if parsed.get("bozo") and not parsed.entries:
        bozo_exc = parsed.get("bozo_exception")
        raise ValueError(f"非法 RSS/Atom 提要：{bozo_exc}")

    feed_title = str(parsed.feed.get("title", "")) if parsed.feed else ""
    items = [
        RssItem(
            title=(e.get("title") or "").strip(),
            link=(e.get("link") or "").strip(),
            published=_normalize_time(e),
            summary=(e.get("summary") or "")[:MAX_SUMMARY_CHARS],
            source_url=feed_url,
        )
        for e in parsed.entries
    ]
    if not items:
        raise ValueError("提要不含任何条目")
    logger.info("RSS 解析成功 feed=%s 条目=%d（%s）", feed_url, len(items), feed_title)
    return RssExtract(items=items, feed_title=feed_title)


def looks_like_rss_url(url: str) -> bool:
    """RSS 特征识别（Collector 路由用）：路径含 feed/rss/.xml 或指令词。"""
    lower = url.lower()
    path = lower.split("?")[0]
    return (
        path.endswith(".xml")
        or "/feed" in path
        or "/rss" in path
        or path.endswith("/feed/")
        or "atom" in path
    )