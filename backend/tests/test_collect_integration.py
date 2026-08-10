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
    """无 Schema 时返回纯文本结果。"""
    from app.services.collector_service import collect

    result = asyncio.run(
        collect(local_test_page, "回答：这个页面是什么平台？", max_steps=15, allow_internal=True)
    )
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