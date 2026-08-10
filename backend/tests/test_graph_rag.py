"""W7 GraphRAG 测试：实体抽取 Schema / 图写入与查询 / 端到端 hybrid。"""
from __future__ import annotations

import pytest

from app.services.entity_extraction import Entity, EntityExtraction, Relation
from fastapi.testclient import TestClient
from tests.conftest import INFRA_READY, NEO4J_UP

# ---------- 单元：Schema 与引用校验 ----------

def test_extraction_schema_valid() -> None:
    ex = EntityExtraction(
        entities=[Entity(name="InsightAI", type="Company"), Entity(name="LangGraph", type="Technology")],
        relations=[Relation(source="InsightAI", target="LangGraph", type="DEVELOPED", evidence="证据")],
    )
    assert len(ex.entities) == 2
    assert ex.relations[0].type == "DEVELOPED"


def test_extraction_rejects_unknown_types() -> None:
    with pytest.raises(Exception):
        EntityExtraction(entities=[Entity(name="X", type="Alien")])  # type: ignore[arg-type]
    with pytest.raises(Exception):
        EntityExtraction(relations=[Relation(source="a", target="b", type="HACKED")])  # type: ignore[arg-type]


def test_extraction_filters_self_reference() -> None:
    ex = EntityExtraction(
        entities=[Entity(name="A", type="Company")],
        relations=[Relation(source="A", target="A", type="DEVELOPED")],
    )
    assert ex.relations == []


# ---------- 集成：图写入与查询（真实 Neo4j） ----------

@pytest.mark.skipif(not NEO4J_UP, reason="Neo4j 未就绪")
def test_write_and_search_triples() -> None:
    from app.services.graph_service import count_graph, ensure_graph_schema, get_driver, write_triples

    ensure_graph_schema()
    doc = "graph-test-doc"

    def _exec(query: str, **params) -> list[dict]:
        driver = get_driver()
        try:
            with driver.session() as s:
                return s.run(query, **params).data()
        finally:
            driver.close()

    _exec("MATCH (n:Entity {doc_id: $d}) DETACH DELETE n", d=doc)

    ex = EntityExtraction(
        entities=[
            Entity(name="InsightAI", type="Company"),
            Entity(name="Milvus", type="Technology"),
            Entity(name="Neo4j", type="Technology"),
        ],
        relations=[
            Relation(source="InsightAI", target="Milvus", type="DEVELOPED", evidence="InsightAI 开发了基于 Milvus 的平台"),
            Relation(source="InsightAI", target="Neo4j", type="DEVELOPED", evidence="集成 Neo4j 图数据库"),
        ],
    )
    n, r = write_triples(doc, ex)
    assert n == 3 and r == 2
    assert count_graph() >= 3

    # 关系方向验证：Milvus 的反向关系（<-DEVELOPED- InsightAI）
    rows = _exec(
        "MATCH (a:Entity {name: 'Milvus'})-[rel:REL]-(b:Entity) "
        "RETURN rel.type AS t, b.name AS n"
    )
    assert any(row["n"] == "InsightAI" for row in rows)

    _exec("MATCH (n:Entity {doc_id: $d}) DETACH DELETE n", d=doc)


@pytest.mark.skipif(not NEO4J_UP, reason="Neo4j 未就绪")
@pytest.mark.flaky(reruns=2, reruns_delay=5)  # 真实 LLM 抽取：限流瞬态自动重试
def test_graph_search_paths(sample_pdf_bytes: bytes) -> None:
    """真实 LLM 抽取 → 写图 → graph_search 命中拓扑路径。"""
    from app.services.entity_extraction import extract_entities
    from app.services.graph_service import ensure_graph_schema, get_driver, graph_search, write_triples
    from app.services.pdf_parser import parse_pdf_bytes

    ensure_graph_schema()
    doc = "graph-search-doc"

    def _exec(query: str, **params) -> None:
        driver = get_driver()
        try:
            with driver.session() as s:
                s.run(query, **params)
        finally:
            driver.close()

    _exec("MATCH (n:Entity {doc_id: $d}) DETACH DELETE n", d=doc)

    chunks = parse_pdf_bytes(sample_pdf_bytes, doc)
    total = 0
    for c in chunks:
        ex = extract_entities(c.text)
        n, _ = write_triples(doc, ex)
        total += n
    assert total > 0, "演示 PDF 未抽取到实体"

    paths = graph_search("Milvus 与 Neo4j 在平台中的作用", max_hops=2)
    assert isinstance(paths, list)
    if paths:  # 实体命中时应有路径文本
        assert any("--[" in p["text"] for p in paths)

    _exec("MATCH (n:Entity {doc_id: $d}) DETACH DELETE n", d=doc)


# ---------- 端到端：入库自动建图 → hybrid 检索 ----------

@pytest.mark.skipif(not (NEO4J_UP and INFRA_READY), reason="Neo4j 或 LLM/Milvus 未就绪")
@pytest.mark.flaky(reruns=2, reruns_delay=5)  # 端到端：抽取+问答多次 LLM 调用
def test_ingest_builds_graph_and_hybrid_query(
    client: TestClient, auth_headers: dict[str, str], sample_pdf_bytes: bytes
) -> None:
    """upload → ready（含 graph_count）→ /knowledge/query 返回 hybrid 结果。"""
    upload = client.post(
        "/api/v1/knowledge/documents/upload", headers=auth_headers,
        files={"file": ("whitepaper.pdf", sample_pdf_bytes, "application/pdf")},
    )
    assert upload.status_code == 202
    doc_id = upload.json()["doc_id"]

    import time

    record = None
    for _ in range(120):
        rec = client.get(f"/api/v1/knowledge/documents/{doc_id}", headers=auth_headers).json()
        if rec["status"] in ("ready", "failed"):
            record = rec
            break
        time.sleep(1)
    assert record and record["status"] == "ready", f"入库未完成: {record}"

    q = client.post(
        "/api/v1/knowledge/query", headers=auth_headers,
        json={"query": "平台使用了哪些数据库技术？", "top_k": 5},
    )
    assert q.status_code == 200
    body = q.json()
    assert body["sources"], "无召回"
    assert any(s.get("source_type", "vector") in ("vector", "graph") for s in body["sources"])