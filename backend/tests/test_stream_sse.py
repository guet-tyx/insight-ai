"""W10 stream_sse 事件流单元测试：token/tool_start/tool_end/done/心跳/错误分支。

用 Fake Agent 驱动（不依赖真实 LLM）：覆盖 stream_sse 的完整事件协议与
Trace 埋点（tool ok/fail 计数），以及心跳（空白超时）与错误传播。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.services.agent_service import stream_sse


class _FakeAgent:
    """模拟 langgraph ReAct agent 的 astream/aget_state。"""

    def __init__(self, fail: bool = False, slow_token: bool = False) -> None:
        self.fail = fail
        self.slow_token = slow_token
        self.last_inputs: dict = {}
        self.last_config: dict = {}
        self.last_mode: list | None = None

    async def astream(self, inputs, config, stream_mode=None, **_k):
        self.last_inputs = inputs
        self.last_config = config
        self.last_mode = stream_mode
        if self.fail:
            raise RuntimeError("LLM 网关超时")
        yield "messages", (AIMessage(content="正在检索知识库…"), {"langgraph_node": "agent"})
        yield "updates", {"agent": {"messages": [
            AIMessage(content="", tool_calls=[{
                "id": "call_1", "name": "knowledge_search", "args": {"query": "检索技术"},
                "type": "tool_call",
            }])
        ]}}
        yield "updates", {"tools": {"messages": [
            ToolMessage(content="[1] (文档 abc12345，第 1 页)：Milvus HNSW",
                        name="knowledge_search", tool_call_id="call_1"),
        ]}}
        if self.slow_token:
            await asyncio.sleep(0.05)
        yield "messages", (AIMessage(content="最终答案：平台采用 Milvus [1]。"),
                           {"langgraph_node": "agent"})

    async def aget_state(self, config):
        from types import SimpleNamespace

        return SimpleNamespace(values={"messages": [
            AIMessage(content="最终答案：平台采用 Milvus [1]。"),
        ]})


def _collect(agent, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """消费 stream_sse 生成器，返回事件列表（桩掉工具工厂与 agent 获取）。"""
    import app.services.agent_service as svc
    from app.agents import tool_factory

    async def _noop_tools():
        return []

    def _get_agent(tools=None):
        return agent

    monkeypatch.setattr(tool_factory, "get_agent_tools", _noop_tools)
    monkeypatch.setattr(svc, "get_agent", _get_agent)
    async def _noop_checkpointer():
        return None

    monkeypatch.setattr(svc, "ensure_checkpointer", _noop_checkpointer)  # memory 检查点

    async def _run() -> list[dict]:
        events = []
        async for frame in stream_sse("test-session", "平台用了什么检索技术？"):
            if frame.startswith("data: "):
                events.append(json.loads(frame[6:]))
            else:
                events.append({"type": "_raw", "frame": frame})
        return events

    return asyncio.run(_run())


def test_stream_sse_full_event_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """token → tool_start → tool_end → token → done 完整事件链。"""
    agent = _FakeAgent()
    events = _collect(agent, monkeypatch)

    types = [e["type"] for e in events]
    assert "token" in types and "tool_start" in types
    assert "tool_end" in types and "done" in types
    assert types[0] == "token" and types[-1] == "done"

    tool_start = next(e for e in events if e["type"] == "tool_start")
    assert tool_start["name"] == "knowledge_search"
    assert tool_start["args"] == {"query": "检索技术"}

    done = events[-1]
    assert "Milvus" in done["answer"]
    # agent 收到输入与 thread_id 配置
    assert agent.last_inputs["messages"][0]["content"] == "平台用了什么检索技术？"
    assert agent.last_config["configurable"]["thread_id"] == "test-session"


def test_stream_sse_dedup_tool_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一工具调用 ID 只发一次 tool_start（去重）。"""
    agent = _FakeAgent()

    async def _astream(self, inputs, config, stream_mode=None, **_k):
        yield "updates", {"agent": {"messages": [
            AIMessage(content="", tool_calls=[
                {"id": "call_1", "name": "knowledge_search", "args": {}, "type": "tool_call"},
            ])
        ]}}
        # 第二次 updates 再次携带同一调用（防御性场景）
        yield "updates", {"agent": {"messages": [
            AIMessage(content="", tool_calls=[
                {"id": "call_1", "name": "knowledge_search", "args": {}, "type": "tool_call"},
            ])
        ]}}
        yield "updates", {"tools": {"messages": [
            ToolMessage(content="ok", name="knowledge_search", tool_call_id="call_1"),
        ]}}

    agent.astream = _astream.__get__(agent)  # type: ignore[method-assign]
    events = _collect(agent, monkeypatch)
    starts = [e for e in events if e["type"] == "tool_start"]
    assert len(starts) == 1


def test_stream_sse_error_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """agent 执行抛异常 → error 事件（不 hang、无 done）。"""
    agent = _FakeAgent(fail=True)
    events = _collect(agent, monkeypatch)
    types = [e["type"] for e in events]
    assert "error" in types
    assert "done" not in types
    assert "网关超时" in next(e for e in events if e["type"] == "error")["message"]


def test_stream_sse_heartbeat_on_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具执行长耗时（空白超时）→ 输出 ': ping' 心跳。"""
    import app.services.agent_service as svc
    from app.agents import tool_factory

    monkeypatch.setattr(svc, "HEARTBEAT_SECONDS", 0.01)
    agent = _FakeAgent(slow_token=True)

    async def _noop_tools():
        return []

    monkeypatch.setattr(tool_factory, "get_agent_tools", _noop_tools)
    monkeypatch.setattr(svc, "get_agent", lambda tools=None: agent)
    async def _noop_checkpointer():
        return None

    monkeypatch.setattr(svc, "ensure_checkpointer", _noop_checkpointer)

    async def _run() -> list[str]:
        frames = []
        async for frame in stream_sse("s1", "问题"):
            frames.append(frame)
            if '"done"' in frame:
                break
        return frames

    frames = asyncio.run(_run())
    assert any(f.startswith(": ping") for f in frames)


def test_stream_sse_trace_tool_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具执行后 TraceLogger 计数：成功工具 +1（tool_fail_ratio 不受影响）。"""
    import app.services.agent_service as svc
    import app.services.trace_logger as tl
    from app.services.trace_logger import TraceLogger

    fake = TraceLogger()
    monkeypatch.setattr(tl, "trace", fake)
    agent = _FakeAgent()
    _collect(agent, monkeypatch)
    s = fake.summary()
    assert s["events_by_kind"].get("tool_ok", 0) >= 1
    assert s["tool_fail_ratio"] == 0.0


def test_stream_sse_trace_tool_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具返回失败文本（Error 前缀）→ tool_fail 计数 +1。"""
    import app.services.agent_service as svc
    import app.services.trace_logger as tl
    from app.services.trace_logger import TraceLogger

    agent = _FakeAgent()

    async def _astream(self, inputs, config, stream_mode=None, **_k):
        yield "updates", {"tools": {"messages": [
            ToolMessage(content="Error: 静态抓取被反爬拦截（HTTP 403）",
                        name="fetch_static", tool_call_id="call_2"),
        ]}}
        yield "messages", (AIMessage(content="采集失败"), {"langgraph_node": "agent"})

    agent.astream = _astream.__get__(agent)  # type: ignore[method-assign]
    fake = TraceLogger()
    monkeypatch.setattr(tl, "trace", fake)
    _collect(agent, monkeypatch)
    s = fake.summary()
    assert s["events_by_kind"].get("tool_fail", 0) == 1
    assert s["tool_fail_ratio"] == 1.0
