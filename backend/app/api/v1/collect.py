"""采集路由：POST /collect 启动自然语言采集（后台执行）+ 状态轮询。"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.collect import CollectRequest, CollectStartResponse, CollectTaskOut
from app.services.collector_service import collect_task, task_store, validate_url

router = APIRouter(prefix="/collect", tags=["collect"])


@router.post(
    "", response_model=CollectStartResponse, status_code=202,
    summary="启动采集任务（自然语言指令，后台执行）",
)
async def start_collect(
    payload: CollectRequest,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    _: User = Depends(get_current_user),
) -> CollectStartResponse:
    """202 立即返回 task_id；结果通过 GET /collect/tasks/{task_id} 轮询。"""
    ok, err = validate_url(payload.url)
    if not ok:
        raise HTTPException(status_code=422, detail=err)
    # output_schema 合法性前置校验（避免任务进入后台才失败）
    if payload.output_schema is not None:
        try:
            from app.services.collector_service import schema_to_pydantic

            schema_to_pydantic("CollectOutput", payload.output_schema)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    task_id = task_store.create(payload.url)
    background_tasks.add_task(
        collect_task, task_id, payload.url, payload.instruction,
        payload.output_schema, payload.max_steps,
    )
    return CollectStartResponse(task_id=task_id, url=payload.url)


@router.get("/tasks/{task_id}", response_model=CollectTaskOut)
def get_task(
    task_id: str,
    _: User = Depends(get_current_user),
) -> CollectTaskOut:
    """采集任务状态轮询：running → ready（含 data）/ failed（含 error）。"""
    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task