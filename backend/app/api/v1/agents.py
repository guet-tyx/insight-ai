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
    status: str  # running / awaiting_review / ready / failed
    stages: list[dict[str, Any]]
    draft_report: str | None = None  # awaiting_review 时的报告草稿
    final_report: str | None = None
    error: str | None = None
    created_at: datetime


class ReviewRequest(BaseModel):
    action: str = Field(description="approve / reject / revise", pattern="^(approve|reject|revise)$")
    comment: str = Field(default="", max_length=2000, description="审核意见（revise 必填）")


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

    def set_awaiting_review(self, run_id: str, draft: str) -> None:
        """HITL 挂起：状态转 awaiting_review 并保存报告草稿。"""
        with self._lock:
            run = self._runs[run_id]
            self._runs[run_id] = run.model_copy(
                update={"status": "awaiting_review", "draft_report": draft}
            )

    def mark_running(self, run_id: str) -> None:
        with self._lock:
            run = self._runs[run_id]
            self._runs[run_id] = run.model_copy(
                update={"status": "running", "draft_report": None}
            )

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


def _push_stage_events(run_id: str, data: dict) -> bool:
    """消费一轮 updates：推阶段事件；返回是否遇到 __interrupt__（HITL 挂起）。"""
    if not isinstance(data, dict):
        return False
    if "__interrupt__" in data:
        return True
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
    return False


def _run_inputs(instruction: str) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": instruction}],
        "task_requirement": instruction,
        "next_node": "",
        "raw_artifacts": [],
        "semantic_chunks": [],
        "extracted_entities": [],
        "final_report": "",
        "human_feedback": "",
        "review_count": 0,
    }


async def _execute_run(run_id: str, instruction: str) -> None:
    """执行一次多智能体任务；遇 HITL interrupt 转 awaiting_review 挂起。"""
    from app.core.checkpointer import ensure_checkpointer

    await ensure_checkpointer()  # 检查点构建于本事件循环（幂等）
    g = agents_graph.get_graph()
    try:
        async for _mode, data in g.astream(_run_inputs(instruction),
                                           config={"configurable": {"thread_id": run_id}},
                                           stream_mode=["updates"]):
            if _push_stage_events(run_id, data):
                # HITL 挂起：保存草稿 → 等待人工审核
                state = await g.aget_state({"configurable": {"thread_id": run_id}})
                draft = (state.values or {}).get("final_report", "")
                run_store.set_awaiting_review(run_id, draft)
                logger.info("agents run %s 进入人工审核（草稿 %d 字）", run_id, len(draft))
                return
        await _finalize(run_id, g)
    except Exception as exc:  # noqa: BLE001 — 后台任务转 failed
        run_store.finish(run_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
        logger.error("agents run %s 失败：%s", run_id, exc, exc_info=True)


async def _continue_run(run_id: str, feedback: dict[str, Any]) -> None:
    """以 Command(resume=feedback) 恢复挂起的 HITL 审核。"""
    from langgraph.types import Command

    from app.core.checkpointer import ensure_checkpointer

    await ensure_checkpointer()
    g = agents_graph.get_graph()
    run_store.mark_running(run_id)
    try:
        async for _mode, data in g.astream(Command(resume=feedback),
                                           config={"configurable": {"thread_id": run_id}},
                                           stream_mode=["updates"]):
            if _push_stage_events(run_id, data):
                state = await g.aget_state({"configurable": {"thread_id": run_id}})
                draft = (state.values or {}).get("final_report", "")
                run_store.set_awaiting_review(run_id, draft)
                logger.info("agents run %s 修订后再次进入人工审核", run_id)
                return
        await _finalize(run_id, g)
    except Exception as exc:  # noqa: BLE001
        # 限流/瞬时故障：不判死任务，置回 awaiting_review 供再次审核
        if "429" in str(exc) or "RateLimit" in str(exc):
            state = await g.aget_state({"configurable": {"thread_id": run_id}})
            draft = (state.values or {}).get("final_report", "")
            run_store.set_awaiting_review(run_id, draft or "")
            run_store.append_stage(run_id, {
                "type": "stage", "stage": "human_review",
                "detail": "恢复执行遇瞬时限流，请重新提交审核",
            })
            logger.warning("agents run %s 恢复执行遇限流，回到待审核", run_id)
            return
        run_store.finish(run_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
        logger.error("agents run %s 恢复执行失败：%s", run_id, exc, exc_info=True)


async def _finalize(run_id: str, g) -> None:
    """任务完成：读取最终报告并置 ready。"""
    state = await g.aget_state({"configurable": {"thread_id": run_id}})
    final_report = (state.values or {}).get("final_report", "")
    run_store.finish(run_id, "ready", report=final_report)
    logger.info("agents run %s 完成（报告 %d 字）", run_id, len(final_report))


@router.post("/runs", response_model=RunStartResponse, status_code=202)
async def create_run(payload: RunRequest, _: User = Depends(get_current_user)) -> RunStartResponse:
    """启动多智能体任务：202 返回 run_id；SSE 阶段事件见 GET /runs/{id}/stream。"""
    run_id = run_store.create()
    asyncio.create_task(_execute_run(run_id, payload.instruction))
    return RunStartResponse(run_id=run_id)


@router.post("/runs/{run_id}/review", response_model=RunStartResponse, status_code=202)
async def review_run(
    run_id: str,
    payload: ReviewRequest,
    _: User = Depends(get_current_user),
) -> RunStartResponse:
    """HITL 审核：approve 通过 / reject 拒绝 / revise 带意见返回修订。

    仅 awaiting_review 状态可审核；revise 必须提供修改意见（422 校验）。
    """
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run.status != "awaiting_review":
        raise HTTPException(status_code=409, detail=f"任务当前状态 {run.status}，不可审核")
    if payload.action == "revise" and not payload.comment.strip():
        raise HTTPException(status_code=422, detail="revise 必须提供修改意见 comment")
    asyncio.create_task(
        _continue_run(run_id, {"action": payload.action, "comment": payload.comment})
    )
    return RunStartResponse(run_id=run_id, status="running")


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
        _last_review_draft: list[str] = [""]  # 已推送审核事件的草稿（修订后 draft 变化会重推）
        # 轮询任务表直至终态（后台 ainvoke 异步推进；HITL 等待期保持连接）
        while True:
            now = run_store.get(run_id)
            if now is None:
                yield _sse({"type": "error", "message": "任务不存在"})
                break
            new_stages = now.stages[last_count:]
            for stage in new_stages:
                yield _sse(stage)
            last_count = len(now.stages)
            # W8：HITL 挂起 → 推送 review_required（含草稿全文；修订后 draft 变化重推）
            if now.status == "awaiting_review" and now.draft_report != _last_review_draft[0]:
                yield _sse({"type": "review_required", "draft": now.draft_report or ""})
                _last_review_draft[0] = now.draft_report or ""
            if now.status in ("ready", "failed"):
                if now.final_report:
                    yield _sse({"type": "done", "answer": now.final_report})
                if now.error:
                    yield _sse({"type": "error", "message": now.error})
                break
            # 兜底：执行异常/超时（避免无限挂起；审核等待期同样适用）
            if (datetime.now(timezone.utc) - run.created_at).total_seconds() > 900:
                yield _sse({"type": "error", "message": "任务执行超时（15 分钟）"})
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