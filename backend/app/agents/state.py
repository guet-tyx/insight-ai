"""多智能体系统的全局与子图私有状态 Schema（对应计划《状态 Schema 设计与 Reducer 机制》）。

全局状态（GlobalState）：贯穿所有节点的强类型数据载体；
子图私有状态（*State）：只存在于对应 worker 子图内部，父图 checkpoint
不记录其中间变量（重试计数、浏览器 payload 等），仅在子图 END 后读取
暴露字段 —— 保障状态空间清洁与隔离。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class GlobalState(TypedDict):
    """主图全局状态（父图 checkpoint 记录全部字段）。"""

    # 消息历史：add_messages Reducer 增量追加而非覆盖
    messages: Annotated[list[BaseMessage], add_messages]
    # 任务分发路由指针（Supervisor 决策结果）
    next_node: str
    # Supervisor 拆解后的复合任务指令
    task_requirement: str
    # 采集阶段产出（Collector → GlobalState）
    raw_artifacts: Annotated[list[dict[str, Any]], operator.add]
    # 语义分块产出（Research → GlobalState，供 Analyst 引用真实内容）
    semantic_chunks: Annotated[list[dict[str, Any]], operator.add]
    # 研究阶段产出：实体/关系缓存（Research，W7 图谱补全）
    extracted_entities: Annotated[list[dict[str, Any]], operator.add]
    # 最终情报分析报告（Analyst 产出，Markdown）
    final_report: str
    # 人工审核反馈指令（W8 HITL interrupt 恢复注入）
    human_feedback: str
    # 审核修订轮数（W8 熔断：每轮写 1，Reducer 累加）
    review_count: Annotated[int, operator.add]
    # 循环计数：Supervisor 每轮写入 1，Reducer 累加（最大 Loop 熔断见 supervisor.py）
    iteration: Annotated[int, operator.add]


class CollectorState(TypedDict):
    """Collector 子图私有状态：采集任务与原始产物（不泄露到父图 checkpoint）。"""

    task_requirement: str  # 从父图注入的采集任务
    url: str  # 待采集 URL（Supervisor 拆解产出，可空）
    raw_artifacts: Annotated[list[dict[str, Any]], operator.add]
    retry_count: int  # 采集重试计数（私有）
    browser_payload: dict[str, Any]  # 浏览器执行细节（W6 扩展）


class ResearchState(TypedDict):
    """Research 子图私有状态：检索查询与语义/实体产出。"""

    task_requirement: str
    query: str
    semantic_chunks: Annotated[list[dict[str, Any]], operator.add]
    extracted_entities: Annotated[list[dict[str, Any]], operator.add]


class AnalystState(TypedDict):
    """Analyst 子图私有状态：报告草稿（私有中间产物）。"""

    raw_artifacts: list[dict[str, Any]]
    semantic_chunks: list[dict[str, Any]]  # W8 修复：Research 真实内容注入
    extracted_entities: list[dict[str, Any]]
    human_feedback: str = ""  # W8：HITL 修改意见（由父图映射注入）
    final_report: str
