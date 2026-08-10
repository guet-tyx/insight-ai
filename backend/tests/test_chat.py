"""W4 Agent/聊天测试：工具注册、SSE 事件流、接口鉴权、端到端流式问答。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.services.agent_service import _extract_token_text, build_agent
from tests.conftest import INFRA_READY

# ---------- 单元：Agent 构建 ----------

def test_agent_builds_with_tools() -> None:
    agent = build_agent()
    assert {"__start__", "agent", "tools"} <= set(agent.nodes.keys())


def test_extract_token_text_filters_reasoning() -> None:
    # 含推理分区 + 文本分区的列表 content → 仅保留文本
    content = [
        {"type": "reasoning", "text": "思考过程……"},
        {"type": "text", "text": "最终"},
        {"type": "text", "text": "答案"},
    ]
    assert _extract_token_text(content) == "最终答案"
    # 纯字符串直通
    assert _extract_token_text("直接文本") == "直接文本"
    # 空值安全
    assert _extract_token_text("") == ""
    assert _extract_token_text(None) == ""
    assert _extract_token_text([]) == ""


# ---------- 接口：鉴权与基本行为 ----------

def test_chat_requires_auth(client: TestClient) -> None:
    assert client.post("/api/v1/chat/sessions").status_code == 401
    assert client.get("/api/v1/chat/sessions/xxx/messages").status_code == 401
    assert client.post("/api/v1/chat/sessions/xxx/messages", json={"message": "hi"}).status_code == 401


def test_create_session(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.post("/api/v1/chat/sessions", headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["session_id"]


def test_messages_unknown_session_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/chat/sessions/does-not-exist/messages", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.skipif(not INFRA_READY, reason="LLM Key 未就绪")
def test_end_to_end_stream_with_knowledge_tool(
    client: TestClient, auth_headers: dict[str, str], sample_pdf_bytes: bytes
) -> None:
    """端到端：上传文档 → 建会话 → 提问 → SSE 事件流含工具调用与最终答案。"""
    # 1) 准备知识库（直接走上传接口，后台任务会同步完成）
    upload = client.post(
        "/api/v1/knowledge/documents/upload",
        headers=auth_headers,
        files={"file": ("chat_test.pdf", sample_pdf_bytes, "application/pdf")},
    )
    assert upload.status_code == 202

    # 2) 创建会话
    session = client.post("/api/v1/chat/sessions", headers=auth_headers).json()["session_id"]

    # 3) 提问：应触发 knowledge_search 工具并给出引用答案
    with client.stream(
        "POST",
        f"/api/v1/chat/sessions/{session}/messages",
        headers=auth_headers,
        json={"message": "平台的知识库是如何存储数据的？"},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events: list[dict] = []
        for line in resp.iter_lines():
            if line and line.startswith("data: "):
                events.append(json.loads(line[6:]))

    types = [e["type"] for e in events]
    assert "done" in types, f"缺少 done 事件: {types}"
    assert "tool_start" in types, f"缺少工具调用事件: {types}"
    tool_events = [e for e in events if e["type"] == "tool_start"]
    assert any(e.get("name") == "knowledge_search" for e in tool_events)
    assert any(e["type"] == "tool_end" for e in events)

    done = next(e for e in events if e["type"] == "done")
    # 真实 LLM 输出措辞有随机性：仅断言有实质回答（内容准确性由 W2 检索单测覆盖）
    assert len(done["answer"]) > 20, f"答案过短或为空: {done['answer'][:200]}"

    # 4) 对话历史接口应返回 user/assistant 消息
    history = client.get(
        f"/api/v1/chat/sessions/{session}/messages", headers=auth_headers
    ).json()
    roles = [m["role"] for m in history]
    assert "user" in roles and "assistant" in roles
    assert any(len(m["content"]) > 20 for m in history if m["role"] == "assistant")


@pytest.mark.skipif(not INFRA_READY, reason="LLM Key 未就绪")
def test_multiturn_context(client: TestClient, auth_headers: dict[str, str]) -> None:
    """多轮上下文：第二问引用第一问的会话记忆（MemorySaver thread_id）。"""
    session = client.post("/api/v1/chat/sessions", headers=auth_headers).json()["session_id"]

    def ask(text: str) -> list[dict]:
        events: list[dict] = []
        with client.stream(
            "POST",
            f"/api/v1/chat/sessions/{session}/messages",
            headers=auth_headers,
            json={"message": text},
        ) as resp:
            for line in resp.iter_lines():
                if line and line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return events

    first = ask("你好，请记住：我的代号是猎鹰")
    done1 = next(e for e in first if e["type"] == "done")
    assert done1["answer"].strip()

    second = ask("我的代号是什么？")
    done2 = next(e for e in second if e["type"] == "done")
    assert "猎鹰" in done2["answer"], f"多轮记忆丢失: {done2['answer'][:120]}"