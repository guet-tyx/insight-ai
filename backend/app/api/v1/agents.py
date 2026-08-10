"""多智能体路由（W5）：启动 Supervisor-Worker 任务（SSE 阶段事件流）+ 状态轮询。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents import graph as agents_graph
from app.core.deps import get_current_user
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


class RunRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000, description="复合情报分析指令")


class RunStartResponse(BaseModel):
    run_id: str
    status: str = "running"


class RunStatusOut(BaseModel):
    run_id: str
    status: str  # running / ready / failed
    stages: list[dict[str, Any]]
    final_report: str | None = None
    error: str | None = None
    created_at: datetime


class _RunStore:
    """内存态任务表（与 collector 任务表同模式；W11 换 Celery）。"""

    def __init__(self) -> None:
        self._runs: dict[str, RunStatusOut] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        run_id = uuid.uuid4().hex
        with self._lock:
            self._runs[run_id] = RunStatusOut(
                run_id=run_id, status="running", stages=[],
                created_at=datetime.now(timezone.utc),
            )
        return run_id

    def append_stage(self, run_id: str, stage: dict[str, Any]) -> None:
        with self._lock:
            run = self._runs[run_id]
            self._runs[run_id] = run.model_copy(update={"stages": [*run.stages, stage]})

    def finish(self, run_id: str, status: str, report: str | None = None, error: str | None = None) -> None:
        with self._lock:
            run = self._runs[run_id]
            self._runs[run_id] = run.model_copy(
                update={"status": status, "final_report": report, "error": error}
            )

    def get(self, run_id: str) -> RunStatusOut | None:
        with self._lock:
            return self._runs.get(run_id)


run_store = _RunStore()


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _execute_run(run_id: str, instruction: str) -> None:
    """执行一次多智能体任务；astream 逐阶段产出事件并入任务表（供 /stream 推送）。"""
    from app.core.checkpointer import ensure_checkpointer

    await ensure_checkpointer()  # 检查点构建于本事件循环（幂等）
    g = agents_graph.get_graph()
    inputs = {
        "messages": [{"role": "user", "content": instruction}],
        "task_requirement": instruction,
        "next_node": "",
        "raw_artifacts": [],
        "extracted_entities": [],
        "final_report": "",
        "human_feedback": "",
    }
    try:
        async for _mode, data in g.astream(inputs, config={"configurable": {"thread_id": run_id}},
                                           stream_mode=["updates"]):
            for node, state in data.items():
                if not isinstance(state, dict):
                    continue
                if "next_node" in state:  # Supervisor 决策
                    run_store.append_stage(run_id, {
                        "type": "stage", "stage": "supervisor",
                        "next": state.get("next_node", ""),
                        "detail": f"将任务交给 {state.get('next_node', '')}",
                    })
                elif "raw_artifacts" in state:  # Collector 产出
                    arts = state.get("raw_artifacts") or []
                    err = arts[0].get("error") if arts and arts[0].get("error") else ""
                    run_store.append_stage(run_id, {
                        "type": "stage", "stage": "collector",
                        "detail": f"采集产出 {len(arts)} 条" + (f"（{err}）" if err else ""),
                    })
                elif "semantic_chunks" in state:  # Research 产出
                    run_store.append_stage(run_id, {
                        "type": "stage", "stage": "research",
                        "detail": f"检索片段 {len(state.get('semantic_chunks') or [])} 条",
                    })
                elif "final_report" in state:  # Analyst 产出
                    report = state.get("final_report") or ""
                    run_store.append_stage(run_id, {
                        "type": "stage", "stage": "analyst",
                        "detail": f"报告生成 {len(report)} 字",
                    })
        # 稳态读取最终报告（异步取态：AsyncRedisSaver 禁止主线程同步调用）
        state = await g.aget_state({"configurable": {"thread_id": run_id}})
        final_report = (state.values or {}).get("final_report", "")
        run_store.finish(run_id, "ready", report=final_report)
        logger.info("agents run %s 完成（报告 %d 字）", run_id, len(final_report))
    except Exception as exc:  # noqa: BLE001 — 后台任务转 failed
        run_store.finish(run_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
        logger.error("agents run %s 失败：%s", run_id, exc, exc_info=True)


@router.post("/runs", response_model=RunStartResponse, status_code=202)
async def create_run(payload: RunRequest, _: User = Depends(get_current_user)) -> RunStartResponse:
    """启动多智能体任务：202 返回 run_id；SSE 阶段事件见 GET /runs/{id}/stream。"""
    run_id = run_store.create()
    asyncio.create_task(_execute_run(run_id, payload.instruction))
    return RunStartResponse(run_id=run_id)


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, _: User = Depends(get_current_user)) -> StreamingResponse:
    """SSE 流式阶段事件（运行中逐阶段推送；完成后含 final_report）。"""
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_stream():
        # 已完成的阶段先补发
        for stage in run.stages:
            yield _sse(stage)
        last_count = len(run.stages)
        deadline = datetime.now(timezone.utc)
        # 轮询任务表直至终态（后台 ainvoke 异步推进）
        while True:
            now = run_store.get(run_id)
            if now is None:
                yield _sse({"type": "error", "message": "任务不存在"})
                break
            new_stages = now.stages[last_count:]
            for stage in new_stages:
                yield _sse(stage)
            last_count = len(now.stages)
            if now.status in ("ready", "failed"):
                if now.final_report:
                    yield _sse({"type": "done", "answer": now.final_report})
                if now.error:
                    yield _sse({"type": "error", "message": now.error})
                break
            # 兜底：执行异常/超时（避免无限挂起）
            if (datetime.now(timezone.utc) - run.created_at).total_seconds() > 600:
                yield _sse({"type": "error", "message": "任务执行超时（10 分钟）"})
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}", response_model=RunStatusOut)
def get_run(run_id: str, _: User = Depends(get_current_user)) -> RunStatusOut:
    """任务状态查询（stages 阶段明细 + final_report）。"""
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return run