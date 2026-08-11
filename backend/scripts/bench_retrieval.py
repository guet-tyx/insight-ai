"""W10 检索层压测脚本（轻量 asyncio bench）：向量 + 图谱 + RRF 延迟分位数。

直接调用检索服务层（免 LLM 问答，聚焦检索管道）：
  - total：search() 真实总延迟（W10 优化后向量/图谱并行）
  - 可选 --profile：单次串行阶段拆解（embed / milvus / graph / rrf 占比）

用法（需 Milvus/Neo4j/嵌入 Key 就绪；避免与真实对话并发以减小噪声）：
    uv run python scripts/bench_retrieval.py [--qps 1,5,10,20] [--seconds 10] [--query 指定查询]

输出：每档 QPS 的请求数/成功率/总延迟分位数。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import retrieval_service as rs  # noqa: E402

WARMUP = 3  # 预热请求（连接池/集合就绪）


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(round(p / 100 * (len(s) - 1)), len(s) - 1)]


async def bench_once(query: str, counter: dict[str, Any]) -> None:
    """单次检索：search() 真实总延迟（内部向量/图谱并行）。"""
    t0 = time.perf_counter()
    hits = rs.search(query)
    elapsed = time.perf_counter() - t0
    counter["total"].append(elapsed)
    counter["ok"] += 1
    counter["vec_hits"].append(sum(1 for h in hits if h.source_type == "vector"))
    counter["graph_hits"].append(sum(1 for h in hits if h.source_type == "graph"))


def profile_stages(query: str) -> dict[str, float]:
    """单次串行阶段拆解（报告用：确认瓶颈占比，不参与 QPS 计数）。"""
    stages: dict[str, float] = {}
    t1 = time.perf_counter()
    rs._embed_query(query)  # noqa: SLF001
    stages["embed"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    rs._vector_search(query)  # noqa: SLF001
    stages["milvus"] = time.perf_counter() - t2

    t3 = time.perf_counter()
    rs._graph_search(query)  # noqa: SLF001
    stages["graph"] = time.perf_counter() - t3
    return stages


async def run_qps(qps: int, seconds: int, query: str, results: dict) -> None:
    """固定 QPS 档：均匀错峰启动请求，累计统计。"""
    counter: dict[str, Any] = {
        "total": [], "ok": 0, "fail": 0, "vec_hits": [], "graph_hits": [],
    }
    n = qps * seconds
    loop = asyncio.get_running_loop()
    start = loop.time()

    async def _one(i: int) -> None:
        delay = start + i / qps - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await bench_once(query, counter)
        except Exception as exc:  # noqa: BLE001 — 失败计入失败率
            counter["fail"] += 1
            counter.setdefault("errors", []).append(f"{type(exc).__name__}: {exc}"[:120])

    # 分批（每批 qps 个并发）避免一次性创建上千 task
    for batch_start in range(0, n, qps):
        await asyncio.gather(*(_one(i) for i in range(batch_start, min(batch_start + qps, n))))

    total = counter["total"]
    results[str(qps)] = {
        "qps_target": qps,
        "requests": len(total) + counter["fail"],
        "ok": counter["ok"],
        "fail": counter["fail"],
        "latency_ms": {
            "p50": round(_pct(total, 50) * 1000, 1),
            "p95": round(_pct(total, 95) * 1000, 1),
            "p99": round(_pct(total, 99) * 1000, 1),
            "avg": round(statistics.fmean(total) * 1000, 1) if total else 0,
        },
        "hit_avg": {
            "vector": round(statistics.fmean(counter["vec_hits"]), 1) if counter["vec_hits"] else 0,
            "graph": round(statistics.fmean(counter["graph_hits"]), 1) if counter["graph_hits"] else 0,
        },
    }
    if counter.get("errors"):
        results[str(qps)]["sample_errors"] = counter["errors"][:3]
    r = results[str(qps)]
    print(f"  [QPS={qps}] 完成 {r['ok']}/{r['requests']} "
          f"P50={r['latency_ms']['p50']}ms P95={r['latency_ms']['p95']}ms "
          f"P99={r['latency_ms']['p99']}ms")


def render_markdown(results: dict, profile: dict | None = None) -> str:
    lines = [
        "| QPS 目标 | 请求数 | 成功率 | P50 (ms) | P95 (ms) | P99 (ms) | 平均 (ms) |",
        "|---------|-------:|-------:|---------:|---------:|---------:|----------:|",
    ]
    for qps, r in sorted(results.items(), key=lambda kv: int(kv[0])):
        rate = f"{r['ok'] / r['requests']:.1%}" if r["requests"] else "—"
        lines.append(
            f"| {qps} | {r['requests']} | {rate} | {r['latency_ms']['p50']} "
            f"| {r['latency_ms']['p95']} | {r['latency_ms']['p99']} | {r['latency_ms']['avg']} |"
        )
    lines.append("")
    if profile:
        lines.append("| 阶段（串行参考） | 平均耗时 (ms) | 占比 |")
        lines.append("|------|-------------:|-----:|")
        total = sum(profile.values())
        for key, note in (
            ("embed", "查询嵌入（SiliconFlow bge-m3）"),
            ("milvus", "HNSW 向量召回 Top-20（含嵌入）"),
            ("graph", "图谱：lite 实体提取 + Neo4j 1-2 跳"),
        ):
            ms = round(profile[key] * 1000, 1)
            pct = f"{profile[key] / total:.0%}" if total else "—"
            lines.append(f"| {key} | {ms} | {pct} | {note} |")
        lines.append("")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="检索层 QPS 压测")
    parser.add_argument("--qps", default="1,5,10,20", help="QPS 档位（逗号分隔）")
    parser.add_argument("--seconds", type=int, default=10, help="每档持续秒数")
    parser.add_argument("--query", default="Insight AI 平台使用哪些检索技术？", help="压测查询")
    parser.add_argument("--json", default="", help="输出 JSON 路径（可选）")
    parser.add_argument("--profile", action="store_true", help="额外跑一次串行阶段拆解")
    args = parser.parse_args()

    qps_list = [int(q) for q in args.qps.split(",") if q.strip()]
    print(f"预热 {WARMUP} 次…")
    warm = {"total": [], "ok": 0, "fail": 0, "vec_hits": [], "graph_hits": []}
    for _ in range(WARMUP):
        await bench_once(args.query, warm)
    print(f"预热完成（平均 {statistics.fmean(warm['total']) * 1000:.0f}ms）\n")

    profile = profile_stages(args.query) if args.profile else None
    if profile:
        print("阶段拆解（串行参考）：" + ", ".join(
            f"{k}={v * 1000:.0f}ms" for k, v in profile.items()
        ) + "\n")

    results: dict = {}
    for qps in qps_list:
        await run_qps(qps, args.seconds, args.query, results)

    print("\n===== 结果（Markdown）=====")
    md = render_markdown(results, profile)
    print(md)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"results": results, "profile_ms": profile}, f,
                      ensure_ascii=False, indent=2)
        print(f"\nJSON 已写入 {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
