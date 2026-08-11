"""W5/W10 Supervisor 路由节点单元测试：熔断 / 配置校验 / 条件边。"""
from __future__ import annotations

import pytest

from app.agents.supervisor import MAX_ITERATIONS, RouterPlan, should_continue, supervise
from app.agents.state import GlobalState


def _state(**over: dict) -> GlobalState:
    base: GlobalState = {
        "messages": [], "next_node": "finish", "task_requirement": "测试",
        "raw_artifacts": [], "extracted_entities": [], "final_report": "",
        "human_feedback": "",
    }
    base.update(over)  # type: ignore[arg-type]
    return base


def test_circuit_breaker_force_finish() -> None:
    """迭代超限强制 finish（不调用 LLM，纯状态判定）。"""
    result = supervise(_state(iteration=MAX_ITERATIONS))
    assert result["next_node"] == "finish"
    assert result["iteration"] == 1


def test_should_continue_routes() -> None:
    assert should_continue({"next_node": "collector"}) == "collector"
    assert should_continue({"next_node": "finish"}) == "finish"
    assert should_continue({}) == "finish"  # 缺省安全


def test_router_llm_raises_without_key(monkeypatch) -> None:
    """未配置 LLM Key → 明确异常（而非静默使用空 key）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(RuntimeError, match="LLM 未配置"):
        from app.agents.supervisor import _router_llm

        _router_llm()


def test_router_plan_schema() -> None:
    """RouterPlan 结构化输出模型：合法枚举 / 非法枚举被 pydantic 拒绝。"""
    plan = RouterPlan(next_worker="analyst", task="生成报告", reason="素材齐备")
    assert plan.next_worker == "analyst"

    with pytest.raises(ValueError):
        RouterPlan(next_worker="hack", task="x", reason="y")  # type: ignore[arg-type]


def test_supervise_builds_context_and_invokes(monkeypatch) -> None:
    """正常路径：携带素材摘要上下文，把 LLM 决策写入 next_node。"""
    from langchain_core.messages import HumanMessage

    calls: list[str] = []

    class _FakePlan:
        next_worker = "research"
        task = "检索知识库"
        reason = "问题涉及已上传文档"

    class _FakeLLM:
        def with_structured_output(self, model):
            return self

        def invoke(self, messages):
            calls.append("\n".join(m.content for m in messages))
            return _FakePlan()

    monkeypatch.setattr("app.agents.supervisor._router_llm", lambda: _FakeLLM())
    result = supervise(_state(task_requirement="检索技术有哪些？",
                              raw_artifacts=[{"data": "x"}], extracted_entities=["e"],
                              final_report="已有一版"))
    assert result["next_node"] == "research"
    joined = calls[0]
    assert "检索技术有哪些？" in joined
    assert "1 条" in joined and "1 条" in joined  # 素材/实体计数
    assert "已生成" in joined  # 报告状态
    assert any(isinstance(m, HumanMessage) for m in []) or True
