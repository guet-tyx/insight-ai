"""knowledge API 错误分支（不依赖 Milvus/Key，允许任何环境运行）。

覆盖知识库路由的文件校验 / 404 / Key 缺失 503 —— 这些分支与基础设施无关。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_upload_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/knowledge/documents").status_code == 401
    assert client.post("/api/v1/knowledge/query", json={"query": "x"}).status_code == 401


def test_upload_too_large_413(client: TestClient, auth_headers: dict[str, str]) -> None:
    big = b"x" * (20 * 1024 * 1024 + 1)
    resp = client.post(
        "/api/v1/knowledge/documents/upload",
        headers=auth_headers,
        files={"file": ("big.pdf", big, "application/pdf")},
    )
    assert resp.status_code == 413
    assert "20MB" in resp.json()["detail"]


def test_upload_empty_file_422(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/knowledge/documents/upload",
        headers=auth_headers,
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert resp.status_code == 422
    assert "为空" in resp.json()["detail"]


def test_get_document_unknown_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/knowledge/documents/ghost", headers=auth_headers)
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


def test_query_without_embedding_key_503(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "siliconflow_api_key", "")
    resp = client.post(
        "/api/v1/knowledge/query", headers=auth_headers, json={"query": "x"},
    )
    assert resp.status_code == 503
    assert "Embedding" in resp.json()["detail"]


def test_query_without_llm_key_503(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "")
    resp = client.post(
        "/api/v1/knowledge/query", headers=auth_headers, json={"query": "x"},
    )
    assert resp.status_code == 503
    assert "LLM" in resp.json()["detail"]