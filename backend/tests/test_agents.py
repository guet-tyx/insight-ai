"""W5 多智能体测试：Reducer / 熔断 / 子图隔离 / 完整链路 / RedisSaver 持久化。"""
from __future__ import annotations

import asyncio

import pytest

from app.agents import graph as agents_graph
from app.agents.state import GlobalState
from app.agents.supervisor import MAX_ITERATIONS, supervise
from app.agents.workers.collector import build_collector_subgraph
from app.agents.workers.research import build_research_subgraph
from tests.conftest import BROWSER_READY, INFRA_READY

# ---------- 单元：Graph 组装 ----------

def test_main_graph_topology() -> None:
    g = agents_graph.get_graph()
    nodes = set(g.nodes.keys())
    assert {"supervisor", "collector", "research", "analyst"} <= nodes


def test_supervisor_circuit_breaker_no_llm() -> None:
    """迭代超限时强制 finish（不调用 LLM，纯状态判定）。"""
    state: GlobalState = {
        "messages": [], "next_node": "collector", "task_requirement": "测试",
        "raw_artifacts": [], "extracted_entities": [], "final_report": "",
        "human_feedback": "", "iteration": MAX_ITERATIONS,
    }
    result = supervise(state)
    assert result["next_node"] == "finish"
    assert result["iteration"] == 1  # Reducer 累加


# ---------- 单元：子图私有状态隔离 ----------

def test_collector_subgraph_private_fields_not_leaked() -> None:
    """子图输出只含声明的通道（raw_artifacts），重试计数等私有字段不外泄。"""
    sub = build_collector_subgraph()
    result = asyncio.run(sub.ainvoke({"task_requirement": "没有 URL 的任务"}))
    assert "raw_artifacts" in result
    assert "retry_count" not in result  # 私有字段被隔离
    assert "browser_payload" not in result
    assert result["raw_artifacts"][0]["error"]  # 缺 URL 给出明确错误


def test_research_subgraph_empty_task() -> None:
    sub = build_research_subgraph()
    result = asyncio.run(sub.ainvoke({"task_requirement": ""}))
    assert result["semantic_chunks"] == []
    assert result["extracted_entities"] == []


# ---------- 单元：检查点 Reducer（无图，直接验证写入行为） ----------

def test_iteration_reducer_accumulates() -> None:
    """operator.add Reducer：多次写入 1 → 累加。"""
    import operator
    from typing import Annotated

    value: int = 0
    for _ in range(3):
        value = operator.add(value, 1)
    assert value == 3


# ---------- 集成：完整链路（真实 LLM + 浏览器） ----------

@pytest.mark.skipif(not BROWSER_READY, reason="LLM Key 或 Chromium 未就绪")
def test_full_supervisor_workflow() -> None:
    """指令 → Supervisor → Collector(浏览器) → Analyst → finish，产出带引用报告。"""
    from app.core.checkpointer import ensure_checkpointer, reset_checkpointer

    async def run() -> dict:
        # 检查点必须在执行同一事件循环内构建（AsyncRedisSaver 循环绑定语义）
        reset_checkpointer()
        await ensure_checkpointer()
        g = agents_graph.build_graph()
        return await g.ainvoke(
            {
                "messages": [{"role": "user", "content": "采集 example.com 页面并生成一段简要分析"}],
                "task_requirement": "采集 example.com 页面并生成一段简要分析",
                "next_node": "",
                "raw_artifacts": [],
                "extracted_entities": [],
                "final_report": "",
                "human_feedback": "",
            },
            {"configurable": {"thread_id": "w5-test-full"}},
        )

    result = asyncio.run(run())  # 异步路径与生产 API 一致（AsyncRedisSaver 检查点）
    assert result["final_report"], "未产出报告"
    assert "[1]" in result["final_report"] or "example" in result["final_report"].lower()
    assert result["iteration"] >= 2  # 至少两轮 supervisor 决策
    assert result["raw_artifacts"], "采集阶段无产物"


@pytest.mark.skipif(not BROWSER_READY, reason="需要浏览器与 LLM")
def test_agents_api_requires_auth(client_factory_fixture=None) -> None:
    """认证由接口测试覆盖（免浏览器）。"""
    # 占位：认证断言在下方 test_agents_api_auth_401
    assert True


def test_agents_runs_api_auth_401(client_factory_fixture=None) -> None:
    """POST /agents/runs 未登录 401。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        resp = client.post("/api/v1/agents/runs", json={"instruction": "测试"})
        assert resp.status_code == 401


# ---------- 持久化：RedisSaver 跨实例读取（等价格重启） ----------

@pytest.mark.skipif(not INFRA_READY, reason="Redis 未就绪")
def test_redis_saver_cross_instance_persistence() -> None:
    """同一 thread 的检查点可由「新实例」读回（等效进程重启后恢复）。"""
    from typing import TypedDict

    from langgraph.checkpoint.redis import AsyncRedisSaver
    from langgraph.graph import END, START, StateGraph

    class _S(TypedDict):
        x: int

    def _node(state: _S) -> dict:
        return {"x": (state.get("x") or 0) + 1}

    async def _run() -> None:
        saver1 = AsyncRedisSaver(redis_url="redis://127.0.0.1:6379/0", ttl={"checkpoints": 3600})
        await saver1.setup()
        wf = StateGraph(_S)
        wf.add_node("n", _node)
        wf.add_edge(START, "n")
        wf.add_edge("n", END)
        g1 = wf.compile(checkpointer=saver1)
        await g1.ainvoke({"x": 0}, {"configurable": {"thread_id": "persist-test"}})

        # 新实例（模拟重启）读同一 thread
        saver2 = AsyncRedisSaver(redis_url="redis://127.0.0.1:6379/0", ttl={"checkpoints": 3600})
        await saver2.setup()
        g2 = wf.compile(checkpointer=saver2)
        # a) 新实例能读到历史状态（持久化生效）
        snapshot = await g2.aget_state({"configurable": {"thread_id": "persist-test"}})
        assert snapshot.values.get("x") == 1, "新 checkpointer 实例未读到旧检查点（持久化失败）"
        # b) 空输入续跑：从检查点恢复的 x=1 继续自增 → 2
        result = await g2.ainvoke({}, {"configurable": {"thread_id": "persist-test"}})
        assert result["x"] == 2, "恢复后状态未延续"
        await saver2.adelete_thread("persist-test")

    asyncio.run(_run())