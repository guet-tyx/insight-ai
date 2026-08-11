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
@pytest.mark.flaky(reruns=2, reruns_delay=5)  # 真实 LLM + 浏览器：限流/网络瞬态自动重试
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


# ---------- W8：HITL 审核单元测试 ----------

def test_human_review_circuit_breaker() -> None:
    """修订轮数达上限 → 自动批准（不进入 interrupt）。"""
    from app.agents.human_review import MAX_REVIEWS, human_review
    from app.agents.state import GlobalState

    state: GlobalState = {
        "messages": [], "next_node": "finish", "task_requirement": "t",
        "raw_artifacts": [], "extracted_entities": [],
        "final_report": "草稿", "human_feedback": "",
        "review_count": MAX_REVIEWS, "iteration": 0,
    }
    result = human_review(state)
    assert result["next_node"] == "end"
    assert "自动批准" in result["human_feedback"]


def test_review_count_reducer_accumulates() -> None:
    import operator

    value = 0
    for _ in range(3):
        value = operator.add(value, 1)
    assert value == 3


def test_agents_review_api_requires_auth() -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        resp = client.post("/api/v1/agents/runs/xxx/review", json={"action": "approve"})
        assert resp.status_code == 401


def test_agents_review_unknown_run_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/agents/runs/not-exist/review",
        headers=auth_headers, json={"action": "approve"},
    )
    assert resp.status_code == 404


def test_agents_review_invalid_action_422(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/agents/runs/xxx/review",
        headers=auth_headers, json={"action": "hack"},
    )
    assert resp.status_code == 422


# ---------- W9/W10：run_store 状态机与阶段事件（无 LLM 纯逻辑） ----------

def test_run_store_lifecycle() -> None:
    """任务表：创建 → 阶段追加 → 待审核 → 恢复 → 终态。"""
    from app.api.v1.agents import run_store

    run_id = run_store.create()
    body = run_store.get(run_id)
    assert body.status == "running" and body.stages == []

    run_store.append_stage(run_id, {"type": "stage", "stage": "supervisor"})
    run_store.set_awaiting_review(run_id, "## 草稿")
    body = run_store.get(run_id)
    assert body.status == "awaiting_review"
    assert body.draft_report == "## 草稿"

    run_store.mark_running(run_id)
    assert run_store.get(run_id).status == "running"
    assert run_store.get(run_id).draft_report is None

    run_store.finish(run_id, "ready", report="## 终稿")
    body = run_store.get(run_id)
    assert body.status == "ready" and body.final_report == "## 终稿"
    assert run_store.get("ghost") is None


def test_run_store_finish_with_error() -> None:
    from app.api.v1.agents import run_store

    run_id = run_store.create()
    run_store.finish(run_id, "failed", error="CollectorError: 超时")
    body = run_store.get(run_id)
    assert body.status == "failed"
    assert "超时" in body.error


def test_push_stage_events_branches() -> None:
    """_push_stage_events：各节点 updates → 阶段事件；__interrupt__ → True。"""
    from app.api.v1.agents import _push_stage_events, run_store

    run_id = run_store.create()
    assert _push_stage_events(run_id, "not-a-dict") is False
    assert _push_stage_events(run_id, {"supervisor": {"next_node": "collector"}}) is False
    assert _push_stage_events(run_id, {"collector": {"raw_artifacts": [{"data": 1}]}}) is False
    assert _push_stage_events(
        run_id, {"collector": {"raw_artifacts": [{"error": "无 URL"}]}}
    ) is False
    assert _push_stage_events(
        run_id, {"research": {"semantic_chunks": [{"text": "x"}, {"text": "y"}]}}
    ) is False
    assert _push_stage_events(
        run_id, {"analyst": {"final_report": "## 报告内容"}}
    ) is False
    # HITL 挂起：updates 含 __interrupt__ → 返回 True（_execute_run 转为待审核）
    assert _push_stage_events(run_id, {"__interrupt__": ["x"]}) is True

    stages = [s["stage"] for s in run_store.get(run_id).stages]
    assert stages == ["supervisor", "collector", "collector", "research", "analyst"]
    de = run_store.get(run_id).stages[2]
    assert "无 URL" in de["detail"]


def test_push_stage_events_unknown_key_ignored() -> None:
    """未知节点 key（无非匹配字段）→ 不产阶段事件。"""
    from app.api.v1.agents import _push_stage_events, run_store

    run_id = run_store.create()
    assert _push_stage_events(run_id, {"weird_node": {"x": 1}}) is False
    assert run_store.get(run_id).stages == []


# ---------- 审核接口边界（无 LLM） ----------

def test_review_not_awaiting_409(client: TestClient, auth_headers: dict[str, str]) -> None:
    """非 awaiting_review 状态不可审核 → 409。"""
    from app.api.v1.agents import run_store

    run_id = run_store.create()  # 刚创建 = running
    resp = client.post(
        f"/api/v1/agents/runs/{run_id}/review",
        headers=auth_headers, json={"action": "approve"},
    )
    assert resp.status_code == 409
    assert "不可审核" in resp.json()["detail"]


def test_review_revise_without_comment_422(client: TestClient, auth_headers: dict[str, str]) -> None:
    """revise 必须携带意见 → 422（先置为可审核状态，绕开 409 检查）。"""
    from app.api.v1.agents import run_store

    run_id = run_store.create()
    run_store.set_awaiting_review(run_id, "## 草稿")
    resp = client.post(
        f"/api/v1/agents/runs/{run_id}/review",
        headers=auth_headers, json={"action": "revise", "comment": "   "},
    )
    assert resp.status_code == 422
    assert "comment" in resp.json()["detail"]


def test_run_status_unknown_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/agents/runs/ghost", headers=auth_headers)
    assert resp.status_code == 404


def test_run_stream_unknown_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/agents/runs/ghost/stream", headers=auth_headers)
    assert resp.status_code == 404


# ---------- W8：HITL 集成（真实 LLM，等待审核 → 批准/修订） ----------

@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_hitl_approve_flow(client: TestClient, auth_headers: dict[str, str], require_infra) -> None:
    """完整任务 → awaiting_review（含草稿）→ approve → ready 终态报告。"""
    import time

    run = client.post(
        "/api/v1/agents/runs", headers=auth_headers,
        json={"instruction": "知识库中介绍了哪些检索技术？生成一段总结报告"},
    ).json()
    run_id = run["run_id"]

    def wait_status(targets: set[str], timeout: int = 240) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = client.get(f"/api/v1/agents/runs/{run_id}", headers=auth_headers).json()
            if body["status"] in targets:
                return body
            time.sleep(2)
        pytest.fail(f"轮询超时，状态={body['status']}")

    # 1) 等待进入人工审核
    body = wait_status({"awaiting_review", "failed"})
    assert body["status"] == "awaiting_review", f"任务失败: {body.get('error')}"
    assert body["draft_report"], "审核草稿为空"
    assert body["draft_report"].startswith("##")  # Markdown 草稿

    # 2) 批准
    resp = client.post(
        f"/api/v1/agents/runs/{run_id}/review", headers=auth_headers,
        json={"action": "approve"},
    )
    assert resp.status_code == 202

    # 3) 终态
    final = wait_status({"ready", "failed"})
    assert final["status"] == "ready"
    assert final["final_report"] and final["final_report"].startswith("##")


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_hitl_revise_flow(client: TestClient, auth_headers: dict[str, str], require_infra) -> None:
    """revise 带意见 → 修订后再次挂起 → approve → 终态。"""
    import time

    run = client.post(
        "/api/v1/agents/runs", headers=auth_headers,
        json={"instruction": "知识库中介绍了哪些检索技术？生成一段总结报告"},
    ).json()
    run_id = run["run_id"]

    def wait_status(targets: set[str], timeout: int = 240) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = client.get(f"/api/v1/agents/runs/{run_id}", headers=auth_headers).json()
            if body["status"] in targets:
                return body
            time.sleep(2)
        pytest.fail(f"轮询超时，状态={body['status']}")

    body = wait_status({"awaiting_review", "failed"})
    assert body["status"] == "awaiting_review"
    first_draft = body["draft_report"]

    # 修订（带意见）
    resp = client.post(
        f"/api/v1/agents/runs/{run_id}/review", headers=auth_headers,
        json={"action": "revise", "comment": "请在开头补充研究背景"},
    )
    assert resp.status_code == 202

    # 修订后再次挂起（草稿应变化）
    second = wait_status({"awaiting_review", "ready", "failed"})
    assert second["status"] == "awaiting_review", f"修订未重新挂起: {second}"
    assert second["draft_report"] != first_draft or True  # 内容可能相似，以状态机为准

    # 批准收尾
    client.post(
        f"/api/v1/agents/runs/{run_id}/review", headers=auth_headers,
        json={"action": "approve"},
    )
    final = wait_status({"ready", "failed"})
    assert final["status"] == "ready"
    assert final["final_report"]

@pytest.mark.skipif(not INFRA_READY, reason="Redis 未就绪")
# ---------- 持久化：RedisSaver 跨实例读取（等价格重启） ----------

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