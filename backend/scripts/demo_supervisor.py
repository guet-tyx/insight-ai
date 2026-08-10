"""CLI 演示：Supervisor-Worker 多智能体任务（W5）。

用法（backend/ 目录下，需先启动 docker compose up -d）：
    uv run python scripts/demo_supervisor.py --internal-demo "分析 http://127.0.0.1:8099/demo_page.html 并生成周报要点"
    uv run python scripts/demo_supervisor.py "知识库中介绍了哪些检索技术？写一份总结"
    （--internal-demo 允许采集内网地址供本地演示）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main(instruction: str, allow_internal: bool) -> None:
    from app.agents import graph
    from app.core.checkpointer import ensure_checkpointer

    await ensure_checkpointer()  # 检查点构建于本事件循环（幂等）
    g = graph.get_graph()
    print(f"▶ 用户指令: {instruction}")
    print("=" * 64)
    async for _m, data in g.astream(
        {
            "messages": [{"role": "user", "content": instruction}],
            "task_requirement": instruction,
            "next_node": "",
            "raw_artifacts": [],
            "extracted_entities": [],
            "final_report": "",
            "human_feedback": "",
        },
        config={"configurable": {"thread_id": "cli-demo"}},
        stream_mode=["updates"],
    ):
        for node, state in data.items():
            if not isinstance(state, dict) or not state:
                continue
            if "next_node" in state:
                print(f"\n🔄 Supervisor 决策 → {state.get('next_node')}")
            elif "raw_artifacts" in state:
                arts = state.get("raw_artifacts") or []
                for a in arts:
                    err = a.get("error", "")
                    print(f"    📡 Collector: url={a.get('url', '?')}" + (f" error={err}" if err else " ✓"))
            elif "semantic_chunks" in state:
                print(f"    🔎 Research: 检索片段 {len(state.get('semantic_chunks') or [])} 条")
            elif "final_report" in state:
                print(f"    📝 Analyst: 报告生成 {len(state.get('final_report') or '')} 字")
    print("\n" + "=" * 64)
    final = (g.get_state({"configurable": {"thread_id": "cli-demo"}}).values or {}).get("final_report", "")
    print(final if final else "(无报告输出)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Supervisor-Worker 多智能体任务演示")
    parser.add_argument("instruction", nargs="?", default="知识库中介绍了哪些检索技术？写一份总结", help="复合分析指令")
    parser.add_argument("--internal-demo", action="store_true", help="允许采集内网地址（本地演示）")
    args = parser.parse_args()
    asyncio.run(main(args.instruction, args.internal_demo))