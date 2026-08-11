"""LLM 实体与关系抽取：非结构化文本 → 领域本体三元组（W7）。

领域本体（计划指定）：
- 节点类型：Company / Technology / Person / Paper / Event
- 关系类型：DEVELOPED / INVESTED_IN / AUTHORED

⚠️ 必须使用主模型 deepseek-v4-flash 的 with_structured_output：
lite 模型（sensenova-6.7-flash-lite）网关侧缺 xgrammar 模块，
guided grammar 结构化输出会 400（见环境报告 §14）。
"""

from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator

from app.core.config import settings

logger = logging.getLogger(__name__)

# 领域本体（计划定义）
NODE_TYPES = ("Company", "Technology", "Person", "Paper", "Event")
REL_TYPES = ("DEVELOPED", "INVESTED_IN", "AUTHORED")

EXTRACTION_PROMPT = """你是知识图谱构建专家。从给定文本中抽取实体与关系三元组。

实体类型（必须限定为以下之一）：Company（公司/机构）、Technology（技术/产品/框架/模型）、
Person（人物）、Paper（论文/文档）、Event（事件/时间节点）。
关系类型（必须限定为以下之一）：DEVELOPED（开发/创建/构建）、INVESTED_IN（投资/采用/
投入资源）、AUTHORED（撰写/作者关系）。

【示例】
文本：InsightAI 公司基于 LangGraph 框架开发了智能平台，张伟撰写了相关论文。
输出：
entities: [{"name":"InsightAI","type":"Company"},{"name":"LangGraph","type":"Technology"},
{"name":"张伟","type":"Person"}]
relations: [{"source":"InsightAI","target":"LangGraph","type":"DEVELOPED"},
{"source":"张伟","target":"相关论文","type":"AUTHORED"}]

【规则】
1. 只抽取文本中明确出现的实体，不推测、不编造。
2. 技术名词（框架/模型/数据库/产品名）、公司机构、人名是高频实体，应积极抽取。
3. 三元组必须有文本依据：evidence 字段填写原句摘录（≤120 字）。
4. 只有完全无法判断类型时才放弃该实体。"""


class Entity(BaseModel):
    name: str = Field(description="实体规范名")
    type: Literal["Company", "Technology", "Person", "Paper", "Event"] = Field(
        description="实体类型"
    )


class Relation(BaseModel):
    source: str = Field(description="关系源实体名（须与 entities 中一致）")
    target: str = Field(description="关系目标实体名（须与 entities 中一致）")
    type: Literal["DEVELOPED", "INVESTED_IN", "AUTHORED"] = Field(description="关系类型")
    evidence: str = Field(default="", description="原句证据摘录（≤120 字）")


class EntityExtraction(BaseModel):
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)

    @field_validator("relations")
    @classmethod
    def _relations_refer_existing_entities(cls, v: list[Relation]) -> list[Relation]:
        """图写入前的引用完整性由 write_triples 兜底；此处仅过滤自引用。"""
        return [r for r in v if r.source != r.target]


def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.llm_model,  # 主模型：受 xgrammar 限制，lite 不可用
        api_key=settings.openai_api_key,
        base_url=settings.llm_base_url,
        temperature=0,
        max_retries=4,
        request_timeout=120,
    )


def extract_entities(text: str) -> EntityExtraction:
    """从一段文本抽取实体与关系三元组（单次 LLM 调用）。"""
    if not text.strip():
        return EntityExtraction()
    llm = _llm().with_structured_output(EntityExtraction)
    result = llm.invoke(
        [
            SystemMessage(content=EXTRACTION_PROMPT),
            HumanMessage(content=f"【文本】\n{text[:3000]}"),
        ]
    )
    # with_structured_output 可能返回 dict（兼容保护）
    if isinstance(result, dict):
        result = EntityExtraction.model_validate(result)
    logger.info("实体抽取: %d 实体, %d 关系", len(result.entities), len(result.relations))
    return result
