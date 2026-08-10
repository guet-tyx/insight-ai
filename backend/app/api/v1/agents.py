"""智能体路由（占位）：W5+ 实现 Supervisor-Worker 多智能体编排与 HITL 审核。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/runs")
def create_run() -> None:
    """启动一次复合情报分析任务（Supervisor 拆解 → Collector/Research/Analyst）。"""
    raise HTTPException(status_code=501, detail="W5 实现：多智能体编排")


@router.post("/runs/{run_id}/review")
def review_run(run_id: str) -> None:
    """HITL 审核：批准 / 拒绝 / 注入修改意见（interrupt 恢复）。"""
    raise HTTPException(status_code=501, detail="W8 实现：HITL 人机协作审核")