"""W6 RSS 服务与路由单元测试。"""
from __future__ import annotations

import asyncio

import pytest

from app.services.rss_service import fetch_rss, looks_like_rss_url


def test_looks_like_rss_url() -> None:
    assert looks_like_rss_url("https://news.example.com/feed")
    assert looks_like_rss_url("https://news.example.com/rss")
    assert looks_like_rss_url("https://a.com/feed.xml")
    assert looks_like_rss_url("http://x.com/atom.xml")
    assert not looks_like_rss_url("https://example.com/article/123")
    assert not looks_like_rss_url("https://example.com/")


def test_fetch_rss_structured(local_rss_feed: str) -> None:
    extract = asyncio.run(fetch_rss(local_rss_feed))
    assert extract.feed_title == "Insight AI 情报速递"
    assert len(extract.items) == 3
    first = extract.items[0]
    assert first.title == "LangGraph 发布多智能体增强版"
    assert first.link.startswith("https://example.com/news/")
    assert first.published.startswith("2026-08-10")  # 规范化 ISO 时间
    assert "检查点" in first.summary
    assert first.source_url == local_rss_feed


def test_fetch_rss_invalid_feed(local_test_page: str) -> None:
    """非 RSS 内容（HTML 页面）→ 明确错误。"""
    with pytest.raises(ValueError, match="RSS|非法|Atom"):
        asyncio.run(fetch_rss(local_test_page))


def test_fetch_rss_unreachable() -> None:
    """网络不可达 → 连接类异常向上抛（由调用方转为采集失败记录）。"""
    with pytest.raises(Exception):
        asyncio.run(fetch_rss("http://127.0.0.1:1/feed.xml"))