"""LangGraph 单 Agent：知识库检索 + 网页采集工具编排，SSE 事件流输出。

阶段一里程碑核心：用户提问 → Agent 决策调用工具 → 流式回答。
事件协议（每行 data: JSON，空行分隔）：
    {"type": "tool_start", "name", "args"}
    {"type": "tool_end",   "name", "preview"}
    {"type": "token",      "content"}     # 仅在模型输出阶段
    {"type": "done",       "answer"}
    {"type": "error",      "message"}
心跳：工具执行长耗时期间每 15s 输出 SSE 注释行 ": ping"。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from app.core.config import settings
from app.services.collector_service import collect
from app.services.retrieval_service import search as knowledge_search_svc

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15  # 工具执行长耗时期间的心跳间隔
TOOL_PREVIEW_CHARS = 200


@tool
def knowledge_search(query: str, top_k: int = 5) -> str:
    """在知识库中检索与问题最相关的资料片段（带引用编号与溯源信息）。

    当用户问题涉及平台已上传文档的内容时优先调用本工具。
    """
    hits = knowledge_search_svc(query, top_k=top_k)
    if not hits:
        return "知识库中未检索到相关内容。"
    return "\n".join(
        f"[{i + 1}] (文档 {h.doc_id[:8]}，第 {h.page_number} 页，标题「{h.parent_header}」)：{h.chunk_text}"
        for i, h in enumerate(hits)
    )


@tool
async def collect_webpage(url: str, instruction: str) -> str:
    """采集指定网页内容（真实浏览器执行，支持动态渲染页面）。

    注意：浏览器操作需要约 20-60 秒；结果可能为结构化 JSON 或文本。
    """
    data = await collect(url, instruction)
    return json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)


SYSTEM_PROMPT = """你是 Insight AI 情报分析助手（阶段一 MVP），负责回答与知识库、网页信息相关的问题。

工具使用规则：
1. 优先调用 knowledge_search 检索知识库；引用结果时保留 [编号] 溯源标注。
2. 用户明确要求采集某个网页时，调用 collect_webpage；提前告知"正在采集网页（约 20-60 秒）"。
3. 知识库无相关内容时明确说明"知识库中未找到相关信息"，不要编造。
4. 始终使用中文回答，简洁准确。"""


def _extract_token_text(content: Any) -> str:
    """从模型输出块提取纯回答文本，过滤思维链（reasoning）等非展示内容。

    langchain 流式块 content 可能为 str 或 list[dict]（含 type=text/reasoning
    等分区）；deepseek-v4-flash 会产生 reasoning_content，必须过滤。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type", "text") == "text"
        )
    return ""


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def build_agent():
    """构建 ReAct 单 Agent（MemorySaver 内存检查点，thread_id=会话 ID）。"""
    if not settings.openai_api_key:
        raise RuntimeError("LLM 未配置（OPENAI_API_KEY）")
    llm = ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.llm_base_url,
        temperature=0,
    )
    return create_react_agent(
        llm,
        tools=[knowledge_search, collect_webpage],
        checkpointer=MemorySaver(),
        prompt=SYSTEM_PROMPT,
    )


_agent = None


def get_agent():
    """全局单例 Agent。

    ⚠️ 必须复用同一实例：各请求共享同一个 MemorySaver 检查点，
    否则每次请求新建实例会导致多轮会话记忆完全丢失。
    （多进程部署需切换 RedisSaver，W5 落地）
    """
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


async def stream_sse(session_id: str, message: str) -> AsyncIterator[str]:
    """以 SSE 文本形式流式返回 Agent 执行事件。

    通过独立任务消费 agent.astream（避免心跳超时取消中断工具执行），
    事件经 asyncio.Queue 转发；空白超时发 ": ping" 心跳。
    """
    agent = get_agent()
    config = {"configurable": {"thread_id": session_id}}
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    seen_tools: set[str] = set()  # 去重工具启动事件

    async def consume() -> None:
        try:
            async for mode, data in agent.astream(
                {"messages": [{"role": "user", "content": message}]},
                config=config,
                stream_mode=["messages", "updates"],
            ):
                await queue.put((mode, data))
        except Exception as exc:  # noqa: BLE001 — 异常转为 error 事件
            logger.error("Agent 流式执行失败：%s", exc, exc_info=True)
            await queue.put(("error", exc))
        finally:
            await queue.put(("close", None))

    task = asyncio.create_task(consume())
    try:
        while True:
            try:
                mode, data = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # SSE 注释行心跳，防中间层断流
                continue
            if mode == "close":
                break
            if mode == "error":
                yield _sse({"type": "error", "message": f"Agent 执行出错：{data}"[:500]})
                break
            if mode == "messages":
                chunk, metadata = data
                # create_react_agent 的模型节点名为 "agent"（非 "model"）
                if metadata.get("langgraph_node") != "agent":
                    continue
                text = _extract_token_text(chunk.content)
                if text:
                    yield _sse({"type": "token", "content": text})
            elif mode == "updates":
                for node_name, state in data.items():
                    if node_name == "agent":
                        for msg in state.get("messages", []):
                            calls = getattr(msg, "tool_calls", None)
                            if calls:
                                for call in calls:
                                    key = call.get("id") or call.get("name")
                                    if key in seen_tools:
                                        continue
                                    seen_tools.add(key)
                                    yield _sse({
                                        "type": "tool_start",
                                        "name": call.get("name", ""),
                                        "args": call.get("args", {}),
                                    })
                    elif node_name == "tools":
                        for msg in state.get("messages", []):
                            preview = str(getattr(msg, "content", ""))[:TOOL_PREVIEW_CHARS]
                            yield _sse({"type": "tool_end", "name": "tools", "preview": preview})
        # 最终答案：检查点保存的最新 AI 消息（无工具调用的最终回答）
        state = agent.get_state(config)
        answer = ""
        for msg in reversed((state.values or {}).get("messages", [])):
            if getattr(msg, "type", "") == "ai" and not getattr(msg, "tool_calls", None):
                answer = _extract_token_text(getattr(msg, "content", ""))
                break
        yield _sse({"type": "done", "answer": answer})
    finally:
        task.cancel()  # 客户端断开 / 流结束 → 取消消费任务