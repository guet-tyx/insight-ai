"""human_review 审核节点（W8 HITL）：interrupt 挂起 + 恢复分支。

拓扑：
    supervisor(finish) → human_review ──approve──→ END
                                   ├──reject───→ END
                                   └──revise───→ analyst（带意见重写）→ supervisor → human_review

修订熔断（MAX_REVIEWS）：超限自动 approve，防无限修订循环。
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from app.agents.state import GlobalState

logger = logging.getLogger(__name__)

MAX_REVIEWS = 3  # 修订轮数上限（自动批准）

_ACTION_TO_NEXT = {
    "approve": "end",
    "reject": "end",
    "revise": "analyst",
}


def human_review(state: GlobalState) -> dict[str, Any]:
    """报告审核卡点：挂起图，等待人工反馈（approve/reject/revise + comment）。

    - 挂起 payload：草稿全文 + 阶段标识（前端渲染审核卡）
    - 恢复值：Command(resume={"action": ..., "comment": ...})
    - 熔断：修订轮数达上限 → 自动 approve 且附注
    """
    review_count = state.get("review_count", 0)
    if review_count >= MAX_REVIEWS:
        logger.warning("修订轮数达上限 %s，自动批准", MAX_REVIEWS)
        return {
            "next_node": "end",
            "human_feedback": f"(系统)修订轮数达上限 {MAX_REVIEWS}，自动批准",
            "review_count": 1,
        }

    feedback: Any = interrupt({
        "draft": state.get("final_report", ""),
        "stage": "report_review",
    })
    action = "approve"
    comment = ""
    if isinstance(feedback, dict):
        action = str(feedback.get("action", "approve"))
        comment = str(feedback.get("comment", ""))
    if action not in _ACTION_TO_NEXT:
        logger.warning("未知审核动作 %r，按 reject 处理", action)
        action = "reject"
    logger.info("HITL 审核: action=%s comment=%s", action, comment[:50])
    return {
        "next_node": _ACTION_TO_NEXT[action],
        "human_feedback": comment,
        "review_count": 1,
    }


def after_review(state: GlobalState) -> str:
    """human_review 条件边：revise → analyst 重写；其余 → END。"""
    return "analyst" if state.get("next_node") == "analyst" else "finish"