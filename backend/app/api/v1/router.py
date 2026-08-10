"""API v1 路由聚合。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import agents, auth, chat, collect, health, knowledge

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(knowledge.router)
api_router.include_router(collect.router)
api_router.include_router(chat.router)
api_router.include_router(agents.router)