"""Neo4j EXPLAIN 索引命中验证（W10 压测报告支撑）。

对 graph_search 使用的三个查询模式执行 EXPLAIN，确认走索引：
  1. 节点属性索引：MATCH (n:Entity) WHERE n.name IN $names
  2. 全文索引：CALL db.index.fulltext.queryNodes('graph_entity_names', ...)
  3. 关系跳数遍历：MATCH p = (n)-[:REL*1..2]-(m)

注：neo4j 驱动 6.2 中 EXPLAIN/PROFILE 的结果在 consume().plan（Summary.plan），
结果流本身不返回 plan 记录。

用法：uv run python scripts/verify_neo4j_indexes.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.graph_service import get_driver  # noqa: E402


def _walk(plan: dict, depth: int = 0) -> list[str]:
    """递归提取运算符链（含索引名/过程详情）。"""
    args = plan.get("args", {})
    idx = args.get("IndexName", "")
    details = args.get("Details", "")
    label = plan.get("operatorType", "") + (f"[{idx}]" if idx else "") \
        + (f"[{details}]" if details else "")
    lines = ["  " * depth + label]
    for child in plan.get("children", []):
        lines += _walk(child, depth + 1)
    return lines


def _explain(session, cypher: str, **params) -> list[str]:
    """执行 EXPLAIN（不真正运行），返回运算符链文本。"""
    res = session.run(f"EXPLAIN {cypher}", **params)
    for _ in res:
        pass  # 消费结果流（EXPLAIN 无数据行）
    plan = res.consume().plan
    if plan is None:
        return ["（无计划返回）"]
    return _walk(plan)


def main() -> None:
    driver = get_driver()
    with driver.session() as s:
        # 0) 现有索引清单
        idx = s.run(
            "SHOW INDEXES YIELD name, type, labelsOrTypes, properties"
        ).data()
        print("===== 现有索引 =====")
        for i in idx:
            print(f"  {i['name']:<28} {i['type']:<12} {i['labelsOrTypes']} {i['properties']}")

        # 1) 属性索引命中：name IN $names（graph_search 定位核心实体）
        ops = _explain(s, """
            MATCH (n:Entity)
            WHERE n.name IN $names
            RETURN n LIMIT 5
        """, names=["测试实体"])
        hit = any("NodeIndexSeek" in o or "NodeIndexScan" in o for o in ops)
        print("\n===== 1. name IN 属性索引 =====")
        print("\n".join(ops))
        print(f"判定：{'✓ 走索引' if hit else '✗ 未走索引（全表扫描）'}")

        # 2) 全文索引命中
        try:
            ops = _explain(s, """
                CALL db.index.fulltext.queryNodes('graph_entity_names', $q)
                YIELD node, score RETURN node LIMIT 5
            """, q="测试")
            # 全文索引用过程调用实现：计划根为 ProcedureCall（Details 含过程名）
            hit = any("Fulltext" in o for o in ops) or any(
                "db.index.fulltext" in o for o in ops
            )
            print("\n===== 2. 全文索引（graph_entity_names）=====")
            print("\n".join(ops))
            print(f"判定：{'✓ 走全文索引' if hit else '✗ 未命中全文索引'}")
        except Exception as exc:  # noqa: BLE001 — 索引不存在等
            print(f"\n===== 2. 全文索引 =====\n✗ 不可用：{exc}")

        # 3) 1-2 跳路径遍历（从索引定位的节点出发，graph_search 主查询）
        ops = _explain(s, """
            MATCH (n:Entity) WHERE n.name IN $names
            CALL (n) {
                MATCH p = (n)-[:REL*1..2]-(m:Entity)
                WHERE n <> m
                RETURN p AS path
                LIMIT 30
            }
            RETURN path LIMIT 30
        """, names=["测试实体"])
        hit = any("NodeIndexSeek" in o or "NodeIndexScan" in o for o in ops)
        print("\n===== 3. REL 1-2 跳路径 =====")
        print("\n".join(ops))
        print(f"判定：{'✓ 起点走索引' if hit else '✗ 起点未走索引'}")
    driver.close()


if __name__ == "__main__":
    main()
