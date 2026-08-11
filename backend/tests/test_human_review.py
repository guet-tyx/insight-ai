"""W8/W10 human_review 审核节点单元测试：interrupt 挂起 / 动作映射 / 熔断。"""
from __future__ import annotations

from app.agents.human_review import MAX_REVIEWS, after_review, human_review
from app.agents.state import GlobalState


def _state(**over: dict) -> GlobalState:
    base: GlobalState = {
        "messages": [], "next_node": "finish", "task_requirement": "t",
        "raw_artifacts": [], "extracted_entities": [],
        "final_report": "## 草稿", "human_feedback": "",
        "review_count": 0,
    }
    base.update(over)  # type: ignore[arg-type]
    return base


def test_interrupt_payload_contains_draft(monkeypatch) -> None:
    """挂起 payload：草稿全文 + 阶段标识（前端审核卡渲染依据）。"""
    captured: dict = {}

    def _fake_interrupt(payload: dict):
        captured.update(payload)
        return {"action": "approve"}

    monkeypatch.setattr("app.agents.human_review.interrupt", _fake_interrupt)
    result = human_review(_state(final_report="## 草稿\n内容"))
    assert captured == {"draft": "## 草稿\n内容", "stage": "report_review"}
    assert result["next_node"] == "end"
    assert result["human_feedback"] == ""
    assert result["review_count"] == 1


def test_reject_action(monkeypatch) -> None:
    monkeypatch.setattr("app.agents.human_review.interrupt",
                        lambda payload: {"action": "reject", "comment": "数据不足"})
    result = human_review(_state())
    assert result["next_node"] == "end"
    assert result["human_feedback"] == "数据不足"


def test_revise_action_routes_to_analyst(monkeypatch) -> None:
    monkeypatch.setattr("app.agents.human_review.interrupt",
                        lambda payload: {"action": "revise", "comment": "补充背景"})
    result = human_review(_state())
    assert result["next_node"] == "analyst"
    assert result["human_feedback"] == "补充背景"


def test_unknown_action_falls_back_reject(monkeypatch, caplog) -> None:
    """未知动作 → 按 reject 处理（不崩溃、不越权）。"""
    import logging

    monkeypatch.setattr("app.agents.human_review.interrupt",
                        lambda payload: {"action": "hack"})
    with caplog.at_level(logging.WARNING):
        result = human_review(_state())
    assert result["next_node"] == "end"
    assert "未知审核动作" in caplog.text


def test_non_dict_feedback_default_approve(monkeypatch) -> None:
    """反馈非 dict（如 None）→ 默认 approve。"""
    monkeypatch.setattr("app.agents.human_review.interrupt", lambda payload: None)
    result = human_review(_state())
    assert result["next_node"] == "end"


def test_circuit_breaker_auto_approve() -> None:
    """修订轮数达上限 → 自动批准（附注），不进入 interrupt。"""
    state = _state(review_count=MAX_REVIEWS)
    result = human_review(state)
    assert result["next_node"] == "end"
    assert "自动批准" in result["human_feedback"]
    assert result["review_count"] == 1


def test_after_review_routing() -> None:
    """条件边：next_node=analyst → analyst；其余 → finish。"""
    assert after_review({"next_node": "analyst"}) == "analyst"
    assert after_review({"next_node": "end"}) == "finish"
    assert after_review({"next_node": "finish"}) == "finish"
