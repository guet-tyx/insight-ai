"""Analyst 专家子图：基于多源素材生成 Markdown 情报分析报告（引用溯源）。

子图输入：raw_artifacts（采集）+ extracted_entities（研究）
子图输出：final_report（Markdown，含 [N] 引用编号，映射进 GlobalState）
W8 增强：报告生成后进入 HITL 审核卡点（interrupt）。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from app.agents.state import AnalystState
from app.core.config import settings

logger = logging.getLogger(__name__)

ANALYST_PROMPT = """你是归纳分析专家（Analyst Agent）。
基于提供的【采集素材】与【知识库检索结果】生成 Markdown 情报分析报告。
要求：
1. 结构完整：## 概述 / ## 关键发现 / ## 时间线（如适用）/ ## 结论
2. 引用溯源：引用素材时在句末标注 [编号]（编号对应素材列表序号）
3. 明确区分「素材事实」与「分析推断」，禁止编造素材之外的信息
4. 使用中文输出"""


def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.llm_base_url,
        temperature=0.3,
    )


def analyst_node(state: AnalystState) -> dict[str, Any]:
    artifacts = state.get("raw_artifacts") or []
    entities = state.get("extracted_entities") or []
    if not artifacts and not entities:
        return {"final_report": "## 结论\n\n当前没有可用素材，无法生成分析报告。"}

    material = []
    for i, a in enumerate(artifacts, start=1):
        material.append(f"[{i}] 采集产物：{a}")
    for j, e in enumerate(entities, start=len(artifacts) + 1):
        material.append(f"[{j}] 研究片段：{e}")

    llm = _llm()
    resp = llm.invoke(
        [
            SystemMessage(content=ANALYST_PROMPT),
            HumanMessage(content="【素材列表】\n" + "\n".join(material) + "\n\n请生成分析报告。"),
        ]
    )
    report = str(resp.content)
    logger.info("Analyst 报告生成：%d 字", len(report))
    return {"final_report": report}


def build_analyst_subgraph():
    """构建 Analyst 子图（私有状态 AnalystState，单节点）。"""
    workflow = StateGraph(AnalystState)
    workflow.add_node("generate", analyst_node)
    workflow.add_edge(START, "generate")
    workflow.add_edge("generate", END)
    return workflow.compile()