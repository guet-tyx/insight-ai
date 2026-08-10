"""顶层 Supervisor-Worker 主图组装。

拓扑：
    START → supervisor ──collector──→ [Collector 子图] ──┐
              │  ──research───→  [Research 子图]  ──┐   │（子图产出并入 GlobalState）
              │  ──analyst────→  [Analyst 子图]   ──┐ │ │
              │  ──finish─────→  END（W8: 先经 HITL interrupt 卡点）
              └──────────────────── 循环回 supervisor ┘ └ ┘
Supervisor 不持有工具（Handoff 语义），仅路由；循环由 iteration Reducer 熔断。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from app.agents.state import GlobalState
from app.agents.supervisor import supervise, should_continue
from app.agents.workers.analyst import build_analyst_subgraph
from app.agents.workers.collector import build_collector_subgraph
from app.agents.workers.research import build_research_subgraph
from app.core.checkpointer import get_checkpointer_sync

logger = logging.getLogger(__name__)

_graph = None


def _subgraph_mapper(sub_state: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """父图 → 子图：注入全局任务指令到子图输入（按需裁剪字段）。"""
    return {"task_requirement": (parent or {}).get("task_requirement", "")}


def build_graph():
    """构建主图（Supervisor + 三专家子图，共享 RedisSaver 检查点）。"""
    collector = build_collector_subgraph()
    research = build_research_subgraph()
    analyst = build_analyst_subgraph()

    workflow = StateGraph(GlobalState)
    workflow.add_node("supervisor", supervise)
    workflow.add_node("collector", collector)
    workflow.add_node("research", research)
    workflow.add_node("analyst", analyst)

    workflow.add_edge(START, "supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        should_continue,
        {
            "collector": "collector",
            "research": "research",
            "analyst": "analyst",
            "finish": END,
        },
    )
    # worker 完成后回到 supervisor 决策下一轮（直至 finish / 熔断）
    workflow.add_edge("collector", "supervisor")
    workflow.add_edge("research", "supervisor")
    workflow.add_edge("analyst", "supervisor")

    return workflow.compile(checkpointer=get_checkpointer_sync())


def get_graph():
    """全局单例主图（检查点共享，重启后任务状态跨实例可恢复）。"""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def start_run(instruction: str, thread_id: str) -> dict:
    """启动一次多智能体任务（同步入口，供 CLI / 后台任务使用）。"""
    graph = get_graph()
    return graph.invoke(
        {
            "messages": [HumanMessage(content=instruction)],
            "task_requirement": instruction,
            "next_node": "",
            "raw_artifacts": [],
            "extracted_entities": [],
            "final_report": "",
            "human_feedback": "",
        },
        config={"configurable": {"thread_id": thread_id}},
    )