"""Supervisor 路由节点：意图识别 + 任务拆解 + 最大 Loop 熔断。

按计划设计：Supervisor 本身不挂载具体工具（Handoff 语义），
通过 LLM 结构化输出一次完成「下一步专家 + 子任务指令」的决策；
条件边按 next_worker 分发，finish → END（W8 在此插入 HITL interrupt 卡点）。
"""
from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.agents.state import GlobalState
from app.core.config import settings

logger = logging.getLogger(__name__)

# 最大循环次数（计划风险表：节点无限死循环熔断）
MAX_ITERATIONS = 6

WORKER_DESCRIPTIONS = """你是 Insight AI 多智能体系统的调度中枢（Supervisor）。
根据用户指令决定下一步交给哪个专家，并为该专家写清子任务。

可用的专家：
- collector：需要采集网页内容时（用户给出网址、或要求"采集/抓取/爬取某个网站"）
- research：需要检索知识库/交叉验证时（问题涉及已上传文档、需要引用知识库）
- analyst：已有素材，需要生成分析报告/总结/周报时（Markdown 输出）
- finish：任务已完成或无法推进（如素材不足且无法采集）时

决策规则：
1. 复合任务按「采集 → 研究 → 分析」推进：缺素材先 collector，有素材先 research，
   素材齐备后 analyst，最后 finish。
2. 每轮只选择一个下一步动作；子任务须具体可执行。
3. 无法完成任务时直接 finish，不要无限循环。"""


class RouterPlan(BaseModel):
    """Supervisor 的结构化路由决策。"""

    next_worker: Literal["collector", "research", "analyst", "finish"] = Field(
        description="下一步执行专家"
    )
    task: str = Field(description="交给该专家的具体子任务指令")
    reason: str = Field(description="决策理由（简洁）")


def _router_llm() -> ChatOpenAI:
    if not settings.openai_api_key:
        raise RuntimeError("LLM 未配置（OPENAI_API_KEY）")
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.llm_base_url,
        temperature=0,
        max_retries=4,
        request_timeout=120,
    )


def supervise(state: GlobalState) -> dict:
    """Supervisor 节点：LLM 决策下一步专家；迭代超限强制 finish（熔断）。

    返回 iteration=1，由 state.py 的 operator.add Reducer 累加计数。
    """
    iteration = state.get("iteration", 0)
    if iteration >= MAX_ITERATIONS:
        logger.warning("Supervisor 达到最大循环次数 %s，强制终止", MAX_ITERATIONS)
        return {"next_node": "finish", "iteration": 1}

    llm = _router_llm().with_structured_output(RouterPlan)
    # 携带已有素材摘要，帮助决策（避免重复采集/研究）
    artifacts_n = len(state.get("raw_artifacts", []))
    entities_n = len(state.get("extracted_entities", []))
    report = state.get("final_report", "")
    context = (
        f"用户指令：{state.get('task_requirement', '')}\n"
        f"当前素材：采集产物 {artifacts_n} 条，研究实体 {entities_n} 条，"
        f"报告 {'已生成' if report else '未生成'}。"
    )
    plan: RouterPlan = llm.invoke(
        [
            HumanMessage(content=WORKER_DESCRIPTIONS),
            HumanMessage(content=f"当前进度\n{context}\n请决策下一步。"),
        ]
    )
    logger.info("Supervisor 决策: %s -> %s（%s）", plan.next_worker, plan.task[:60], plan.reason)
    return {"next_node": plan.next_worker, "iteration": 1}


def should_continue(state: GlobalState) -> str:
    """条件边：按 next_node 路由到对应 worker；finish 直接结束。

    W8 预留：finish 前插入 human_review 节点（interrupt 卡点）。
    """
    return state.get("next_node", "finish")