"""Neo4j 图服务：索引保障 / 参数化批量写入 / 1-2 跳拓扑查询（W7 GraphRAG）。

- 写入：MERGE（去重）+ 参数化 UNWIND 批量（防注入），关系携带 doc_id/evidence
- 查询：LLM 提取查询核心实体 → 全文索引定位 → 1-2 跳路径 → 自然语言段落
- 空图/未命中返回 []（上游 RRF 自动退化为纯向量）
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from neo4j import GraphDatabase

from app.core.config import settings
from app.services.entity_extraction import NODE_TYPES, REL_TYPES, EntityExtraction

logger = logging.getLogger(__name__)

MAX_PATH_ROWS = 30  # 拓扑路径返回上限（防路径爆炸）

# W10 压测优化：实体提取走 lite LLM（~2-17s 且易限流），同查询重复检索
# 场景缓存提取结果（TTL 5 分钟），避免热点查询反复打 LLM。
_EXTRACT_CACHE_TTL = 300.0
_extract_cache: dict[str, tuple[float, list[str]]] = {}


_driver = None  # 模块级单例：Neo4j driver 线程安全，复用连接池避免每次握手


def get_driver():
    """获取共享 driver（惰性创建；线程安全连接池，官方推荐单例复用）。

    W10 压测发现：每请求新建 driver 含 TCP+Bolt 握手（~1s），改为复用后
    查询链路显著下降。调用方不要 close；进程退出由 close_driver() 收尾。
    """
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


def close_driver() -> None:
    """显式关闭共享 driver（测试收尾/进程退出用）。"""
    global _driver
    if _driver is not None:
        try:
            _driver.close()
        finally:
            _driver = None


def ensure_graph_schema() -> None:
    """幂等建索引（计划风险表：对常用查询路径的节点 Attribute 建立索引）。"""
    with get_driver().session() as s:
        s.run("CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)")
        s.run("CREATE INDEX entity_type IF NOT EXISTS FOR (n:Entity) ON (n.type)")
        # 实体名全文索引：支持别名/模糊匹配定位
        try:
            s.run(
                "CREATE FULLTEXT INDEX graph_entity_names IF NOT EXISTS "
                "FOR (n:Entity) ON EACH [n.name]"
            )
        except Exception as exc:  # noqa: BLE001 — 旧版本语法差异不阻断
            logger.warning("全文索引创建失败（不影响基础查询）：%s", exc)


def write_triples(doc_id: str, extraction: EntityExtraction) -> tuple[int, int]:
    """参数化批量 MERGE 写入三元组；返回 (实体数, 关系数)。

    - 节点：MERGE on (name, type)；属性 doc_id（来源文档）、created_at
    - 关系：MERGE on (source.name, type, target.name)，属性 doc_id + evidence
    - 实体名/类型全部走参数（黑名单校验），杜绝 Cypher 注入
    """
    if not extraction.entities and not extraction.relations:
        return 0, 0
    names = {e.name for e in extraction.entities}
    rels = [
        r for r in extraction.relations
        if r.source in names and r.target in names and r.type in REL_TYPES
    ]
    with get_driver().session() as s:
        if extraction.entities:
            s.run(
                "UNWIND $rows AS row "
                "MERGE (n:Entity {name: row.name}) "
                "SET n.type = row.type, "
                "n.doc_id = coalesce(n.doc_id, row.doc_id)",  # 保留首发来源
                rows=[
                    {"name": e.name, "type": e.type, "doc_id": doc_id}
                    for e in extraction.entities
                ],
            )
        if rels:
            s.run(
                "UNWIND $rows AS row "
                "MATCH (a:Entity {name: row.source}), (b:Entity {name: row.target}) "
                "MERGE (a)-[r:REL {type: row.type}]->(b) "
                "SET r.doc_id = coalesce(r.doc_id, row.doc_id), r.evidence = row.evidence",
                rows=[
                    {
                        "source": r.source, "target": r.target,
                        "type": r.type, "doc_id": doc_id, "evidence": r.evidence,
                    }
                    for r in rels
                ],
            )
    return len(extraction.entities), len(rels)


def _extract_query_entities(query: str) -> list[str]:
    """从查询中提取核心实体名（用于图定位）；失败时返回 []。

    普通 chat 输出（无结构化约束）→ 使用 lite 模型，降低主模型配额压力；
    结果带 TTL 缓存（同查询 5 分钟内复用，压测/热查询显著降延迟）。
    """
    now = time.monotonic()
    hit = _extract_cache.get(query)
    if hit and now - hit[0] < _EXTRACT_CACHE_TTL:
        return hit[1]

    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=settings.llm_model_lite,
        api_key=settings.openai_api_key,
        base_url=settings.llm_base_url,
        temperature=0,
        max_retries=3,
        request_timeout=60,
    )
    resp = llm.invoke(
        [
            SystemMessage(content="从用户查询中提取专有名词实体（公司名/技术名/人名/事件名/产品名）。"
                                  "不提取泛指词如：平台、报告、白皮书、系统、技术。"
                                  "每行输出一个实体名，不要任何标点；没有则输出「无」。"),
            HumanMessage(content=query),
        ]
    )
    # 兼容中英文逗号/顿号/换行分隔（lite 模型偶尔混用中文标点）
    names = [
        n.strip() for n in re.split(r"[,，、;；\n]+", str(resp.content))
        if n.strip() and n.strip() != "无"
    ]
    _extract_cache[query] = (now, names)
    logger.info("查询实体抽取: %s", names)
    return names[:3]


def graph_search(query: str, max_hops: int = 2) -> list[dict[str, Any]]:
    """图谱拓扑查询：定位查询实体 → 1-2 跳路径 → 自然语言段落列表。

    返回 [{text, doc_id, score(跳数倒数，融合用), path}]；
    未命中/空图返回 []。
    """
    entities = _extract_query_entities(query)
    if not entities:
        return []
    with get_driver().session() as s:
        rows = s.run(
            f"""
            MATCH (n:Entity)
            WHERE n.name IN $names
            CALL (n) {{
                MATCH p = (n)-[:REL*1..{max_hops}]-(m:Entity)
                WHERE n <> m
                RETURN p AS path, [r IN relationships(p) | r.type] AS rel_types
                LIMIT {MAX_PATH_ROWS}
            }}
            RETURN path, rel_types
            LIMIT {MAX_PATH_ROWS}
            """,
            names=entities,
        ).data()

    texts = []
    for row in rows:
        path = row["path"]
        nodes, _ = _parse_path(path)
        # 关系类型优先取 rel_types（属性：DEVELOPED 等），回退扁平列表中的标签
        rel_types = row.get("rel_types") or _parse_path(path)[1]
        hops = len(rel_types)
        # 路径 → "A -[DEVELOPED]-> B" 自然语言描述
        segs = []
        prev = nodes[0].get("name", "")
        for rel_type, node in zip(rel_types, nodes[1:], strict=False):
            segs.append(f"{prev}--[{rel_type}]-->{node.get('name')}")
            prev = node.get("name", "")
        text = " 且 ".join(segs)
        texts.append({
            "text": text,
            "doc_id": nodes[0].get("doc_id", "") or nodes[-1].get("doc_id", ""),
            "score": 1.0 / (hops + 1),  # 跳数越少相关度越高（融合排名用）
            "path": segs,
        })
    logger.info("图谱查询命中 %d 条路径（查询=%s）", len(texts), query[:40])
    return texts


def _parse_path(path) -> tuple[list[dict], list[str]]:
    """统一解析驱动的路径返回（兼容两种形态）。

    - 旧形态（Path 对象 / dict）：path['nodes'] + path['relationships']
    - 驱动 6.2 扁平形态：list —— 节点与关系类型交替
      [node, 'REL', node, 'REL', node]
    """
    if isinstance(path, dict):
        nodes = path.get("nodes", [])
        rels = path.get("relationships", [])
        return nodes, [r.get("type", "") for r in rels]
    if isinstance(path, list):
        # 扁平交替：节点/关系类型/节点/...
        nodes = [path[i] for i in range(0, len(path), 2)]
        rel_types = [str(path[i]) for i in range(1, len(path), 2)]
        return nodes, rel_types
    # Path 对象兼容
    rels = getattr(path, "relationships", [])
    nodes = getattr(path, "nodes", [])
    return list(nodes), [r.get("type", "") if isinstance(r, dict) else getattr(r, "type", "") for r in rels]


# 供测试复位（避免重复建索引覆盖既有数据）
def count_graph() -> int:
    """图内节点总数（验证/测试用）。"""
    with get_driver().session() as s:
        return s.run("MATCH (n:Entity) RETURN count(n) AS c").single()["c"]