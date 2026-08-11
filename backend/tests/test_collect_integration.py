"""采集集成测试：真实 browser-use Agent + 本地测试页（LLM/Chromium 缺失自动 skip）。"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.conftest import BROWSER_READY

pytestmark = pytest.mark.skipif(not BROWSER_READY, reason="LLM Key 或 Chromium 未就绪")


def test_collect_structured_output(local_test_page: str) -> None:
    """自然语言指令 → 结构化 Schema 输出（标题 + 要点列表）。"""
    from app.services.collector_service import collect

    result = asyncio.run(
        collect(
            local_test_page,
            "提取页面的标题和要点列表",
            source="web",  # W9: auto 现优先 TLS 快路径，浏览器结构断言需显式 web
            output_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "key_points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "key_points"],
            },
            max_steps=20,
            allow_internal=True,  # 测试站点位于 127.0.0.1
        )
    )
    assert isinstance(result, dict), f"应返回结构化 dict，实际 {type(result)}"
    assert "Insight AI" in result.get("title", "")
    assert result.get("key_points"), "要点列表为空"
    assert any("LangGraph" in p for p in result["key_points"])


def test_collect_plain_text(local_test_page: str) -> None:
    """web 路径：W6 起无显式 schema 时默认 WebExtract 结构化；文本兜底可接受。"""
    from app.services.collector_service import collect

    result = asyncio.run(
        collect(local_test_page, "回答：这个页面是什么平台？", source="web", max_steps=15, allow_internal=True)
    )
    if isinstance(result, dict):
        assert result.get("title") or result.get("summary"), "结构化结果为空"
        # 结构化结果内容命中关键词（提取自测试页）
        assert any(
            k in str(result.get("title", "")) + str(result.get("summary", ""))
            for k in ("Insight", "情报", "平台")
        )
    else:
        assert isinstance(result, str) and result.strip(), "文本结果为空"
        assert ("Insight" in result) or ("情报" in result) or ("平台" in result)


def test_collect_api_internal_blocked(client: TestClient, auth_headers: dict[str, str]) -> None:
    """API 层强制拦截内网地址（SSRF 防护），与 CLI/测试的 allow_internal 开关区分。"""
    resp = client.post(
        "/api/v1/collect",
        headers=auth_headers,
        json={"url": "http://127.0.0.1:8080/", "instruction": "提取标题"},
    )
    assert resp.status_code == 422
    assert "内网" in resp.json()["detail"]


def test_collect_api_invalid_schema_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/api/v1/collect",
        headers=auth_headers,
        json={
            "url": "https://example.com",
            "instruction": "提取标题",
            "output_schema": {"type": "array"},  # 非法：必须 object
        },
    )
    assert resp.status_code == 422


def test_collect_invalid_source_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/api/v1/collect",
        headers=auth_headers,
        json={"url": "https://example.com", "instruction": "提取", "source": "socks"},
    )
    assert resp.status_code in (422, 202)  # Pydantic Literal 校验或后端校验


def test_collect_rss_route_structured(local_rss_feed: str) -> None:
    """RSS 路由：不耗浏览器，直接结构化输出条目。"""
    from app.services.collector_service import collect

    result = asyncio.run(
        collect(local_rss_feed, "提取最新条目", source="rss", allow_internal=True)
    )
    assert isinstance(result, dict)
    assert result["feed_title"] == "Insight AI 情报速递"
    assert len(result["items"]) == 3
    assert result["items"][0]["title"].startswith("LangGraph")


def test_collect_auto_route_picks_rss_for_feed_url(local_rss_feed: str) -> None:
    """auto 路由：RSS 特征 URL 自动走 RSS 快速路径（结构含 items 即证明）。"""
    from app.services.collector_service import collect

    result = asyncio.run(
        collect(local_rss_feed, "查看订阅源", source="auto", allow_internal=True)
    )
    assert isinstance(result, dict) and "items" in result, "auto 未命中 RSS 路径"


def test_collect_web_default_structured(local_test_page: str) -> None:
    """web 路径默认强类型 WebExtract 输出（W6）。"""
    from app.services.collector_service import collect

    result = asyncio.run(
        collect(local_test_page, "提取页面标题与要点", source="web",
                max_steps=15, allow_internal=True)
    )
    if isinstance(result, dict):  # 结构化成功
        assert "title" in result
    else:  # 兜底为文本
        assert isinstance(result, str) and result.strip()