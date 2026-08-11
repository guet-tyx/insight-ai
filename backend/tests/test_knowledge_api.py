"""knowledge API 集成测试：鉴权 / 上传 / 状态轮询 / 向量问答。

pytestmark：依赖 Milvus 容器与 SiliconFlow/SenseNova Key，缺一自动 skip。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import INFRA_READY

pytestmark = pytest.mark.skipif(not INFRA_READY, reason="Milvus 或 API Key 未就绪")


def test_upload_requires_auth(client: TestClient) -> None:
    """未登录访问知识库接口一律 401。"""
    resp = client.get("/api/v1/knowledge/documents")
    assert resp.status_code == 401
    resp = client.post("/api/v1/knowledge/query", json={"query": "测试"})
    assert resp.status_code == 401


def test_upload_non_pdf_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/knowledge/documents/upload",
        headers=auth_headers,
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 422
    assert "仅支持 PDF" in resp.json()["detail"]


def _wait_ready(client: TestClient, headers: dict[str, str], doc_id: str, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/v1/knowledge/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in ("ready", "failed"):
            return body
        time.sleep(1)
    pytest.fail(f"轮询超时（{timeout}s），doc_id={doc_id}")


@pytest.mark.flaky(reruns=2, reruns_delay=5)  # 真实 LLM 问答：限流瞬态自动重试
def test_upload_list_query_full_flow(
    client: TestClient, auth_headers: dict[str, str], sample_pdf_bytes: bytes
) -> None:
    # 共享 Milvus 集合可能残留其它测试文档 → 先清空，保证 Top1 归属本用例（确定性）
    from app.services.ingest_service import COLLECTION, get_milvus_client

    mc = get_milvus_client()
    if mc.has_collection(COLLECTION):
        mc.delete(COLLECTION, filter='doc_id != ""')

    # 1) 上传 → 202 + doc_id
    upload = client.post(
        "/api/v1/knowledge/documents/upload",
        headers=auth_headers,
        files={"file": ("manual.pdf", sample_pdf_bytes, "application/pdf")},
    )
    assert upload.status_code == 202
    doc_id = upload.json()["doc_id"]

    # 2) 轮询处理状态
    record = _wait_ready(client, auth_headers, doc_id)
    assert record["status"] == "ready", f"入库失败：{record.get('error')}"
    assert record["chunk_count"] >= 3
    assert record["filename"] == "manual.pdf"

    # 3) 文档列表包含新文档
    docs = client.get("/api/v1/knowledge/documents", headers=auth_headers).json()
    assert any(d["doc_id"] == doc_id for d in docs)

    # 4) 向量问答：命中 + LLM 回答 + 溯源
    q = client.post(
        "/api/v1/knowledge/query",
        headers=auth_headers,
        json={"query": "Insight AI 的知识库数据存在哪里？", "top_k": 3},
    )
    assert q.status_code == 200
    body = q.json()
    assert body["answer"], "LLM 回答为空"
    assert body["sources"], "未召回任何片段"
    assert body["sources"][0]["doc_id"] == doc_id
    assert body["sources"][0]["score"] > 0
    assert body["sources"][0]["parent_header"], "溯源缺少标题"
    assert any("Milvus" in s["chunk_text"] for s in body["sources"])


def test_query_empty_knowledge_base(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """空知识库时给出明确提示而非报错（用例内先清空共享集合与图谱，保证确定性）。"""
    from app.services.graph_service import close_driver, get_driver
    from app.services.ingest_service import COLLECTION, get_milvus_client

    mc = get_milvus_client()
    if mc.has_collection(COLLECTION):
        mc.delete(COLLECTION, filter='doc_id != ""')
    # W7：图路径也是召回源，一并清空（测试环境）
    driver = get_driver()
    try:
        with driver.session() as s:
            s.run("MATCH (n:Entity) DETACH DELETE n")
    finally:
        close_driver()

    q = client.post(
        "/api/v1/knowledge/query",
        headers=auth_headers,
        json={"query": "完全不存在的内容 xyzabc", "top_k": 1},
    )
    assert q.status_code == 200
    body = q.json()
    assert "未检索到" in body["answer"] or "未找到" in body["answer"]
    assert body["sources"] == []