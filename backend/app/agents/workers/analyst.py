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
        max_retries=4,
        request_timeout=120,
    )


def analyst_node(state: AnalystState) -> dict[str, Any]:
    artifacts = state.get("raw_artifacts") or []
    chunks = state.get("semantic_chunks") or []  # W8 修复：Research 真实内容
    entities = state.get("extracted_entities") or []
    if not artifacts and not chunks and not entities:
        return {"final_report": "## 结论\n\n当前没有可用素材，无法生成分析报告。"}

    material = []
    for i, a in enumerate(artifacts, start=1):
        material.append(f"[{i}] 采集产物：{a}")
    for j, c in enumerate(chunks, start=len(artifacts) + 1):
        # 语义分块携带真实检索内容与溯源（得分/来源类型）
        material.append(
            f"[{j}] 知识片段（{c.get('source_type', 'vector')} · 文档 {str(c.get('doc_id', ''))[:8]}"
            f"{' · 第 ' + str(c.get('page')) + ' 页' if c.get('page') else ''}"
            f"{' · 标题「' + str(c.get('header')) + '」' if c.get('header') else ''}）：{c.get('text', '')}"
        )
    for k, e in enumerate(entities, start=len(artifacts) + len(chunks) + 1):
        material.append(f"[{k}] 研究片段：{e}")

    # W8：HITL 修改意见注入（修订轮次针对性调整，不推翻重写）
    feedback = (state.get("human_feedback") or "").strip()
    prompt = ANALYST_PROMPT
    if feedback:
        prompt += (
            "\n\n⚠️ 用户审核意见（必须在本次修订中落实）：\n"
            f"{feedback}\n"
            "请在保留原有结构的基础上，针对意见修改对应章节；"
            "不要删除与意见无关的既有事实与引用。"
        )

    llm = _llm()
    resp = llm.invoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(content="【素材列表】\n" + "\n".join(material) + "\n\n请生成分析报告。"),
        ]
    )
    report = str(resp.content)
    logger.info("Analyst 报告生成：%d 字（素材 %d 采集/%d 片段/%d 实体）",
                len(report), len(artifacts), len(chunks), len(entities))
    return {"final_report": report}


def build_analyst_subgraph():
    """构建 Analyst 子图（私有状态 AnalystState，单节点）。"""
    workflow = StateGraph(AnalystState)
    workflow.add_node("generate", analyst_node)
    workflow.add_edge(START, "generate")
    workflow.add_edge("generate", END)
    return workflow.compile()