"""Insight AI 后端入口：FastAPI 应用工厂。

启动：uv run uvicorn app.main:app --reload --port 8000
文档：http://localhost:8000/docs（OpenAPI 自动生成）
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.db.session import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表（SQLite 零迁移 MVP；后续周次引入 Alembic）。"""
    Base.metadata.create_all(bind=engine)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "基于 LangGraph 多智能体协作与 MCP 架构的情报分析平台后端。\n\n"
            "认证流程：POST /api/v1/auth/register 注册 → POST /api/v1/auth/login "
            "获取 access_token → 点击右上角 Authorize 填入后调用受保护接口。"
        ),
        lifespan=lifespan,
    )

    # 开发环境放开 CORS（W4 前端联调；生产由网关/同源收口）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()